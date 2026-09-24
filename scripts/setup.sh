#!/bin/bash
# ===========================================
# aRSSe Setup Script
# ===========================================
# Dieses Skript initialisiert die aRSSe-Umgebung auf einem Unraid-Server
# oder einem anderen Docker-fähigen System. Es kann gefahrlos erneut
# ausgeführt werden: Eine bestehende .env wird nur ergänzt, nie überschrieben.
#
# Aufruf: scripts/setup.sh [--yes] [--no-start]
#   --yes       Services ohne Rückfrage starten
#   --no-start  Services nicht starten (Standard ohne Terminal)

set -euo pipefail
# .env enthält Passwörter und den API-Key
umask 077

# Farben für Ausgabe
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Konfiguration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
PLACEHOLDER="HIER_SICHERES_PASSWORT_EINTRAGEN"
DEFAULT_BASE_URL="http://localhost:8080"
OBSOLETE_KEYS=(CLUSTERING_EPS CLUSTERING_MIN_SAMPLES)

START_MODE="ask"
COMPOSE_CMD="docker compose"
WAIT_FLAG="--wait"
ENV_CREATED=0
ADMIN_PASSWORD_NEW=""
IP=""

# Funktionen
print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}  aRSSe - Autonomer News-Aggregator${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

print_step() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1" >&2
}

usage() {
    echo "Aufruf: $0 [--yes] [--no-start]"
    echo "  --yes       Services ohne Rückfrage starten"
    echo "  --no-start  Services nicht starten (Standard ohne Terminal)"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -y|--yes) START_MODE="yes" ;;
            --no-start) START_MODE="no" ;;
            -h|--help) usage; exit 0 ;;
            *) print_error "Unbekannte Option: $1"; usage >&2; exit 2 ;;
        esac
        shift
    done
}

# Zufälliges Passwort aus 24 alphanumerischen Zeichen (ohne openssl)
gen_pw() {
    local pw
    pw=$(head -c 48 /dev/urandom | base64 | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 24) || true
    if [ "${#pw}" -ne 24 ]; then
        print_error "Konnte kein Passwort erzeugen"
        exit 1
    fi
    echo "$pw"
}

# Wert aus .env lesen (letzte Zuweisung gewinnt, Anführungszeichen entfernt)
env_get() {
    local value
    value=$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2-) || true
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    echo "$value"
}

# Wie docker-compose.yml: ohne ADMIN_USERNAME heißt der Admin "admin"
admin_user() {
    local name
    name=$(env_get ADMIN_USERNAME)
    echo "${name:-admin}"
}

# KEY=VALUE in .env setzen (zeilengenau) oder anhängen
set_env() {
    local key="$1" value="$2" escaped
    escaped=$(printf '%s' "$value" | sed 's/[\\|&]/\\&/g')
    if grep -qE "^$key=" "$ENV_FILE"; then
        sed -i "s|^$key=.*|$key=$escaped|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

# Absoluter Datenpfad: Umgebung > .env > ./data
resolve_data_dir() {
    local path="${DATA_PATH:-}"
    [ -n "$path" ] || path=$(env_get DATA_PATH)
    path="${path:-./data}"
    [[ "$path" = /* ]] || path="$PROJECT_DIR/${path#./}"
    echo "$path"
}

detect_ip() {
    IP=$(hostname -I 2>/dev/null | awk '{print $1}') || IP=""
    if [ -z "$IP" ]; then
        IP=$(ip -4 route get 1.1.1.1 2>/dev/null \
            | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}') || IP=""
    fi
}

check_requirements() {
    echo -e "${BLUE}Prüfe Voraussetzungen...${NC}\n"

    # Docker prüfen (zum Starten zwingend, sonst nur Hinweis)
    if ! command -v docker &> /dev/null; then
        if [ "$START_MODE" = "no" ]; then
            print_warning "Docker ist nicht installiert – Services können nicht gestartet werden"
            echo ""
            return
        fi
        print_error "Docker ist nicht installiert!"
        exit 1
    fi
    print_step "Docker gefunden: $(docker --version)"

    # Docker Compose prüfen (v2-Plugin bevorzugt, kann auf Health Checks warten)
    if docker compose version &> /dev/null; then
        print_step "Docker Compose (Plugin) gefunden: $(docker compose version)"
    elif command -v docker-compose &> /dev/null; then
        print_step "Docker Compose gefunden: $(docker-compose --version)"
        print_warning "docker-compose v1 ist veraltet, bitte auf 'docker compose' umsteigen"
        COMPOSE_CMD="docker-compose"
        WAIT_FLAG=""
    elif [ "$START_MODE" = "no" ]; then
        print_warning "Docker Compose ist nicht installiert"
    else
        print_error "Docker Compose ist nicht installiert!"
        exit 1
    fi

    echo ""
}

create_env() {
    local data_dir postgres_password
    data_dir=$(resolve_data_dir)
    if [ -d /mnt/user/appdata ] && [ -z "${DATA_PATH:-}" ]; then
        data_dir="/mnt/user/appdata/arsse"
    fi

    # Postgres liest POSTGRES_PASSWORD nur beim ersten Start: neue Passwörter
    # würden nicht mehr zur bestehenden Datenbank passen
    if [ -f "$data_dir/postgresql/PG_VERSION" ]; then
        print_error "Datenbank existiert bereits – .env aus Backup wiederherstellen"
        print_error "($data_dir/postgresql gehört zu einer früheren .env; neue Passwörter"
        print_error " würden nicht mehr passen. Zum Neubeginn das Verzeichnis löschen.)"
        exit 1
    fi

    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    postgres_password=$(gen_pw)
    ADMIN_PASSWORD_NEW=$(gen_pw)
    set_env POSTGRES_PASSWORD "$postgres_password"
    set_env ADMIN_PASSWORD "$ADMIN_PASSWORD_NEW"

    # Datenpfad und Benutzer für Unraid anpassen
    if [ -d /mnt/user/appdata ]; then
        set_env DATA_PATH "${DATA_PATH:-/mnt/user/appdata/arsse}"
        set_env PUID 99
        set_env PGID 100
        print_step "Unraid-Umgebung erkannt, Pfade angepasst"
    elif [ -n "${DATA_PATH:-}" ]; then
        set_env DATA_PATH "$DATA_PATH"
    fi

    ENV_CREATED=1
    print_step ".env erstellt mit sicheren Passwörtern"
}

# Schlüssel aus .env.example ergänzen, die in einer älteren .env fehlen
upgrade_env() {
    local line key comments="" added=0
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)= ]]; then
            key="${BASH_REMATCH[1]}"
            # Auch auskommentierte Einträge gelten als bewusste Entscheidung
            if ! grep -qE "^#? *$key=" "$ENV_FILE"; then
                if [[ "$line" == *"$PLACEHOLDER"* ]]; then
                    print_warning "$key fehlt in .env – bitte selbst eintragen"
                else
                    if [ $added -eq 0 ]; then
                        [ -z "$(tail -c 1 "$ENV_FILE")" ] || echo "" >> "$ENV_FILE"
                        printf '\n# --- Ergänzt von setup.sh am %s ---\n' "$(date +%F)" >> "$ENV_FILE"
                    fi
                    [ -z "$comments" ] || echo "" >> "$ENV_FILE"
                    printf '%s%s\n' "$comments" "$line" >> "$ENV_FILE"
                    print_step "$key in .env ergänzt"
                    added=$((added + 1))
                fi
            fi
            comments=""
        elif [[ "$line" =~ ^#\ *-+$ ]]; then
            continue
        elif [[ "$line" == \#* ]]; then
            comments+="$line"$'\n'
        else
            comments=""
        fi
    done < "$ENV_EXAMPLE"

    for key in "${OBSOLETE_KEYS[@]}"; do
        if grep -qE "^$key=" "$ENV_FILE"; then
            print_warning "$key ist veraltet und wird ignoriert – bitte durch CLUSTERING_THRESHOLD ersetzen"
        fi
    done
    [ $added -gt 0 ] || print_step ".env ist aktuell"
}

setup_environment() {
    echo -e "${BLUE}Konfiguriere Umgebung...${NC}\n"

    if [ -f "$ENV_FILE" ]; then
        print_warning ".env existiert bereits, wird nur ergänzt"
        upgrade_env
    else
        create_env
    fi
    if chmod 600 "$ENV_FILE"; then
        print_step "Rechte von .env auf 600 gesetzt"
    else
        print_warning "Konnte Rechte von .env nicht setzen: bitte 'chmod 600 .env' ausführen"
    fi

    # Links der Top Stories zeigen auf BASE_URL; localhost wäre auf dem
    # E-Ink-Reader das Gerät selbst
    local base_url port
    base_url=$(env_get BASE_URL)
    port=$(env_get MINIFLUX_PORT)
    detect_ip
    if [ "$ENV_CREATED" -eq 1 ] || [ -z "$base_url" ] || [ "$base_url" = "$DEFAULT_BASE_URL" ]; then
        if [ -n "$IP" ]; then
            set_env BASE_URL "http://$IP:${port:-8080}"
            print_step "BASE_URL auf http://$IP:${port:-8080} gesetzt"
        else
            print_warning "IP-Adresse nicht ermittelbar: bitte BASE_URL in .env auf die Adresse setzen, unter der Ihre Geräte Miniflux erreichen"
        fi
    fi

    if [ "$ENV_CREATED" -eq 1 ]; then
        echo ""
        echo -e "${YELLOW}WICHTIG: Notieren Sie sich diese Zugangsdaten:${NC}"
        echo -e "  Admin-Benutzer: $(admin_user)"
        echo -e "  Admin-Passwort: $ADMIN_PASSWORD_NEW"
        echo "  (Das Passwort steht auch in .env und kann nach der ersten Anmeldung"
        echo "   in Miniflux geändert werden.)"
    fi
    echo ""
}

create_directories() {
    echo -e "${BLUE}Erstelle Verzeichnisstruktur...${NC}\n"

    local data_dir puid pgid
    data_dir=$(resolve_data_dir)
    puid=$(env_get PUID)
    pgid=$(env_get PGID)

    # Rechte setzen die Container selbst (Postgres: 700, Intelligence: PUID:PGID)
    mkdir -p "$data_dir/postgresql" "$data_dir/intelligence"
    chown "${puid:-1000}:${pgid:-1000}" "$data_dir/intelligence" 2>/dev/null || true
    print_step "Datenverzeichnis: $data_dir"

    echo ""
}

start_services() {
    echo -e "${BLUE}Starte Services...${NC}\n"

    cd "$PROJECT_DIR"

    # Nur Miniflux und PostgreSQL starten (Intelligence Layer braucht erst den API-Key)
    print_step "Starte Datenbank und Miniflux, warte auf Health Checks..."
    # shellcheck disable=SC2086
    if $COMPOSE_CMD up -d $WAIT_FLAG db miniflux; then
        print_step "Miniflux gestartet"
    else
        print_error "Miniflux konnte nicht gestartet werden!"
        echo ""
        echo "Logs anzeigen mit: $COMPOSE_CMD logs miniflux"
        exit 1
    fi

    echo ""
}

maybe_start_services() {
    case "$START_MODE" in
        yes) start_services ;;
        no) print_warning "Services nicht gestartet (starten mit: $COMPOSE_CMD up -d db miniflux)"; echo "" ;;
        *)
            if [[ -t 0 ]]; then
                read -p "Services jetzt starten? (j/N) " -n 1 -r || REPLY=n
                echo ""
            else
                REPLY=n
                print_warning "Kein Terminal: Services werden nicht gestartet (--yes erzwingt den Start)"
            fi
            if [[ $REPLY =~ ^[Jj]$ ]]; then
                start_services
            fi
            ;;
    esac
}

print_next_steps() {
    echo -e "${BLUE}Nächste Schritte:${NC}\n"

    local host port intelligence_port
    host="${IP:-<server-ip>}"
    port=$(env_get MINIFLUX_PORT)
    port="${port:-8080}"
    intelligence_port=$(env_get INTELLIGENCE_PORT)
    intelligence_port="${intelligence_port:-8081}"

    echo "1. Öffnen Sie Miniflux im Browser:"
    echo -e "   ${GREEN}http://$host:$port${NC}"
    echo ""
    echo "2. Melden Sie sich mit den Zugangsdaten aus dem Setup an"
    echo "   (Benutzer: $(admin_user), Passwort: ADMIN_PASSWORD in .env)"
    echo ""
    echo "3. Fügen Sie RSS-Feeds hinzu oder importieren Sie eine OPML-Datei"
    echo ""
    echo "4. (Optional) Generieren Sie einen API-Key unter:"
    echo "   Einstellungen > API-Schlüssel"
    echo ""
    echo "5. (Optional) Fügen Sie das E-Ink-Theme hinzu unter:"
    echo "   Einstellungen > Benutzerdefiniertes CSS"
    echo "   (CSS-Datei: $PROJECT_DIR/css/eink-theme.css)"
    echo "   Für die Web-Fonts unter 'Externe Schriftart-Hosts' eintragen:"
    echo "   fonts.googleapis.com fonts.gstatic.com"
    echo ""
    echo "6. (Optional) Starten Sie den Intelligence Layer (Top Stories):"
    echo "   a. Tragen Sie den API-Key in .env ein (MINIFLUX_API_KEY=...)"
    echo "   b. $COMPOSE_CMD up -d intelligence"
    echo -e "   c. Top Stories: ${GREEN}http://$host:$intelligence_port${NC}"
    echo "   Links in den Top Stories führen zu BASE_URL: $(env_get BASE_URL)"
    echo ""
    echo -e "${BLUE}Nützliche Befehle:${NC}"
    echo "  Status:    $COMPOSE_CMD ps"
    echo "  Logs:      $COMPOSE_CMD logs -f"
    echo "  Stoppen:   $COMPOSE_CMD down"
    echo "  Neustart:  $COMPOSE_CMD restart"
    echo ""
}

# Hauptprogramm
main() {
    parse_args "$@"
    print_header
    check_requirements
    setup_environment
    create_directories
    maybe_start_services
    print_next_steps

    echo -e "${GREEN}Setup abgeschlossen!${NC}"
}

# Skript ausführen
main "$@"

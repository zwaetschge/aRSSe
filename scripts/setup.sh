#!/bin/bash
# ===========================================
# aRSSe Setup Script
# ===========================================
# Dieses Skript initialisiert die aRSSe-Umgebung auf einem Unraid-Server
# oder einem anderen Docker-fähigen System. Es kann gefahrlos erneut
# ausgeführt werden: Eine bestehende .env wird nur ergänzt. Ersetzt werden
# einzig eine BASE_URL, die noch auf localhost zeigt, und Platzhalter-
# Passwörter, solange noch keine Datenbank existiert.
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
ENV_TMP=""
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

# KEY=VALUE in .env setzen (zeilengenau) oder anhängen. ENV_FILE ist
# dynamisch gebunden: create_env schreibt so in seine temporäre Datei.
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

# Host-Port aus MINIFLUX_PORT, auch bei Bindung an eine Adresse
# (127.0.0.1:8080, wie in docker-compose.yml erlaubt)
miniflux_port() {
    local port
    port=$(env_get MINIFLUX_PORT)
    port="${port##*:}"
    echo "${port:-8080}"
}

# Existiert schon eine Datenbank? Postgres liest POSTGRES_PASSWORD nur beim
# ersten Start. Nach dem ersten Start gehört das Verzeichnis Postgres
# (uid 70, Rechte 700): Was nicht lesbar oder nicht leer ist, gilt als
# bestehende Datenbank.
database_exists() {
    local pg="$1/postgresql"
    [ -d "$pg" ] || return 1
    [ -f "$pg/PG_VERSION" ] && return 0
    ls -A "$pg" > /dev/null 2>&1 || return 0
    [ -n "$(ls -A "$pg" 2>/dev/null)" ]
}

# Datenpfad einer neuen .env (Unraid: appdata)
new_env_data_dir() {
    if [ -d /mnt/user/appdata ] && [ -z "${DATA_PATH:-}" ]; then
        echo "/mnt/user/appdata/arsse"
    else
        resolve_data_dir
    fi
}

# Neues Passwort, das sich vom übergebenen unterscheidet (errexit gilt in
# Befehlssubstitutionen nicht, daher explizit abbrechen)
gen_distinct_pw() {
    local pw
    pw=$(gen_pw) || exit 1
    while [ "$pw" = "${1:-}" ]; do pw=$(gen_pw) || exit 1; done
    echo "$pw"
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
    local data_dir postgres_password target="$ENV_FILE"
    data_dir=$(new_env_data_dir)

    # Neue Passwörter würden nicht mehr zur bestehenden Datenbank passen
    if database_exists "$data_dir"; then
        print_error "Datenbank existiert bereits – .env aus Backup wiederherstellen"
        print_error "($data_dir/postgresql gehört zu einer früheren .env; neue Passwörter"
        print_error " würden nicht mehr passen. Zum Neubeginn das Verzeichnis löschen.)"
        exit 1
    fi

    # In einer temporären Datei aufbauen: Ein Abbruch hinterlässt keine
    # .env mit Platzhalter-Passwörtern
    ENV_TMP=$(mktemp "$PROJECT_DIR/.env.XXXXXX")
    cp "$ENV_EXAMPLE" "$ENV_TMP"
    chmod 600 "$ENV_TMP"
    local ENV_FILE="$ENV_TMP"

    postgres_password=$(gen_pw)
    ADMIN_PASSWORD_NEW=$(gen_distinct_pw "$postgres_password")
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

    mv "$ENV_TMP" "$target"
    ENV_TMP=""
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
                # Fehlende Passwörter übernimmt fill_passwords
                if [[ "$line" != *"$PLACEHOLDER"* ]]; then
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

# Leere oder Platzhalter-Passwörter einer bestehenden .env ersetzen (z.B.
# nach 'cp .env.example .env'), solange noch keine Datenbank existiert
fill_passwords() {
    local key value password data_dir missing=()
    for key in POSTGRES_PASSWORD ADMIN_PASSWORD; do
        value=$(env_get "$key")
        if [ -z "$value" ] || [ "$value" = "$PLACEHOLDER" ]; then
            missing+=("$key")
        fi
    done
    [ ${#missing[@]} -gt 0 ] || return 0

    data_dir=$(resolve_data_dir)
    if database_exists "$data_dir"; then
        # Miniflux liest ADMIN_PASSWORD nur, solange der Admin noch nicht
        # existiert: leer ist dann unschädlich, der Platzhalter nicht
        if [ "${missing[*]}" = ADMIN_PASSWORD ] && [ -z "$(env_get ADMIN_PASSWORD)" ]; then
            return 0
        fi
        print_error "${missing[*]} in .env ist leer oder noch der Platzhalter,"
        print_error "die Datenbank in $data_dir/postgresql existiert aber schon."
        print_error "Neue Passwörter würden nicht mehr passen: bitte die bisherigen aus dem"
        print_error "Backup eintragen (wurde mit dem Platzhalter gestartet: Passwörter ändern)."
        print_error "Services werden nicht gestartet."
        exit 1
    fi

    for key in "${missing[@]}"; do
        case "$key" in
            POSTGRES_PASSWORD)
                password=$(gen_distinct_pw "$(env_get ADMIN_PASSWORD)")
                set_env "$key" "$password"
                ;;
            ADMIN_PASSWORD)
                ADMIN_PASSWORD_NEW=$(gen_distinct_pw "$(env_get POSTGRES_PASSWORD)")
                set_env "$key" "$ADMIN_PASSWORD_NEW"
                ;;
        esac
        print_step "$key in .env durch zufälliges Passwort ersetzt"
    done
}

setup_environment() {
    echo -e "${BLUE}Konfiguriere Umgebung...${NC}\n"

    if [ -f "$ENV_FILE" ]; then
        print_warning ".env existiert bereits, wird nur ergänzt"
        upgrade_env
        fill_passwords
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
    port=$(miniflux_port)
    detect_ip
    if [ "$ENV_CREATED" -eq 1 ] || [ -z "$base_url" ] || [ "$base_url" = "$DEFAULT_BASE_URL" ]; then
        if [ -n "$IP" ]; then
            set_env BASE_URL "http://$IP:$port"
            print_step "BASE_URL auf http://$IP:$port gesetzt"
        else
            print_warning "IP-Adresse nicht ermittelbar: bitte BASE_URL in .env auf die Adresse setzen, unter der Ihre Geräte Miniflux erreichen"
        fi
    fi

    if [ -n "$ADMIN_PASSWORD_NEW" ]; then
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
    prepare_user_config "$data_dir/intelligence/config.yaml" "${puid:-1000}:${pgid:-1000}"

    echo ""
}

# Wurde intelligence/config.yaml im Git-Arbeitsverzeichnis geändert? (So
# stand es früher im README; heute blockiert das 'git pull'.)
reference_config_modified() {
    local rc=0
    command -v git &> /dev/null || return 1
    git -C "$PROJECT_DIR" ls-files --error-unmatch intelligence/config.yaml &> /dev/null \
        || return 1
    git -C "$PROJECT_DIR" diff --quiet HEAD -- intelligence/config.yaml &> /dev/null || rc=$?
    [ "$rc" -eq 1 ]
}

# Eigene Einstellungen des Intelligence Layers: DATA_PATH/intelligence/config.yaml
# (im Container /app/data/config.yaml). Neu: kommentierte Vorlage. Wurde
# intelligence/config.yaml geändert, werden diese Änderungen übernommen,
# solange die eigene Datei fehlt oder noch die unveränderte Vorlage ist.
prepare_user_config() {
    local target="$1" owner="$2"
    local stub="$PROJECT_DIR/intelligence/config.stub.yaml"
    local reference="$PROJECT_DIR/intelligence/config.yaml"
    local untouched=false
    [ -f "$stub" ] || return 0
    if [ ! -e "$target" ] || [ "$(cat "$target")" = "$(cat "$stub")" ]; then
        untouched=true
    fi

    if reference_config_modified; then
        if [ "$untouched" = true ]; then
            cp "$reference" "$target"
            print_warning "Ihre Änderungen an intelligence/config.yaml wurden übernommen nach"
            print_warning "  $target (bleibt bei Updates erhalten)."
            print_warning "  Bitte dort nur die geänderten Einstellungen stehen lassen und die"
            print_warning "  Referenz zurücksetzen, sonst blockiert sie 'git pull':"
            print_warning "  git checkout -- intelligence/config.yaml"
        else
            print_warning "intelligence/config.yaml ist geändert. Einstellungen, die in $target"
            print_warning "  fehlen, gelten vorerst weiter (das Log nennt sie), aber nur für den Umstieg:"
            print_warning "  bitte nach $target übernehmen und dann"
            print_warning "  git checkout -- intelligence/config.yaml"
            return 0
        fi
    elif [ ! -e "$target" ]; then
        cp "$stub" "$target"
        print_step "Vorlage für eigene Einstellungen: $target"
    else
        return 0
    fi
    # Nur für den Dienst lesbar: Die Datei darf Passwörter enthalten
    chmod 600 "$target"
    chown "$owner" "$target" 2>/dev/null || true
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
            read -p "Services jetzt starten? (j/N) " -n 1 -r || REPLY=n
            echo ""
            if [[ $REPLY =~ ^[Jj]$ ]]; then
                start_services
            fi
            ;;
    esac
}

print_next_steps() {
    echo -e "${BLUE}Nächste Schritte:${NC}\n"

    local host port intelligence_port intelligence_host local_only auth_mode
    host="${IP:-<server-ip>}"
    port=$(miniflux_port)
    intelligence_port=$(env_get INTELLIGENCE_PORT)
    # INTELLIGENCE_PORT=127.0.0.1:8081 veröffentlicht nur auf dieser Adresse
    intelligence_host="$host"
    case "$intelligence_port" in
        *:*) intelligence_host="${intelligence_port%:*}" ;;
    esac
    case "$intelligence_host" in
        0.0.0.0|::|"[::]"|"") intelligence_host="$host" ;;
    esac
    local_only=false
    case "$intelligence_host" in
        127.*|localhost|"[::1]") local_only=true ;;
    esac
    intelligence_port="${intelligence_port##*:}"
    intelligence_port="${intelligence_port:-8081}"
    auth_mode=$(env_get WEB_AUTH_MODE)

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
    echo "   Empfohlen: als eigener Benutzer ohne Admin-Rechte, mit dem Sie"
    echo "   auch lesen (Einstellungen > Benutzer, siehe README 'Absicherung')"
    echo ""
    echo "5. (Optional) Fügen Sie das E-Ink-Theme hinzu unter:"
    echo "   Einstellungen > Benutzerdefiniertes CSS"
    echo "   (CSS-Datei: $PROJECT_DIR/css/eink-theme.css)"
    echo "   Es nutzt die Schriften des Geräts, externe Schriftart-Hosts sind"
    echo "   nicht nötig"
    echo ""
    echo "6. (Optional) Starten Sie den Intelligence Layer (Top Stories):"
    echo "   a. Tragen Sie den API-Key in .env ein (MINIFLUX_API_KEY=...)"
    echo "   b. $COMPOSE_CMD up -d intelligence"
    echo -e "   c. Top Stories: ${GREEN}http://$intelligence_host:$intelligence_port${NC}"
    if [ "$local_only" = true ]; then
        echo "   Nur auf dem Server selbst bzw. über den Reverse Proxy erreichbar"
    elif [ -z "$auth_mode" ] || [ "$auth_mode" = "none" ]; then
        echo "   Ohne WEB_AUTH_MODE in .env kann jeder im Netz die Top Stories lesen"
    fi
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
    trap '[ -z "$ENV_TMP" ] || rm -f "$ENV_TMP"' EXIT
    print_header
    # Ohne Terminal keine Rückfrage: nicht starten (auch Docker ist dann optional)
    if [ "$START_MODE" = "ask" ] && [[ ! -t 0 ]]; then
        START_MODE="no"
        print_warning "Kein Terminal: Services werden nicht gestartet (--yes erzwingt den Start)"
        echo ""
    fi
    check_requirements
    setup_environment
    create_directories
    maybe_start_services
    print_next_steps

    echo -e "${GREEN}Setup abgeschlossen!${NC}"
}

# Skript ausführen
main "$@"

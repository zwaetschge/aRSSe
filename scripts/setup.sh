#!/bin/bash
# ===========================================
# aRSSe Setup Script
# ===========================================
# Dieses Skript initialisiert die aRSSe-Umgebung auf einem Unraid-Server
# oder einem anderen Docker-fähigen System.

set -e

# Farben für Ausgabe
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Konfiguration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="${DATA_PATH:-$PROJECT_DIR/data}"

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
    echo -e "${RED}[✗]${NC} $1"
}

check_requirements() {
    echo -e "${BLUE}Prüfe Voraussetzungen...${NC}\n"

    # Docker prüfen
    if ! command -v docker &> /dev/null; then
        print_error "Docker ist nicht installiert!"
        exit 1
    fi
    print_step "Docker gefunden: $(docker --version)"

    # Docker Compose prüfen (v2-Plugin bevorzugt, kann auf Health Checks warten)
    if docker compose version &> /dev/null; then
        print_step "Docker Compose (Plugin) gefunden: $(docker compose version)"
        COMPOSE_CMD="docker compose"
        WAIT_FLAG="--wait"
    elif command -v docker-compose &> /dev/null; then
        print_step "Docker Compose gefunden: $(docker-compose --version)"
        print_warning "docker-compose v1 ist veraltet, bitte auf 'docker compose' umsteigen"
        COMPOSE_CMD="docker-compose"
        WAIT_FLAG=""
    else
        print_error "Docker Compose ist nicht installiert!"
        exit 1
    fi

    echo ""
}

create_directories() {
    echo -e "${BLUE}Erstelle Verzeichnisstruktur...${NC}\n"

    mkdir -p "$DATA_DIR/postgresql"
    mkdir -p "$DATA_DIR/intelligence"

    print_step "Datenverzeichnis erstellt: $DATA_DIR"

    # Berechtigungen setzen (wichtig für PostgreSQL)
    chmod 700 "$DATA_DIR/postgresql"
    print_step "Berechtigungen gesetzt"

    echo ""
}

setup_environment() {
    echo -e "${BLUE}Konfiguriere Umgebung...${NC}\n"

    ENV_FILE="$PROJECT_DIR/.env"

    if [ -f "$ENV_FILE" ]; then
        print_warning ".env existiert bereits, überspringe..."
    else
        cp "$PROJECT_DIR/.env.example" "$ENV_FILE"

        # Generiere sichere Passwörter
        POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)
        ADMIN_PASSWORD=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)

        # Ersetze Platzhalter
        sed -i "s/HIER_SICHERES_PASSWORT_EINTRAGEN/$POSTGRES_PASSWORD/1" "$ENV_FILE"
        sed -i "s/HIER_SICHERES_PASSWORT_EINTRAGEN/$ADMIN_PASSWORD/1" "$ENV_FILE"

        # Datenpfad für Unraid anpassen
        if [ -d "/mnt/user/appdata" ]; then
            sed -i "s|DATA_PATH=./data|DATA_PATH=/mnt/user/appdata/arsse|" "$ENV_FILE"
            sed -i "s|^PUID=.*|PUID=99|; s|^PGID=.*|PGID=100|" "$ENV_FILE"
            mkdir -p /mnt/user/appdata/arsse/postgresql
            mkdir -p /mnt/user/appdata/arsse/intelligence
            print_step "Unraid-Umgebung erkannt, Pfade angepasst"
        fi

        print_step ".env erstellt mit sicheren Passwörtern"
        echo ""
        echo -e "${YELLOW}WICHTIG: Notieren Sie sich diese Zugangsdaten:${NC}"
        echo -e "  Admin-Benutzer: admin"
        echo -e "  Admin-Passwort: $ADMIN_PASSWORD"
        echo ""
    fi

    fix_permissions
    echo ""
}

fix_permissions() {
    # Der Intelligence-Container läuft als PUID:PGID und braucht Schreibrechte
    local env_file="$PROJECT_DIR/.env"
    local data_path puid pgid
    data_path=$(grep -E '^DATA_PATH=' "$env_file" | cut -d= -f2)
    puid=$(grep -E '^PUID=' "$env_file" | cut -d= -f2)
    pgid=$(grep -E '^PGID=' "$env_file" | cut -d= -f2)
    data_path="${data_path:-./data}"
    [[ "$data_path" = /* ]] || data_path="$PROJECT_DIR/${data_path#./}"

    mkdir -p "$data_path/intelligence"
    if chown "${puid:-1000}:${pgid:-1000}" "$data_path/intelligence" 2>/dev/null; then
        print_step "Rechte für $data_path/intelligence gesetzt (${puid:-1000}:${pgid:-1000})"
    else
        print_warning "Konnte Besitzer von $data_path/intelligence nicht setzen."
        print_warning "Bitte ausführen: sudo chown ${puid:-1000}:${pgid:-1000} $data_path/intelligence"
    fi
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

print_next_steps() {
    echo -e "${BLUE}Nächste Schritte:${NC}\n"

    # IP-Adresse ermitteln
    IP=$(hostname -I | awk '{print $1}')
    PORT=$(grep -E '^MINIFLUX_PORT=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2)
    PORT="${PORT:-8080}"
    INTELLIGENCE_PORT=$(grep -E '^INTELLIGENCE_PORT=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2)
    INTELLIGENCE_PORT="${INTELLIGENCE_PORT:-8081}"

    echo "1. Öffnen Sie Miniflux im Browser:"
    echo -e "   ${GREEN}http://$IP:$PORT${NC}"
    echo ""
    echo "2. Melden Sie sich mit den Zugangsdaten aus dem Setup an"
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
    echo -e "   c. Top Stories: ${GREEN}http://$IP:$INTELLIGENCE_PORT${NC}"
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
    print_header
    check_requirements
    create_directories
    setup_environment

    read -p "Services jetzt starten? (j/N) " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Jj]$ ]]; then
        start_services
    fi

    print_next_steps

    echo -e "${GREEN}Setup abgeschlossen!${NC}"
}

# Skript ausführen
main "$@"

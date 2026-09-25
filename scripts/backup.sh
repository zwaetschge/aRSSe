#!/bin/bash
# ===========================================
# aRSSe Backup
# ===========================================
# Sichert den laufenden Stack, ohne ihn anzuhalten, nach
# BACKUP_DIR/<Datum_Uhrzeit>/:
#   miniflux.dump  Miniflux-Datenbank (pg_dump -Fc: Benutzer, Feeds, Artikel,
#                  Lesestatus); Wiederherstellen mit pg_restore
#   feeds.opml     Abonnements des Benutzers von MINIFLUX_API_KEY (lässt sich
#                  in jeden Feedreader importieren)
#   arsse.db       Story-Datenbank des Intelligence Layers (SQLite, konsistente
#                  Online-Sicherung)
#   config.yaml    eigene Einstellungen des Intelligence Layers (falls vorhanden)
#   env            Kopie der .env mit allen Passwörtern (Rechte 600)
# Sicherungen, die älter als BACKUP_KEEP_DAYS Tage sind (Standard 14), werden
# danach gelöscht. Wiederherstellen: README, Abschnitt "Backup und Updates".
#
# Aufruf: scripts/backup.sh [ZIELVERZEICHNIS]
#   ZIELVERZEICHNIS  Standard: BACKUP_DIR aus .env, sonst DATA_PATH/backups
#
# Weitere Compose-Dateien und Projektnamen übernimmt docker compose wie
# gewohnt aus COMPOSE_FILE und COMPOSE_PROJECT_NAME; ENV_FILE wählt eine
# andere .env.

set -euo pipefail
# Die Sicherungen enthalten Passwörter und alle Abonnements
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
WORK_DIR=""

info() { echo "[✓] $*"; }
warn() { echo "[!] $*" >&2; }
fail() { echo "[✗] $*" >&2; exit 1; }

usage() {
    echo "Aufruf: $0 [ZIELVERZEICHNIS]"
    echo "  Sichert Miniflux-Datenbank, OPML, Story-Datenbank, Einstellungen und .env"
}

# Wert aus .env lesen (letzte Zuweisung gewinnt, Anführungszeichen entfernt).
# Die Datei wird nicht mit 'source' ausgeführt: Compose-Syntax ist kein Bash.
env_get() {
    local value
    value=$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2-) || true
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    echo "$value"
}

compose() {
    docker compose --project-directory "$PROJECT_DIR" --env-file "$ENV_FILE" "$@"
}

running() {
    compose ps --status running --services 2>/dev/null | grep -qx "$1"
}

cleanup() {
    [ -z "$WORK_DIR" ] || rm -rf "$WORK_DIR"
}

# Python im Intelligence-Container als Besitzer des Datenverzeichnisses
# (PUID:PGID): Als root angelegte Dateien (-wal, -shm) könnte der Dienst
# danach nicht mehr öffnen
intelligence_python() {
    local owner
    owner=$(compose exec -T intelligence stat -c '%u:%g' /app/data | tr -d '\r')
    compose exec -T -u "$owner" intelligence python -
}

main() {
    case "${1:-}" in
        -h|--help) usage; exit 0 ;;
    esac
    [ -f "$ENV_FILE" ] || fail "$ENV_FILE fehlt (erst scripts/setup.sh ausführen)"
    command -v docker > /dev/null || fail "Docker ist nicht installiert"

    local data_path backup_dir keep stamp target postgres_user
    data_path=$(env_get DATA_PATH)
    data_path="${data_path:-./data}"
    [[ "$data_path" = /* ]] || data_path="$PROJECT_DIR/${data_path#./}"
    backup_dir="${1:-$(env_get BACKUP_DIR)}"
    backup_dir="${backup_dir:-$data_path/backups}"
    [[ "$backup_dir" = /* ]] || backup_dir="$PWD/$backup_dir"
    keep=$(env_get BACKUP_KEEP_DAYS)
    keep="${keep:-14}"
    [[ "$keep" =~ ^[0-9]+$ ]] || fail "BACKUP_KEEP_DAYS muss eine Zahl sein, ist '$keep'"
    postgres_user=$(env_get POSTGRES_USER)
    postgres_user="${postgres_user:-miniflux}"

    running db || fail "Der Datenbank-Container läuft nicht (docker compose up -d db)"

    mkdir -p "$backup_dir"
    stamp=$(date +%Y-%m-%d_%H%M%S)
    target="$backup_dir/$stamp"
    [ ! -e "$target" ] || fail "$target existiert bereits"
    # Erst am Ende umbenennen: Ein Abbruch hinterlässt keine halbe Sicherung
    WORK_DIR=$(mktemp -d "$backup_dir/.unfertig-XXXXXX")
    trap cleanup EXIT

    compose exec -T db pg_dump -U "$postgres_user" -Fc miniflux > "$WORK_DIR/miniflux.dump"
    [ -s "$WORK_DIR/miniflux.dump" ] || fail "pg_dump hat nichts geliefert"
    info "Miniflux-Datenbank gesichert ($(du -h "$WORK_DIR/miniflux.dump" | cut -f1))"

    if running intelligence; then
        # OPML über das Docker-Netzwerk, mit dem API-Key des Dienstes
        # (auch MINIFLUX_API_KEY_FILE); ohne Key wird sie übersprungen
        if intelligence_python > "$WORK_DIR/feeds.opml" <<'PY'
import logging, os, sys, urllib.request
logging.disable(logging.CRITICAL)
from config import DEFAULT_CONFIG_PATH, USER_CONFIG_PATH, load_config
config = load_config(os.getenv('ARSSE_CONFIG', DEFAULT_CONFIG_PATH),
                     os.getenv('ARSSE_USER_CONFIG', USER_CONFIG_PATH))
if not config.miniflux_api_key:
    sys.exit("MINIFLUX_API_KEY fehlt")
request = urllib.request.Request(config.miniflux_url.rstrip('/') + '/v1/export',
                                 headers={'X-Auth-Token': config.miniflux_api_key})
with urllib.request.urlopen(request, timeout=60) as response:
    sys.stdout.buffer.write(response.read())
PY
        then
            info "Abonnements als OPML gesichert"
        else
            rm -f "$WORK_DIR/feeds.opml"
            warn "OPML-Export fehlgeschlagen (die Abonnements stecken auch in miniflux.dump)"
        fi

        # SQLite-Online-Backup: konsistent, auch während ein Lauf schreibt
        intelligence_python > "$WORK_DIR/arsse.db" <<'PY'
import logging, os, sqlite3, sys
logging.disable(logging.CRITICAL)
from config import DEFAULT_CONFIG_PATH, USER_CONFIG_PATH, load_config
path = load_config(os.getenv('ARSSE_CONFIG', DEFAULT_CONFIG_PATH),
                   os.getenv('ARSSE_USER_CONFIG', USER_CONFIG_PATH)).storage.db_path
source = sqlite3.connect(path, timeout=30)
copy = sqlite3.connect(':memory:')
source.backup(copy)
sys.stdout.buffer.write(copy.serialize())
PY
        [ -s "$WORK_DIR/arsse.db" ] || fail "Story-Datenbank: Sicherung ist leer"
        info "Story-Datenbank gesichert ($(du -h "$WORK_DIR/arsse.db" | cut -f1))"

        if compose exec -T intelligence test -f /app/data/config.yaml; then
            compose exec -T intelligence cat /app/data/config.yaml > "$WORK_DIR/config.yaml"
            info "Eigene Einstellungen (config.yaml) gesichert"
        fi
    else
        warn "Intelligence Layer läuft nicht: Story-Datenbank und OPML übersprungen"
    fi

    cp "$ENV_FILE" "$WORK_DIR/env"
    chmod 600 "$WORK_DIR/env"
    info ".env gesichert (Datei env, Rechte 600)"

    chmod 700 "$WORK_DIR"
    mv "$WORK_DIR" "$target"
    WORK_DIR=""
    info "Sicherung: $target"

    # Rotation: nur fertige Sicherungen (Verzeichnisse mit Zeitstempel)
    find "$backup_dir" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]-*' \
        -mtime +"$keep" -print -exec rm -rf {} + | sed 's/^/[✓] Alte Sicherung gelöscht: /'
}

main "$@"

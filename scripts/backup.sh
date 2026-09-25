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
#   .arsse-backup  Markierung: nur so markierte Verzeichnisse rotiert das Skript
# miniflux.dump und env sind Pflicht; scheitert einer der anderen Teile, gibt
# es eine Warnung, und die Sicherung bleibt ohne ihn erhalten.
# Sicherungen, die älter als BACKUP_KEEP_DAYS Tage sind (Standard 14), werden
# danach gelöscht; andere Verzeichnisse in BACKUP_DIR bleiben unberührt.
# Wiederherstellen: README, Abschnitt "Backup und Updates".
#
# Aufruf: scripts/backup.sh [ZIELVERZEICHNIS]
#   ZIELVERZEICHNIS  Standard: BACKUP_DIR aus .env, sonst DATA_PATH/backups
#                    (relativ: das Argument zum aktuellen Verzeichnis, Pfade
#                    aus .env wie bei Compose zum Projektverzeichnis)
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
# Markiert fertige Sicherungen dieses Skripts; nur sie werden rotiert
MARKER=".arsse-backup"
# Name einer Sicherung (date +%Y-%m-%d_%H%M%S), als Muster für find
STAMP_GLOB='20[0-9][0-9]-[01][0-9]-[0-3][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]'

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

# Befehl in einem Container. stdin ist immer /dev/null: docker compose exec
# reicht stdin auch mit -T an den Befehl weiter, und der würde sonst
# verschlucken, was für einen späteren Befehl bestimmt ist
compose_exec() {
    compose exec -T "$@" < /dev/null
}

# Einstellungen des Dienstes laden, ohne sich auf Interna einer bestimmten
# Version zu verlassen: Direkt nach 'git pull' läuft oft noch das alte Image,
# dessen load_config() nur die Referenzdatei kennt
LOAD_CONFIG_PY=$(cat <<'PY'
import inspect, logging, os, sys
logging.disable(logging.CRITICAL)
import config as arsse_config
paths = [os.getenv('ARSSE_CONFIG', '/app/config.yaml'),
         os.getenv('ARSSE_USER_CONFIG', '/app/data/config.yaml'),
         os.getenv('ARSSE_LEGACY_CONFIG', '/app/legacy/config.yaml')]
accepted = len(inspect.signature(arsse_config.load_config).parameters)
config = arsse_config.load_config(*paths[:accepted])
PY
)

# OPML über das Docker-Netzwerk, mit dem API-Key des Dienstes (auch
# MINIFLUX_API_KEY_FILE); ohne Key schlägt der Export fehl
OPML_PY=$(cat <<'PY'
import urllib.request
if not config.miniflux_api_key:
    sys.exit("MINIFLUX_API_KEY fehlt")
request = urllib.request.Request(config.miniflux_url.rstrip('/') + '/v1/export',
                                 headers={'X-Auth-Token': config.miniflux_api_key})
with urllib.request.urlopen(request, timeout=60) as response:
    sys.stdout.buffer.write(response.read())
PY
)

# SQLite-Online-Backup: konsistent, auch während ein Lauf schreibt
SQLITE_PY=$(cat <<'PY'
import sqlite3
source = sqlite3.connect(config.storage.db_path, timeout=30)
copy = sqlite3.connect(':memory:')
source.backup(copy)
sys.stdout.buffer.write(copy.serialize())
PY
)

# Python-Code (als Argument) im Intelligence-Container als Besitzer des
# Datenverzeichnisses (PUID:PGID): Als root angelegte Dateien (-wal, -shm)
# könnte der Dienst danach nicht mehr öffnen
intelligence_python() {
    local owner
    owner=$(compose_exec intelligence stat -c '%u:%g' /app/data | tr -d '\r')
    [ -n "$owner" ] || return 1
    compose_exec -u "$owner" intelligence python -c "$LOAD_CONFIG_PY
$1"
}

main() {
    case "${1:-}" in
        -h|--help) usage; exit 0 ;;
    esac
    [ -f "$ENV_FILE" ] || fail "$ENV_FILE fehlt (erst scripts/setup.sh ausführen)"
    command -v docker > /dev/null || fail "Docker ist nicht installiert"

    local data_path backup_dir keep stamp target postgres_user old
    data_path=$(env_get DATA_PATH)
    data_path="${data_path:-./data}"
    [[ "$data_path" = /* ]] || data_path="$PROJECT_DIR/${data_path#./}"
    if [ -n "${1:-}" ]; then
        backup_dir="$1"
        [[ "$backup_dir" = /* ]] || backup_dir="$PWD/${backup_dir#./}"
    else
        # Wie DATA_PATH relativ zum Projektverzeichnis: Geplante Läufe (User
        # Scripts, cron) starten in / oder $HOME, auf Unraid liegt / im RAM
        backup_dir=$(env_get BACKUP_DIR)
        backup_dir="${backup_dir:-$data_path/backups}"
        [[ "$backup_dir" = /* ]] || backup_dir="$PROJECT_DIR/${backup_dir#./}"
    fi
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

    compose_exec db pg_dump -U "$postgres_user" -Fc miniflux > "$WORK_DIR/miniflux.dump"
    [ -s "$WORK_DIR/miniflux.dump" ] || fail "pg_dump hat nichts geliefert"
    info "Miniflux-Datenbank gesichert ($(du -h "$WORK_DIR/miniflux.dump" | cut -f1))"

    # Die folgenden Teile sind verzichtbar: Schlägt einer fehl, bleibt die
    # Sicherung mit miniflux.dump und env trotzdem erhalten
    if running intelligence; then
        if intelligence_python "$OPML_PY" > "$WORK_DIR/feeds.opml" \
                && [ -s "$WORK_DIR/feeds.opml" ]; then
            info "Abonnements als OPML gesichert"
        else
            rm -f "$WORK_DIR/feeds.opml"
            warn "OPML-Export fehlgeschlagen (die Abonnements stecken auch in miniflux.dump)"
        fi

        if intelligence_python "$SQLITE_PY" > "$WORK_DIR/arsse.db" \
                && [ -s "$WORK_DIR/arsse.db" ]; then
            info "Story-Datenbank gesichert ($(du -h "$WORK_DIR/arsse.db" | cut -f1))"
        else
            rm -f "$WORK_DIR/arsse.db"
            warn "Story-Datenbank nicht gesichert (verzichtbar: ohne sie baut der" \
                "nächste Lauf die Stories neu auf)"
        fi

        if compose_exec intelligence test -f /app/data/config.yaml; then
            if compose_exec intelligence cat /app/data/config.yaml > "$WORK_DIR/config.yaml"; then
                info "Eigene Einstellungen (config.yaml) gesichert"
            else
                rm -f "$WORK_DIR/config.yaml"
                warn "Eigene Einstellungen (config.yaml) nicht gesichert"
            fi
        fi
    else
        warn "Intelligence Layer läuft nicht: Story-Datenbank und OPML übersprungen"
    fi

    cp "$ENV_FILE" "$WORK_DIR/env"
    chmod 600 "$WORK_DIR/env"
    info ".env gesichert (Datei env, Rechte 600)"

    echo "aRSSe-Sicherung $stamp (scripts/backup.sh)" > "$WORK_DIR/$MARKER"
    chmod 700 "$WORK_DIR"
    mv "$WORK_DIR" "$target"
    WORK_DIR=""
    info "Sicherung: $target"

    # Rotation: nur fertige Sicherungen dieses Skripts (Name mit genau
    # diesem Zeitstempel und Markierung). BACKUP_DIR kann eine gemeinsame
    # Freigabe sein, deren andere Ordner (z.B. '2020-05-01@03.00' vom
    # Appdata-Backup) nicht angetastet werden dürfen
    while IFS= read -r -d '' old; do
        [ -f "$old/$MARKER" ] || continue
        rm -rf "$old"
        info "Alte Sicherung gelöscht: $old"
    done < <(find "$backup_dir" -mindepth 1 -maxdepth 1 -type d -name "$STAMP_GLOB" \
                -mtime +"$keep" -print0)
}

main "$@"

#!/bin/bash
# ===========================================
# aRSSe Test für scripts/backup.sh
# ===========================================
# Führt backup.sh mit einem Docker-Stub aus (kein Docker nötig): pg_dump
# liefert Testdaten, die Python-Teile laufen lokal gegen eine echte
# Story-Datenbank und einen kleinen HTTP-Server als Miniflux. Geprüft werden
# Dateien, Rechte, die Rotation und Abbrüche. Braucht Python mit den
# Abhängigkeiten des Intelligence Layers (PYTHON, Standard python3).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PYTHON="${PYTHON:-python3}"
SERVER_PID=""
cleanup() {
    [ -z "$SERVER_PID" ] || kill "$SERVER_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

FAILURES=0
fail() { echo "    FEHLER: $*"; FAILURES=$((FAILURES + 1)); }
step() { echo "==> $*"; }

PROJ="$WORK/project"
DATA="$WORK/data"
mkdir -p "$PROJ/scripts" "$DATA/intelligence" "$WORK/stubs" "$WORK/miniflux/v1"
cp "$ROOT/scripts/backup.sh" "$PROJ/scripts/"
cat > "$PROJ/.env" <<ENV
POSTGRES_USER=leser_db
DATA_PATH=$DATA
MINIFLUX_API_KEY=wird-im-container-gelesen
ENV
chmod 644 "$PROJ/.env"

# Story-Datenbank und eigene Einstellungen wie im Container
"$PYTHON" -c "import sys; sys.path.insert(0, '$ROOT/intelligence'); \
from store import StoryStore; StoryStore('$DATA/intelligence/arsse.db').set_meta('probe', 42)"
cat > "$DATA/intelligence/config.yaml" <<YAML
web:
  min_sources: 3
YAML
# Die Python-Teile von backup.sh lesen diese Einstellungen (ARSSE_USER_CONFIG)
cat > "$WORK/container.yaml" <<YAML
storage:
  db_path: "$DATA/intelligence/arsse.db"
YAML
echo '<?xml version="1.0"?><opml version="2.0"><body/></opml>' > "$WORK/miniflux/v1/export"

# Miniflux-Ersatz für den OPML-Export
PORT=$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
"$PYTHON" -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/miniflux" \
    > /dev/null 2>&1 &
SERVER_PID=$!

cat > "$WORK/stubs/docker" <<'EOF'
#!/bin/bash
# docker compose --project-directory X --env-file Y <Befehl> ...
echo "docker $*" >> "$DOCKER_LOG"
[ "$1" = compose ] || exit 1
shift
while [[ "$1" == --* ]]; do shift 2; done
case "$1" in
    ps) printf '%s\n' $STUB_RUNNING ;;
    exec)
        shift
        [ "$1" = -T ] && shift
        [ "$1" = -u ] && shift 2
        service="$1"; shift
        # Wie docker compose exec: stdin geht an jeden Befehl, auch mit -T.
        # Nur 'python -' liest daraus sein Skript
        [ "$service $*" = "intelligence python -" ] || cat > /dev/null
        case "$service $*" in
            "db pg_dump -U leser_db -Fc miniflux") printf 'PGDMP-testdaten' ;;
            "db "*) exit 1 ;;
            "intelligence stat -c %u:%g /app/data") echo "$(id -u):$(id -g)" ;;
            "intelligence python -"|"intelligence python -c "*)
                cd "$INTELLIGENCE_DIR" && exec env ARSSE_CONFIG=/nonexistent \
                    ARSSE_USER_CONFIG="$STUB_CONFIG" MINIFLUX_URL="$STUB_MINIFLUX" \
                    ARSSE_LEGACY_CONFIG=/nonexistent STUB_DB="$STUB_DATA/arsse.db" \
                    MINIFLUX_API_KEY="$STUB_KEY" "$PYTHON" "${@:2}" ;;
            "intelligence test -f /app/data/config.yaml") test -f "$STUB_DATA/config.yaml" ;;
            "intelligence cat /app/data/config.yaml") cat "$STUB_DATA/config.yaml" ;;
            *) echo "unerwartet: $service $*" >&2; exit 1 ;;
        esac ;;
    *) exit 1 ;;
esac
EOF
chmod +x "$WORK/stubs/docker"
export DOCKER_LOG="$WORK/docker.log" INTELLIGENCE_DIR="$ROOT/intelligence" PYTHON \
    STUB_CONFIG="$WORK/container.yaml" STUB_MINIFLUX="http://127.0.0.1:$PORT" \
    STUB_KEY="test-key" STUB_DATA="$DATA/intelligence" STUB_RUNNING="db intelligence"
STUB_PATH="$WORK/stubs:$PATH"

run_backup() {
    set +e
    OUT=$(cd "$WORK" && PATH="$STUB_PATH" bash "$PROJ/scripts/backup.sh" "$@" 2>&1 < /dev/null)
    STATUS=$?
    set -e
}

latest_backup() { find "$DATA/backups" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -n 1; }

# Warten, bis der HTTP-Server antwortet
for _ in $(seq 50); do
    "$PYTHON" -c "import urllib.request; urllib.request.urlopen('$STUB_MINIFLUX/v1/export')" \
        2> /dev/null && break
    sleep 0.1
done

step "Vollständige Sicherung"
# Alte eigene Sicherung (markiert) und alte fremde Ordner, z.B. vom
# Appdata-Backup in einer gemeinsamen Freigabe
FOREIGN=("eigene-dateien" "2020-05-01@03.00" "2021-dokumente" "2020-01-02_000000")
for dir in 2020-01-01_000000 "${FOREIGN[@]}"; do
    mkdir -p "$DATA/backups/$dir"
    echo wichtig > "$DATA/backups/$dir/daten.tar"
done
echo "aRSSe-Sicherung" > "$DATA/backups/2020-01-01_000000/.arsse-backup"
touch -d '30 days ago' "$DATA/backups/"*
run_backup
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
B=$(latest_backup)
[ "$(cat "$B/miniflux.dump" 2>/dev/null)" = "PGDMP-testdaten" ] || fail "miniflux.dump fehlt oder falsch"
grep -q '<opml' "$B/feeds.opml" 2>/dev/null || fail "feeds.opml fehlt"
CHECK=$("$PYTHON" -c "import sqlite3; c = sqlite3.connect('$B/arsse.db'); \
print(c.execute('PRAGMA integrity_check').fetchone()[0], \
c.execute(\"SELECT value FROM meta WHERE key = 'probe'\").fetchone()[0])" 2>&1) || true
[ "$CHECK" = "ok 42" ] || fail "arsse.db nicht lesbar oder unvollständig: $CHECK"
grep -q "min_sources: 3" "$B/config.yaml" 2>/dev/null || fail "config.yaml fehlt"
cmp -s "$PROJ/.env" "$B/env" || fail "env ist keine Kopie der .env"
[ "$(stat -c %a "$B/env")" = 600 ] || fail "env hat Rechte $(stat -c %a "$B/env")"
[ "$(stat -c %a "$B")" = 700 ] || fail "Sicherungsverzeichnis hat Rechte $(stat -c %a "$B")"
[ ! -e "$DATA/backups/2020-01-01_000000" ] || fail "alte Sicherung nicht gelöscht"
for dir in "${FOREIGN[@]}"; do
    [ -f "$DATA/backups/$dir/daten.tar" ] || fail "fremdes Verzeichnis $dir gelöscht"
done
[ -f "$B/.arsse-backup" ] || fail "Markierung .arsse-backup fehlt"
grep -q "Alte Sicherung gelöscht" <<< "$OUT" || fail "Rotation nicht gemeldet"
grep -q "exec -T -u $(id -u):$(id -g) intelligence python" "$DOCKER_LOG" \
    || fail "Python lief nicht als Besitzer des Datenverzeichnisses"

step "Zielverzeichnis als Argument, ohne API-Key"
STUB_KEY="" run_backup "$WORK/ziel"
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
B=$(find "$WORK/ziel" -mindepth 1 -maxdepth 1 -type d -name '20*' | head -n 1)
[ -s "$B/arsse.db" ] || fail "arsse.db fehlt im Zielverzeichnis"
[ ! -e "$B/feeds.opml" ] || fail "leere feeds.opml angelegt"
grep -q "OPML-Export fehlgeschlagen" <<< "$OUT" || fail "Warnung zum OPML-Export fehlt"

step "Story-Datenbank nicht lesbar: Rest der Sicherung bleibt"
rm -rf "$WORK/ziel"
cat > "$WORK/broken.yaml" <<YAML
storage:
  db_path: "$WORK/fehlt/arsse.db"
YAML
STUB_CONFIG="$WORK/broken.yaml" run_backup "$WORK/ziel"
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
B=$(find "$WORK/ziel" -mindepth 1 -maxdepth 1 -type d -name '20*' | head -n 1)
[ -s "$B/miniflux.dump" ] || fail "miniflux.dump fehlt"
[ -f "$B/env" ] || fail "env fehlt"
grep -q '<opml' "$B/feeds.opml" 2>/dev/null || fail "feeds.opml fehlt"
[ ! -e "$B/arsse.db" ] || fail "unbrauchbare arsse.db angelegt"
grep -q "Story-Datenbank nicht gesichert" <<< "$OUT" || fail "Warnung zur Story-Datenbank fehlt"

step "Leerer OPML-Export gilt nicht als gesichert"
rm -rf "$WORK/ziel"
cp "$WORK/miniflux/v1/export" "$WORK/export.opml"
: > "$WORK/miniflux/v1/export"
run_backup "$WORK/ziel"
cp "$WORK/export.opml" "$WORK/miniflux/v1/export"
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
B=$(find "$WORK/ziel" -mindepth 1 -maxdepth 1 -type d -name '20*' | head -n 1)
[ ! -e "$B/feeds.opml" ] || fail "leere feeds.opml angelegt"
grep -q "OPML-Export fehlgeschlagen" <<< "$OUT" || fail "Warnung zum leeren OPML-Export fehlt"

step "Älteres Image (load_config kennt nur die Referenzdatei)"
# Direkt nach 'git pull' läuft noch der Container der Vorversion
rm -rf "$WORK/ziel"
mkdir -p "$WORK/altes-image"
cat > "$WORK/altes-image/config.py" <<'PY'
import os
from types import SimpleNamespace


def load_config(config_path=None):
    return SimpleNamespace(miniflux_url=os.environ['MINIFLUX_URL'],
                           miniflux_api_key=os.environ['MINIFLUX_API_KEY'],
                           storage=SimpleNamespace(db_path=os.environ['STUB_DB']))
PY
INTELLIGENCE_DIR="$WORK/altes-image" run_backup "$WORK/ziel"
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
B=$(find "$WORK/ziel" -mindepth 1 -maxdepth 1 -type d -name '20*' | head -n 1)
[ -s "$B/miniflux.dump" ] || fail "miniflux.dump fehlt"
grep -q '<opml' "$B/feeds.opml" 2>/dev/null || fail "feeds.opml fehlt"
[ -s "$B/arsse.db" ] || fail "arsse.db fehlt"

step "Intelligence Layer läuft nicht"
rm -rf "$WORK/ziel"
STUB_RUNNING="db" run_backup "$WORK/ziel"
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
B=$(find "$WORK/ziel" -mindepth 1 -maxdepth 1 -type d -name '20*' | head -n 1)
[ -s "$B/miniflux.dump" ] || fail "miniflux.dump fehlt"
[ -f "$B/env" ] || fail "env fehlt"
[ ! -e "$B/arsse.db" ] || fail "arsse.db ohne laufenden Container"
grep -q "Intelligence Layer läuft nicht" <<< "$OUT" || fail "Hinweis fehlt"

step "Datenbank läuft nicht: Abbruch ohne halbe Sicherung"
rm -rf "$WORK/ziel"
STUB_RUNNING="intelligence" run_backup "$WORK/ziel"
[ "$STATUS" -ne 0 ] || fail "backup.sh hätte abbrechen müssen"
[ -z "$(ls -A "$WORK/ziel" 2>/dev/null)" ] || fail "Reste im Zielverzeichnis: $(ls -A "$WORK/ziel")"

step "Relatives BACKUP_DIR aus .env gilt ab dem Projektverzeichnis"
# Geplante Läufe starten in einem anderen Verzeichnis (run_backup: $WORK)
cp "$PROJ/.env" "$WORK/env.orig"
echo "BACKUP_DIR=./sicherungen" >> "$PROJ/.env"
run_backup
cp "$WORK/env.orig" "$PROJ/.env"
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
[ -n "$(find "$PROJ/sicherungen" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null)" ] \
    || fail "keine Sicherung in $PROJ/sicherungen"
[ ! -e "$WORK/sicherungen" ] || fail "Sicherung im aktuellen Verzeichnis statt im Projekt"

step "Relatives Argument gilt ab dem aktuellen Verzeichnis"
run_backup ./ziel-relativ
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS: $OUT"
[ -n "$(find "$WORK/ziel-relativ" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null)" ] \
    || fail "keine Sicherung in $WORK/ziel-relativ"

step "Fehlerhafte Rotation"
echo "BACKUP_KEEP_DAYS=zwei" >> "$PROJ/.env"
run_backup "$WORK/ziel"
[ "$STATUS" -ne 0 ] || fail "BACKUP_KEEP_DAYS=zwei hätte abbrechen müssen"

if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES Fehler"
    exit 1
fi
echo "Alle Tests bestanden"

#!/bin/bash
# ===========================================
# aRSSe Integrationstest
# ===========================================
# Startet den kompletten Stack mit einem echten Miniflux, abonniert
# synthetische Feeds als Benutzer ohne Admin-Rechte und prüft, dass Stories
# entstehen, Duplikate in Miniflux als gelesen markiert werden, die
# Startseite absolute Uhrzeiten und die Dateien für den Startbildschirm
# liefert und die Absicherung greift (Passwortschutz, Sicherheits-Header,
# /metrics aus, fremde Host-Header, schreibgeschützte Container ohne
# Berechtigungen). Prüft außerdem die Fehleranzeige bei falschem API-Key
# und scripts/backup.sh. Kollidiert nicht mit einem laufenden
# Produktions-Stack (eigene Namen, Ports und Datenverzeichnisse).
#
# Varianten (Umgebungsvariablen):
#   IT_PUID/IT_PGID    Besitzer der Intelligence-Daten (Standard: aktueller Benutzer)
#   IT_PRECREATE_DATA  1 = data/intelligence vorab anlegen (Standard),
#                      0 = Docker legt es als root an, der Container übernimmt es
#   IT_LEGACY_USER     1 = Intelligence mit "user: PUID:PGID" starten wie ältere
#                      Compose-Dateien (braucht IT_PRECREATE_DATA=1)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
MF_PORT="${MF_PORT:-18080}"
IT_PORT="${IT_PORT:-18081}"
ADMIN_PASSWORD="it-$(date +%s)-secret"
READER_PASSWORD="it-reader-$(date +%s)"
WEB_PASSWORD="it-web-secret"
API="http://localhost:$MF_PORT/v1"
PUID="${IT_PUID:-$(id -u)}"
PGID="${IT_PGID:-$(id -g)}"
PRECREATE_DATA="${IT_PRECREATE_DATA:-1}"
LEGACY_USER="${IT_LEGACY_USER:-0}"

COMPOSE_FILES=(-f "$ROOT/docker-compose.yml" -f "$ROOT/tests/integration/compose.override.yml")
# The same files for scripts/backup.sh, which docker compose reads from COMPOSE_FILE
COMPOSE_FILE_LIST="$ROOT/docker-compose.yml:$ROOT/tests/integration/compose.override.yml"
if [ "$LEGACY_USER" = 1 ]; then
    COMPOSE_FILES+=(-f "$WORK/legacy-user.yml")
    COMPOSE_FILE_LIST+=":$WORK/legacy-user.yml"
fi

compose() {
    docker compose -p arsse-it --env-file "$WORK/.env" "${COMPOSE_FILES[@]}" "$@"
}

cleanup() {
    local status=$?
    if [ $status -ne 0 ]; then
        echo "::group::Container logs"
        compose logs --no-color --tail 200 || true
        echo "::endgroup::"
    fi
    compose down -v --remove-orphans > /dev/null 2>&1 || true
    # Postgres data belongs to the container's postgres user, not to us
    docker run --rm -v "$WORK:/work" alpine:3 rm -rf /work/data > /dev/null 2>&1 || true
    rm -rf "$WORK" || true
    exit $status
}
trap cleanup EXIT

json() {
    # json <python expression on variable d>
    python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"
}

# wait_for_health <python condition on d = /healthz body> <description>
wait_for_health() {
    local body=""
    for _ in $(seq 60); do
        body=$(curl -sS "http://localhost:$IT_PORT/healthz" 2>/dev/null) || true
        if [ -n "$body" ] && json "bool($1)" <<< "$body" 2>/dev/null | grep -qx True; then
            return 0
        fi
        sleep 2
    done
    echo "Timed out waiting for $2; last /healthz: $body"
    exit 1
}

step() { echo "==> $*"; }

step "Preparing feeds and environment in $WORK (PUID=$PUID PGID=$PGID," \
    "pre-created data dir: $PRECREATE_DATA, legacy user: $LEGACY_USER)"
python3 "$ROOT/tests/integration/make_feeds.py" "$WORK/feeds"
if [ "$PRECREATE_DATA" = 1 ]; then
    mkdir -p "$WORK/data/intelligence"
fi
if [ "$LEGACY_USER" = 1 ]; then
    # Older compose files ran the container unprivileged from the start
    printf 'services:\n  intelligence:\n    user: "%s:%s"\n' "$PUID" "$PGID" > "$WORK/legacy-user.yml"
fi
cat > "$WORK/.env" <<ENV
POSTGRES_PASSWORD=it-db-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
MINIFLUX_PORT=$MF_PORT
INTELLIGENCE_PORT=$IT_PORT
BASE_URL=http://localhost:$MF_PORT
DATA_PATH=$WORK/data
PUID=$PUID
PGID=$PGID
FEEDS_DIR=$WORK/feeds
MINIFLUX_API_KEY=
ENV

step "Starting database, Miniflux and feed server"
compose up -d --build --wait --wait-timeout 180 db miniflux feeds

step "Creating a reading user without admin rights and its API key (README)"
curl -fsS -u "admin:$ADMIN_PASSWORD" -H 'Content-Type: application/json' \
    -d "{\"username\": \"leser\", \"password\": \"$READER_PASSWORD\"}" \
    "$API/users" > /dev/null
API_KEY=$(curl -fsS -u "leser:$READER_PASSWORD" -H 'Content-Type: application/json' \
    -d '{"description": "integration test"}' "$API/api-keys" | json 'd["token"]')
CATEGORY=$(curl -fsS -H "X-Auth-Token: $API_KEY" "$API/categories" | json 'd[0]["id"]')

step "Subscribing to test feeds"
for feed in alpha beta gamma; do
    curl -fsS -H "X-Auth-Token: $API_KEY" -H 'Content-Type: application/json' \
        -d "{\"feed_url\": \"http://feeds:8000/$feed.xml\", \"category_id\": $CATEGORY}" \
        "$API/feeds" > /dev/null
done
TOTAL=$(curl -fsS -H "X-Auth-Token: $API_KEY" "$API/entries?limit=1" | json 'd["total"]')
echo "    $TOTAL entries in Miniflux"
[ "$TOTAL" -eq 7 ] || { echo "Expected 7 entries, got $TOTAL"; exit 1; }

step "Starting intelligence layer"
sed -i "s|^MINIFLUX_API_KEY=.*|MINIFLUX_API_KEY=$API_KEY|" "$WORK/.env"
compose up -d --build --wait --wait-timeout 300 intelligence

step "Checking results"
# /healthz answers 'starting' (200) until the first run succeeded
wait_for_health 'd["status"] == "ok"' "the first clustering run"
curl -fsS "http://localhost:$IT_PORT/healthz" | json 'd["last_stats"]'
STORIES=$(curl -fsS "http://localhost:$IT_PORT/api/stories")
echo "$STORIES" | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    print("    %d sources: %s" % (s["source_count"], s["headline"]["title"]))
'

echo "$STORIES" | python3 -c '
import json, sys
stories = json.load(sys.stdin)
titles = sorted(sorted(a["title"] for a in s["articles"]) for s in stories)
expected = [
    ["Bundestag beschließt Haushalt", "Bundestag beschließt Haushalt", "Haushalt: Bundestag stimmt zu"],
    ["Chiphersteller zeigt neuen Prozessor", "Neuer Prozessor vorgestellt"],
]
assert titles == expected, f"unexpected stories: {titles}"
'

READ=$(curl -fsS -H "X-Auth-Token: $API_KEY" "$API/entries?status=read" | json 'd["total"]')
echo "    $READ entries marked read in Miniflux"
[ "$READ" -eq 1 ] || { echo "Expected exactly 1 duplicate marked read, got $READ"; exit 1; }

HTML=$(curl -fsS "http://localhost:$IT_PORT/")
grep -q "Bundestag beschließt Haushalt" <<< "$HTML"

# BASE_URL is localhost here: links must follow the host the browser used
grep -q "href=\"http://localhost:$MF_PORT/feed/" <<< "$HTML" \
    || { echo "Links do not point to Miniflux on port $MF_PORT"; exit 1; }
HTML=$(curl -fsS -H "Host: 192.0.2.10:$IT_PORT" "http://localhost:$IT_PORT/")
grep -q "href=\"http://192.0.2.10:$MF_PORT/feed/" <<< "$HTML" \
    || { echo "Links do not follow the request host"; exit 1; }

OWNER=$(stat -c %u:%g "$WORK/data/intelligence/arsse.db")
echo "    arsse.db owned by $OWNER"
[ "$OWNER" = "$PUID:$PGID" ] || { echo "Expected arsse.db owned by $PUID:$PGID, got $OWNER"; exit 1; }
# Template for the user's own settings, created on the first start
OWNER=$(stat -c %u:%g "$WORK/data/intelligence/config.yaml")
[ "$OWNER" = "$PUID:$PGID" ] || { echo "Expected config.yaml owned by $PUID:$PGID, got $OWNER"; exit 1; }

step "Checking the hardened intelligence container"
CONTAINER=$(compose ps -q intelligence)
docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.Memory}}' \
    "$CONTAINER" | tee /dev/stderr | grep -qiE '^true \[(cap_)?all\] 805306368$' \
    || { echo "intelligence is not read-only, without capabilities and limited to 768 MB"; exit 1; }
if compose exec -T intelligence sh -c 'echo x > /app/web.py' 2>/dev/null; then
    echo "The code in /app is writable"; exit 1
fi
STATUS=$(compose exec -T intelligence cat /proc/1/status | tr -d '\r')
PROC_IDS=$(awk '/^Uid:/ {u = $2} /^Gid:/ {g = $2} END {print u ":" g}' <<< "$STATUS")
echo "    service runs as $PROC_IDS"
[ "$PROC_IDS" = "$PUID:$PGID" ] || { echo "Expected the service to run as $PUID:$PGID"; exit 1; }
CAPS=$(awk '/^CapEff:/ {print $2}' <<< "$STATUS")
[ "$CAPS" = 0000000000000000 ] || { echo "The service keeps capabilities: $CAPS"; exit 1; }
docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$(compose ps -q miniflux)" | grep -qx true \
    || { echo "Miniflux is not read-only"; exit 1; }

step "Checking security defaults"
http_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
HEADERS=$(curl -fsS -D - -o /dev/null "http://localhost:$IT_PORT/")
grep -qi "^content-security-policy: default-src 'none'" <<< "$HEADERS" \
    || { echo "Top Stories send no Content-Security-Policy"; exit 1; }
LOGS=$(compose logs --no-color intelligence)
grep -q "web.auth.mode=none" <<< "$LOGS" || { echo "No warning about the open Top Stories"; exit 1; }
if grep -q "belongs to the admin user" <<< "$LOGS"; then
    echo "Admin warning although the API key belongs to a normal user"; exit 1
fi
CODE=$(http_code "http://localhost:$MF_PORT/metrics")
echo "    Miniflux /metrics: $CODE"
[ "$CODE" != 200 ] || { echo "Miniflux serves /metrics by default"; exit 1; }

step "Checking the E-Ink front page and home-screen files"
HTML=$(curl -fsS "http://localhost:$IT_PORT/")
# Absolute times need the time zone data in the image (tzdata)
grep -q 'Stand <time datetime="' <<< "$HTML" \
    || { echo "No absolute time ('Stand HH:MM') on the front page"; exit 1; }
if grep -q "is not a time zone name" <<< "$LOGS"; then
    echo "TZ=Europe/Berlin not found in the image"; exit 1
fi
if grep -q "<script" <<< "$HTML"; then echo "The front page contains a script"; exit 1; fi
CODE=$(http_code "http://localhost:$IT_PORT/?seite=999")
[ "$CODE" = 404 ] || { echo "Expected 404 for a page beyond the last, got $CODE"; exit 1; }
curl -fsS "http://localhost:$IT_PORT/static/manifest.webmanifest" \
    | json 'd["start_url"]' | grep -qx '/' \
    || { echo "Web app manifest missing or without start_url /"; exit 1; }
for FILE in static/icon-192.png static/icon-512.png static/icon.svg favicon.ico; do
    CODE=$(http_code "http://localhost:$IT_PORT/$FILE")
    [ "$CODE" = 200 ] || { echo "Expected 200 for /$FILE, got $CODE"; exit 1; }
done

step "Marking a story read ('Story gelesen')"
CHIP=$(echo "$STORIES" | python3 -c '
import json, sys
print(next(s["id"] for s in json.load(sys.stdin) if "Prozessor" in s["headline"]["title"]))
')
STORY_URL="http://localhost:$IT_PORT/story/$CHIP/gelesen"
CODE=$(http_code -d '' -H 'Sec-Fetch-Site: cross-site' "$STORY_URL")
[ "$CODE" = 403 ] || { echo "Expected 403 for a cross-site POST, got $CODE"; exit 1; }
RESULT=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' -d 'next=//evil.example' \
    -H 'Sec-Fetch-Site: same-origin' "$STORY_URL")
echo "    POST $STORY_URL: $RESULT"
[ "$RESULT" = "303 http://localhost:$IT_PORT/" ] \
    || { echo "Expected 303 to / (never to another site), got $RESULT"; exit 1; }
READ=$(curl -fsS -H "X-Auth-Token: $API_KEY" "$API/entries?status=read" | json 'd["total"]')
[ "$READ" -eq 3 ] || { echo "Expected the duplicate and both chip articles read, got $READ"; exit 1; }
curl -fsS "http://localhost:$IT_PORT/api/stories" | python3 -c '
import json, sys
titles = [s["headline"]["title"] for s in json.load(sys.stdin)]
assert not any("Prozessor" in t for t in titles), f"read story still listed: {titles}"
'
curl -fsS "http://localhost:$IT_PORT/api/stories?alle=1" | grep -q "Prozessor" \
    || { echo "The read story is missing from ?alle=1"; exit 1; }

step "Backing up with scripts/backup.sh"
COMPOSE_PROJECT_NAME=arsse-it COMPOSE_FILE="$COMPOSE_FILE_LIST" ENV_FILE="$WORK/.env" \
    "$ROOT/scripts/backup.sh" "$WORK/backup"
BACKUP=$(find "$WORK/backup" -mindepth 1 -maxdepth 1 -type d -name '20*' | head -n 1)
[ -n "$BACKUP" ] || { echo "No backup directory"; exit 1; }
[ "$(head -c 5 "$BACKUP/miniflux.dump")" = PGDMP ] || { echo "miniflux.dump is no pg_dump archive"; exit 1; }
grep -q "http://feeds:8000/alpha.xml" "$BACKUP/feeds.opml" || { echo "feeds.opml lacks the feeds"; exit 1; }
python3 - "$BACKUP/arsse.db" <<'PY' || { echo "arsse.db backup unusable"; exit 1; }
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
assert conn.execute('SELECT COUNT(*) FROM stories').fetchone()[0] > 0
PY
[ "$(stat -c %a "$BACKUP/env")" = 600 ] || { echo "env backup is not mode 600"; exit 1; }
cmp -s "$WORK/.env" "$BACKUP/env" || { echo "env backup differs from .env"; exit 1; }
echo "    $(find "$BACKUP" -mindepth 1 -printf '%f ')"

step "Showing why clustering fails (rejected API key)"
sed -i "s|^MINIFLUX_API_KEY=.*|MINIFLUX_API_KEY=falsch|" "$WORK/.env"
compose up -d intelligence
wait_for_health '(d["last_error"] or {}).get("message") == "Miniflux lehnt den API-Key ab"' \
    "the error of the rejected API key"
curl -fsS "http://localhost:$IT_PORT/" | grep -q "Fehler seit" \
    || { echo "The front page does not show the error"; exit 1; }
sed -i "s|^MINIFLUX_API_KEY=.*|MINIFLUX_API_KEY=$API_KEY|" "$WORK/.env"

step "Protecting Top Stories with a password (WEB_AUTH_MODE=basic)"
cat >> "$WORK/.env" <<ENV
WEB_AUTH_MODE=basic
WEB_USERNAME=leser
WEB_PASSWORD=$WEB_PASSWORD
WEB_ALLOWED_HOSTS=localhost
ENV
# Recreated with the new environment; healthy only if /healthz stays open
compose up -d --wait --wait-timeout 300 intelligence
CODE=$(http_code "http://localhost:$IT_PORT/api/stories")
[ "$CODE" = 401 ] || { echo "Expected 401 without credentials, got $CODE"; exit 1; }
CODE=$(http_code -u "leser:falsch" "http://localhost:$IT_PORT/")
[ "$CODE" = 401 ] || { echo "Expected 401 for a wrong password, got $CODE"; exit 1; }
HTML=$(curl -fsS -u "leser:$WEB_PASSWORD" "http://localhost:$IT_PORT/")
grep -q "Bundestag beschließt Haushalt" <<< "$HTML" \
    || { echo "Top Stories not shown with the right password"; exit 1; }
curl -fsS "http://localhost:$IT_PORT/healthz" > /dev/null \
    || { echo "/healthz must stay reachable without credentials"; exit 1; }
# Android fetches the icons without credentials; the pages stay protected
curl -fsS "http://localhost:$IT_PORT/static/manifest.webmanifest" > /dev/null \
    || { echo "The web app manifest must be reachable without credentials"; exit 1; }
CODE=$(http_code "http://localhost:$IT_PORT/?seite=1")
[ "$CODE" = 401 ] || { echo "Expected 401 for ?seite=1 without credentials, got $CODE"; exit 1; }
CODE=$(http_code -u "leser:$WEB_PASSWORD" -H "Host: rebind.example:$IT_PORT" "http://localhost:$IT_PORT/")
[ "$CODE" = 400 ] || { echo "Expected 400 for a foreign Host header, got $CODE"; exit 1; }
# The good key is back: the next run clears the error
wait_for_health 'd["last_error"] is None and d["status"] == "ok"' "the error to clear"

step "Integration test passed"

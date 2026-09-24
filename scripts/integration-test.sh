#!/bin/bash
# ===========================================
# aRSSe Integrationstest
# ===========================================
# Startet den kompletten Stack mit einem echten Miniflux, abonniert
# synthetische Feeds und prüft, dass Stories entstehen und Duplikate in
# Miniflux als gelesen markiert werden. Kollidiert nicht mit einem
# laufenden Produktions-Stack (eigene Namen, Ports und Datenverzeichnisse).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
MF_PORT="${MF_PORT:-18080}"
IT_PORT="${IT_PORT:-18081}"
ADMIN_PASSWORD="it-$(date +%s)-secret"
API="http://localhost:$MF_PORT/v1"

compose() {
    docker compose -p arsse-it --env-file "$WORK/.env" \
        -f "$ROOT/docker-compose.yml" \
        -f "$ROOT/tests/integration/compose.override.yml" "$@"
}

cleanup() {
    local status=$?
    if [ $status -ne 0 ]; then
        echo "::group::Container logs"
        compose logs --no-color --tail 200 || true
        echo "::endgroup::"
    fi
    compose down -v --remove-orphans > /dev/null 2>&1 || true
    rm -rf "$WORK"
    exit $status
}
trap cleanup EXIT

json() {
    # json <python expression on variable d>
    python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"
}

step() { echo "==> $*"; }

step "Preparing feeds and environment in $WORK"
python3 "$ROOT/tests/integration/make_feeds.py" "$WORK/feeds"
mkdir -p "$WORK/data/intelligence"
cat > "$WORK/.env" <<ENV
POSTGRES_PASSWORD=it-db-secret
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
MINIFLUX_PORT=$MF_PORT
INTELLIGENCE_PORT=$IT_PORT
BASE_URL=http://localhost:$MF_PORT
DATA_PATH=$WORK/data
PUID=$(id -u)
PGID=$(id -g)
FEEDS_DIR=$WORK/feeds
MINIFLUX_API_KEY=
ENV

step "Starting database, Miniflux and feed server"
compose up -d --build --wait --wait-timeout 180 db miniflux feeds

step "Creating API key"
API_KEY=$(curl -fsS -u "admin:$ADMIN_PASSWORD" -H 'Content-Type: application/json' \
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
curl -fsS "http://localhost:$IT_PORT/healthz" | json 'd["last_stats"]'
STORIES=$(curl -fsS "http://localhost:$IT_PORT/api/stories")
echo "$STORIES" | json '"\n".join(f"    {s[\"source_count\"]} sources: {s[\"headline\"][\"title\"]}" for s in d)'

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

curl -fsS "http://localhost:$IT_PORT/" | grep -q "Bundestag beschließt Haushalt"

step "Integration test passed"

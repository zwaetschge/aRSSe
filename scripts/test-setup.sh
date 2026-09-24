#!/bin/bash
# ===========================================
# aRSSe Test für scripts/setup.sh
# ===========================================
# Führt setup.sh in einem temporären Projektverzeichnis mit einem
# Docker-Stub aus (kein Docker nötig) und prüft die erzeugte .env:
# Passwörter, Dateirechte, BASE_URL, Wiederholbarkeit und Ergänzung
# älterer .env-Dateien.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
unset DATA_PATH

FAILURES=0
fail() { echo "    FEHLER: $*"; FAILURES=$((FAILURES + 1)); }
step() { echo "==> $*"; }

# Stubs: docker protokolliert Aufrufe, hostname liefert eine feste IP,
# openssl fehlt (setup.sh darf es nicht brauchen)
STUBS="$WORK/stubs"
mkdir -p "$STUBS"
cat > "$STUBS/docker" <<'EOF'
#!/bin/sh
echo "docker $*" >> "$DOCKER_LOG"
case "$*" in
    --version) echo "Docker version 99.0.0 (stub)" ;;
    "compose version") echo "Docker Compose version v2.99.0 (stub)" ;;
esac
exit 0
EOF
cat > "$STUBS/hostname" <<'EOF'
#!/bin/sh
[ "$1" = "-I" ] && echo "192.0.2.7 fd00::7" && exit 0
echo stubhost
EOF
cat > "$STUBS/openssl" <<'EOF'
#!/bin/sh
echo "openssl: command not found" >&2
exit 127
EOF
chmod +x "$STUBS"/*
export DOCKER_LOG="$WORK/docker.log"
STUB_PATH="$STUBS:$PATH"

new_project() {
    PROJ="$WORK/$1"
    mkdir -p "$PROJ/scripts"
    cp "$ROOT/scripts/setup.sh" "$PROJ/scripts/"
    cp "$ROOT/.env.example" "$PROJ/"
    : > "$DOCKER_LOG"
}

# run_setup <args...>: setzt OUT und STATUS, stdin ist kein Terminal
run_setup() {
    set +e
    OUT=$(PATH="$STUB_PATH" bash "$PROJ/scripts/setup.sh" "$@" < /dev/null 2>&1)
    STATUS=$?
    set -e
}

env_value() { grep -E "^$1=" "$PROJ/.env" | tail -n 1 | cut -d= -f2-; }

check_fresh_env() {
    local printed admin postgres
    [ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
    grep -q "Nächste Schritte" <<< "$OUT" || fail "'Nächste Schritte' fehlt"
    printed=$(grep "Admin-Passwort:" <<< "$OUT" | awk '{print $2}')
    admin=$(env_value ADMIN_PASSWORD)
    postgres=$(env_value POSTGRES_PASSWORD)
    [ "$printed" = "$admin" ] || fail "ausgegebenes Passwort '$printed' != ADMIN_PASSWORD '$admin'"
    [ "$admin" != "$postgres" ] || fail "ADMIN_PASSWORD gleich POSTGRES_PASSWORD"
    [ "${#admin}" -eq 24 ] || fail "ADMIN_PASSWORD hat ${#admin} statt 24 Zeichen"
    [ "${#postgres}" -eq 24 ] || fail "POSTGRES_PASSWORD hat ${#postgres} statt 24 Zeichen"
    ! grep -q HIER_SICHERES_PASSWORT_EINTRAGEN "$PROJ/.env" || fail "Platzhalter in .env"
    [ "$(stat -c %a "$PROJ/.env")" = 600 ] || fail ".env hat Rechte $(stat -c %a "$PROJ/.env")"
    [ "$(env_value BASE_URL)" = "http://192.0.2.7:8080" ] || fail "BASE_URL ist $(env_value BASE_URL)"
    grep -q "Admin-Benutzer: admin" <<< "$OUT" || fail "Admin-Benutzer fehlt"
    [ -d "$PROJ/data/postgresql" ] && [ -d "$PROJ/data/intelligence" ] || fail "Datenverzeichnisse fehlen"
    ! grep -q " up " "$DOCKER_LOG" || fail "Services wurden ohne --yes gestartet"
}

step "Erster Lauf mit --no-start, ohne openssl"
new_project fresh
run_setup --no-start
check_fresh_env

step "Erster Lauf ohne Terminal und ohne Optionen"
new_project notty
run_setup
check_fresh_env
grep -q "Kein Terminal" <<< "$OUT" || fail "Hinweis auf fehlendes Terminal fehlt"

step "Zweiter Lauf: .env bleibt, Rechte werden korrigiert"
PROJ="$WORK/fresh"
before=$(cat "$PROJ/.env")
chmod 644 "$PROJ/.env"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
[ "$(cat "$PROJ/.env")" = "$before" ] || fail ".env wurde verändert"
[ "$(stat -c %a "$PROJ/.env")" = 600 ] || fail ".env hat Rechte $(stat -c %a "$PROJ/.env")"
! grep -q "Admin-Passwort:" <<< "$OUT" || fail "Passwort erneut ausgegeben"

step "Verlorene .env bei bestehender Datenbank"
rm "$PROJ/.env"
echo 15 > "$PROJ/data/postgresql/PG_VERSION"
run_setup --no-start
[ "$STATUS" -ne 0 ] || fail "setup.sh hätte abbrechen müssen"
[ ! -e "$PROJ/.env" ] || fail "neue .env geschrieben"
grep -q "aus Backup wiederherstellen" <<< "$OUT" || fail "Hinweis auf Backup fehlt"

step "Ältere .env wird ergänzt, eigene Werte bleiben"
new_project upgrade
grep -vE '^(INTELLIGENCE_PORT|PUID|PGID|BASE_URL)=' "$ROOT/.env.example" \
    | sed 's/HIER_SICHERES_PASSWORT_EINTRAGEN/geheim/' > "$PROJ/.env"
echo "CLUSTERING_EPS=0.5" >> "$PROJ/.env"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
for key in INTELLIGENCE_PORT PUID PGID; do
    [ "$(grep -cE "^$key=" "$PROJ/.env")" -eq 1 ] || fail "$key nicht genau einmal ergänzt"
done
[ "$(env_value BASE_URL)" = "http://192.0.2.7:8080" ] || fail "BASE_URL ist $(env_value BASE_URL)"
[ "$(env_value POSTGRES_PASSWORD)" = geheim ] || fail "POSTGRES_PASSWORD verändert"
grep -q "# Port der Top-Stories-Oberfläche" "$PROJ/.env" || fail "Kommentar nicht übernommen"
grep -q "CLUSTERING_EPS ist veraltet" <<< "$OUT" || fail "Warnung zu CLUSTERING_EPS fehlt"
run_setup --no-start
grep -q ".env ist aktuell" <<< "$OUT" || fail "zweite Ergänzung nicht leer"

step "Eigene BASE_URL wird nicht überschrieben"
sed -i 's|^BASE_URL=.*|BASE_URL=https://news.example.com|' "$PROJ/.env"
run_setup --no-start
[ "$(env_value BASE_URL)" = "https://news.example.com" ] || fail "BASE_URL überschrieben"

step "--yes startet Datenbank und Miniflux"
new_project start
run_setup --yes
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
grep -q "docker compose up -d --wait db miniflux" "$DOCKER_LOG" || fail "kein compose up: $(cat "$DOCKER_LOG")"

step "Ohne Stubs (echtes docker, hostname und openssl, falls vorhanden)"
new_project realpath
STUB_PATH="$PATH" run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
[ "$(env_value ADMIN_PASSWORD)" != "$(env_value POSTGRES_PASSWORD)" ] || fail "Passwörter gleich"

if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES Prüfung(en) fehlgeschlagen"
    echo "Letzte Ausgabe:"
    echo "$OUT"
    exit 1
fi
step "setup.sh-Test bestanden"

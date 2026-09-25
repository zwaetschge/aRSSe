#!/bin/bash
# ===========================================
# aRSSe Test für scripts/setup.sh
# ===========================================
# Führt setup.sh in einem temporären Projektverzeichnis mit einem
# Docker-Stub aus (kein Docker nötig) und prüft die erzeugte .env:
# Passwörter, Dateirechte, BASE_URL, Wiederholbarkeit, Ergänzung
# älterer .env-Dateien und den Schutz einer bestehenden Datenbank.

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

# PATH ohne docker: nur die Werkzeuge, die setup.sh braucht
NODOCKER="$WORK/nodocker"
mkdir -p "$NODOCKER"
for tool in bash head base64 tr sed grep cut tail cp mv rm chmod chown mkdir \
        mktemp ls awk date dirname stat cat printf ip env; do
    if path=$(command -v "$tool"); then ln -s "$path" "$NODOCKER/$tool"; fi
done
ln -s "$STUBS/hostname" "$NODOCKER/hostname"

# base64 schlägt fehl: gen_pw bricht ab
BROKEN="$WORK/broken"
mkdir -p "$BROKEN"
printf '#!/bin/sh\nexit 1\n' > "$BROKEN/base64"
chmod +x "$BROKEN/base64"

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

env_value() { grep -sE "^$1=" "$PROJ/.env" | tail -n 1 | cut -d= -f2- || true; }

check_fresh_env() {
    local printed admin postgres
    [ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
    grep -q "Nächste Schritte" <<< "$OUT" || fail "'Nächste Schritte' fehlt"
    printed=$(grep "Admin-Passwort:" <<< "$OUT" | awk '{print $2}') || true
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
    grep -q "kann jeder im Netz die Top Stories" <<< "$OUT" || fail "Hinweis auf offene Top Stories fehlt"
    [ -d "$PROJ/data/postgresql" ] || fail "Datenverzeichnis postgresql fehlt"
    [ -d "$PROJ/data/intelligence" ] || fail "Datenverzeichnis intelligence fehlt"
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

step "Verlorene .env, Datenbankverzeichnis gehört Postgres (nicht lesbar)"
# Postgres setzt data/postgresql auf uid 70 und Rechte 700. Root darf
# trotzdem hineinsehen, daher dort stattdessen ein Verzeichnis ohne PG_VERSION.
new_project lostdb
mkdir -p "$PROJ/data/postgresql"
echo x > "$PROJ/data/postgresql/postmaster.opts"
if [ "$(id -u)" -ne 0 ]; then
    chmod 000 "$PROJ/data/postgresql"
fi
run_setup --no-start
chmod 700 "$PROJ/data/postgresql"
[ "$STATUS" -ne 0 ] || fail "setup.sh hätte abbrechen müssen"
[ ! -e "$PROJ/.env" ] || fail "neue .env geschrieben"
! grep -q "Admin-Passwort:" <<< "$OUT" || fail "falsches Passwort ausgegeben"

step "Leeres Datenbankverzeichnis (erster Lauf abgebrochen) gilt nicht als Datenbank"
rm "$PROJ/data/postgresql/postmaster.opts"
run_setup --no-start
check_fresh_env

step "Mit 'cp .env.example .env' angelegte .env: Platzhalter werden ersetzt"
new_project copied
cp "$PROJ/.env.example" "$PROJ/.env"
chmod 600 "$PROJ/.env"
run_setup --no-start
check_fresh_env

step "Platzhalter-Passwörter bei bestehender Datenbank: Abbruch, kein Start"
new_project copieddb
cp "$PROJ/.env.example" "$PROJ/.env"
mkdir -p "$PROJ/data/postgresql"
echo 15 > "$PROJ/data/postgresql/PG_VERSION"
run_setup --yes
[ "$STATUS" -ne 0 ] || fail "setup.sh hätte abbrechen müssen"
! grep -q " up " "$DOCKER_LOG" || fail "Services trotz Platzhalter gestartet"
[ "$(env_value ADMIN_PASSWORD)" = HIER_SICHERES_PASSWORT_EINTRAGEN ] || fail "ADMIN_PASSWORD verändert"

step "Leeres ADMIN_PASSWORD bei bestehender Datenbank: Admin existiert, kein Abbruch"
sed -i -e 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=eigenes/' -e 's/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=/' \
    "$PROJ/.env"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
[ -z "$(env_value ADMIN_PASSWORD)" ] || fail "ADMIN_PASSWORD verändert"
[ "$(env_value POSTGRES_PASSWORD)" = eigenes ] || fail "POSTGRES_PASSWORD verändert"

step "Nur ADMIN_PASSWORD fehlt: wird erzeugt, POSTGRES_PASSWORD bleibt"
new_project halfset
sed -e 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=eigenes/' -e '/^ADMIN_PASSWORD=/d' \
    "$PROJ/.env.example" > "$PROJ/.env"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
[ "$(env_value POSTGRES_PASSWORD)" = eigenes ] || fail "POSTGRES_PASSWORD verändert"
admin=$(env_value ADMIN_PASSWORD)
[ "${#admin}" -eq 24 ] || fail "ADMIN_PASSWORD '$admin' nicht erzeugt"
[ "$(grep "Admin-Passwort:" <<< "$OUT" | awk '{print $2}')" = "$admin" ] \
    || fail "ausgegebenes Passwort passt nicht zu .env"

step "Abbruch beim Erzeugen der Passwörter hinterlässt keine .env"
new_project interrupted
STUB_PATH="$BROKEN:$STUB_PATH" run_setup --no-start
[ "$STATUS" -ne 0 ] || fail "setup.sh hätte abbrechen müssen"
[ ! -e "$PROJ/.env" ] || fail ".env mit Platzhaltern hinterlassen"
[ -z "$(find "$PROJ" -maxdepth 1 -name '.env.?*' ! -name .env.example)" ] \
    || fail "temporäre Datei hinterlassen"
run_setup --no-start
check_fresh_env

step "Ohne Terminal und ohne Docker: .env anlegen, nicht starten"
new_project nodocker
STUB_PATH="$NODOCKER" run_setup
check_fresh_env
grep -q "Docker ist nicht installiert" <<< "$OUT" || fail "Hinweis auf fehlendes Docker fehlt"

step "MINIFLUX_PORT/INTELLIGENCE_PORT mit Host-Adresse (127.0.0.1:8090)"
new_project hostport
sed -e 's/^MINIFLUX_PORT=.*/MINIFLUX_PORT=127.0.0.1:8090/' \
    -e 's/^INTELLIGENCE_PORT=.*/INTELLIGENCE_PORT=127.0.0.1:8091/' \
    "$PROJ/.env.example" > "$PROJ/.env.example.new"
mv "$PROJ/.env.example.new" "$PROJ/.env.example"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
[ "$(env_value BASE_URL)" = "http://192.0.2.7:8090" ] || fail "BASE_URL ist $(env_value BASE_URL)"
grep -q "http://192.0.2.7:8090" <<< "$OUT" || fail "Adresse in 'Nächste Schritte' falsch"
grep -q "http://127.0.0.1:8091" <<< "$OUT" || fail "Top-Stories-Adresse in 'Nächste Schritte' falsch"
grep -q "Nur auf dem Server selbst" <<< "$OUT" || fail "Hinweis auf lokale Top Stories fehlt"
if grep -q "kann jeder im Netz" <<< "$OUT"; then fail "Warnung trotz 127.0.0.1"; fi

step "Top Stories mit WEB_AUTH_MODE: keine Warnung, Adresse mit Server-IP"
new_project webauth
printf 'WEB_AUTH_MODE=basic\nINTELLIGENCE_PORT=8092\n' >> "$PROJ/.env.example"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
grep -q "http://192.0.2.7:8092" <<< "$OUT" || fail "Top-Stories-Adresse in 'Nächste Schritte' falsch"
if grep -q "kann jeder im Netz" <<< "$OUT"; then fail "Warnung trotz WEB_AUTH_MODE=basic"; fi

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

# Projekt mit intelligence/config.yaml und Vorlage als Git-Arbeitsverzeichnis
new_git_project() {
    new_project "$1"
    mkdir -p "$PROJ/intelligence"
    cp "$ROOT/intelligence/config.yaml" "$ROOT/intelligence/config.stub.yaml" "$PROJ/intelligence/"
    git -C "$PROJ" init -q
    git -C "$PROJ" add intelligence
    git -C "$PROJ" -c user.name=test -c user.email=test@example.org commit -qm init
}
USER_CONFIG_PATH_REL="data/intelligence/config.yaml"

step "Eigene Einstellungen: Vorlage in DATA_PATH/intelligence/config.yaml"
new_git_project userconfig
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
cmp -s "$PROJ/intelligence/config.stub.yaml" "$PROJ/$USER_CONFIG_PATH_REL" \
    || fail "Vorlage nicht angelegt"
[ "$(stat -c %a "$PROJ/$USER_CONFIG_PATH_REL")" = 600 ] || fail "Vorlage hat nicht Rechte 600"
echo "web: {min_sources: 4}" > "$PROJ/$USER_CONFIG_PATH_REL"
run_setup --no-start
grep -q "min_sources: 4" "$PROJ/$USER_CONFIG_PATH_REL" || fail "eigene Einstellungen überschrieben"

step "Geänderte intelligence/config.yaml wird übernommen"
new_git_project migrate
sed -i 's/min_sources: 2/min_sources: 3/' "$PROJ/intelligence/config.yaml"
run_setup --no-start
[ "$STATUS" -eq 0 ] || fail "Exit-Code $STATUS"
grep -q "min_sources: 3" "$PROJ/$USER_CONFIG_PATH_REL" 2>/dev/null || fail "Änderung nicht übernommen"
grep -q "git checkout -- intelligence/config.yaml" <<< "$OUT" || fail "Hinweis auf git checkout fehlt"
echo "web: {min_sources: 5}" > "$PROJ/$USER_CONFIG_PATH_REL"
run_setup --no-start
grep -q "min_sources: 5" "$PROJ/$USER_CONFIG_PATH_REL" || fail "eigene Einstellungen überschrieben"
grep -q "gilt aber nur noch in selbst gebauten" <<< "$OUT" || fail "Hinweis auf doppelte Einstellungen fehlt"

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

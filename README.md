# aRSSe - Autonomer RSS-basierter News-Aggregator

Ein selbstgehosteter Nachrichten-Aggregator für Unraid-Systeme, der die Kernfunktionalität von Google News repliziert. Optimiert für E-Ink-Displays und Mobile-First-Paradigmen.

## Architektur

Das System besteht aus fünf logischen Schichten:

| Schicht | Komponente | Technologie | Funktion |
|---------|------------|-------------|----------|
| Ingestion | Miniflux | Go, Docker | RSS/Atom-Abruf, Parsing, Full-Content Scraping |
| Storage | PostgreSQL | SQL | Persistente Speicherung von Artikeln und Metadaten |
| Intelligence | Python-Service | Python, Scikit-Learn, SQLite | TF-IDF, Average-Linkage-Clustering, Deduplizierung |
| Presentation | Top Stories + Miniflux-CSS | Flask, HTML5, CSS3 | Story-Ansicht und E-Ink-optimierte Themes |
| Access | Nginx Proxy | Docker, Let's Encrypt | SSL-Terminierung, externer Zugriff |

## Voraussetzungen

- Unraid 6.x oder höher (oder ein anderer Linux-Server, amd64 oder arm64)
- Docker und Docker Compose – auf Unraid über das Plugin *Compose Manager*, oder ohne Compose über die Templates (siehe [Unraid ohne Compose](#unraid-ohne-compose))
- Etwa 1 GB freier RAM für den ganzen Stack: Der Intelligence Layer braucht bei 2000 Artikeln pro Lauf gemessen rund 270 MB (beim Höchstwert `max_entries: 5000` rund 790 MB) und ist auf 1 GB begrenzt
- SSD-Cache empfohlen für PostgreSQL

## Schnellstart

### 1. Repository klonen

```bash
git clone https://github.com/zwaetschge/aRSSe.git
cd aRSSe
```

### 2. Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
chmod 600 .env
# Bearbeiten Sie .env mit Ihren Einstellungen
```

Tragen Sie für `POSTGRES_PASSWORD` und `ADMIN_PASSWORD` zwei unterschiedliche Passwörter ein.

### 2b. BASE_URL setzen

Setzen Sie `BASE_URL` in `.env` auf die Adresse, unter der Ihre Geräte Miniflux erreichen, z.B. `BASE_URL=http://192.168.1.10:8080` oder `https://news.meinedomain.de`. Die Links der Top Stories führen dorthin – `localhost` wäre auf dem E-Ink-Reader das Gerät selbst. Bleibt `BASE_URL` auf `localhost`, bauen die Top Stories ihre Links notfalls aus der aufgerufenen Adresse und `MINIFLUX_PORT`.

### 3. Stack starten

```bash
docker compose up -d
```

Den Intelligence Layer lädt Compose als fertiges Image (`ghcr.io/zwaetschge/arsse-intelligence`, für amd64 und arm64); gibt es das Image nicht, baut Compose es aus `./intelligence` – nur beim ersten Mal, denn das gebaute Image trägt denselben Namen. `docker compose up -d --build` baut immer den lokalen Code (so auch Updates ohne Release-Version). Die Version wählt `ARSSE_VERSION` in `.env`: `latest` (Standard, die letzte Release-Version), eine feste Version wie `1.0` oder `edge` (jeder Stand des Hauptzweigs).

Das Datenverzeichnis des Intelligence Layers (`${DATA_PATH}/intelligence`) muss nicht vorab angelegt werden: Der Container startet als root, übergibt es an `PUID:PGID` (Standard 1000:1000, Unraid 99:100) und läuft danach ohne Root-Rechte.

### 4. Miniflux aufrufen

Öffnen Sie `http://<unraid-ip>:8080` und melden Sie sich mit den in `.env` konfigurierten Zugangsdaten an.

### 5. Top Stories aktivieren

Erzeugen Sie in Miniflux unter *Einstellungen > API-Schlüssel* einen Key, tragen Sie ihn als `MINIFLUX_API_KEY` in `.env` ein und starten Sie `docker compose up -d intelligence`. Die Top Stories sind dann unter `http://<unraid-ip>:8081` erreichbar – ohne Anmeldung für jeden im Netz. Wie Sie sie mit einem Passwort schützen und warum der Key besser einem eigenen Benutzer ohne Admin-Rechte gehört, steht unter [Absicherung](#absicherung).

Alternativ erledigt `scripts/setup.sh` die Schritte 2–4: Es erzeugt `.env` (Rechte 600) mit zwei zufälligen Passwörtern, setzt `BASE_URL` auf die erkannte IP-Adresse des Servers, legt die Datenverzeichnisse an und startet auf Nachfrage Datenbank und Miniflux. `--yes` startet ohne Rückfrage, `--no-start` gar nicht (Standard ohne Terminal, z.B. in Unraid User Scripts). Ein erneuter Aufruf ergänzt eine bestehende `.env` nur um Einträge, die in neueren Versionen von `.env.example` hinzugekommen sind, und warnt vor veralteten. Ersetzt werden einzig eine `BASE_URL`, die noch auf `localhost` zeigt, und leere oder Platzhalter-Passwörter (etwa nach `cp .env.example .env`), solange noch keine Datenbank existiert. Existiert die Datenbank schon, obwohl `.env` fehlt oder noch Platzhalter enthält, bricht das Skript ab, ohne etwas zu starten – stellen Sie dann `.env` aus dem Backup wieder her.

## Komponenten

### Miniflux (Ingestion Layer)

Miniflux ist ein minimalistischer RSS-Reader, geschrieben in Go. Er dient als zentrale Komponente für:

- Feed-Abruf mit konfigurierbarer Polling-Frequenz
- Full-Content Extraction via Readability-Algorithmus
- Custom Scraper Rules für komplexe Webseiten
- REST-API für externe Automatisierung

**Konfigurationsoptionen:**

| Variable | Beschreibung | Standard |
|----------|--------------|----------|
| `POLLING_FREQUENCY` | Abrufintervall in Minuten | 15 |
| `POLLING_SCHEDULER` | Scheduling-Strategie | entry_frequency |
| `CLEANUP_ARCHIVE_READ_DAYS` | Aufbewahrung gelesener Artikel | 60 |
| `CLEANUP_ARCHIVE_UNREAD_DAYS` | Aufbewahrung ungelesener Artikel | 30 |

### Intelligence Layer (Clustering + Top Stories)

Der Python-Service läuft zyklisch (Standard: alle 30 Minuten) und führt folgende Schritte aus:

1. **Extraction**: Abruf aller Artikel der letzten 24 Stunden via Miniflux API (gelesen und ungelesen, seitenweise nach Artikel-ID – auch bei gleichen Veröffentlichungszeiten kommt kein Artikel doppelt; Feeds und Kategorien, die in Miniflux aus den globalen Listen ausgeblendet sind, bleiben draußen)
2. **Preprocessing**: HTML entfernen, Normalisierung, Stopwords, Stemming (Snowball)
3. **Vectorization**: TF-IDF über Uni- und Bigramme
4. **Clustering**: Agglomeratives Clustering (Average Linkage) gruppiert Artikel zum selben Ereignis zu einer *Story*; ein zweiter, großzügigerer Durchgang fasst Stories zu *Themen* zusammen (Klopps Debüt, seine Aufstellung, der gegnerische Trainer)
5. **Deduplication**: Near-Duplicates (z.B. identische Agenturmeldungen verschiedener Feeds, verglichen ohne Titel) und doppelt gelieferte Artikel innerhalb einer Story
6. **Persistence**: Stories landen in einer lokalen SQLite-Datenbank (`data/intelligence/arsse.db`) mit über Läufe hinweg stabilen IDs; Duplikate werden optional in Miniflux als gelesen markiert – standardmäßig nur in Stories, die die Startseite zeigt (doppelt gelieferte Artikel überall), und jedes nur einmal: Wer ein Duplikat wieder auf ungelesen setzt, behält es ungelesen. Artikel im Zeitfenster, die es in Miniflux nicht mehr gibt (z.B. nach „Verlauf leeren“ oder dem Abbestellen eines Feeds), verschwinden beim nächsten Lauf aus ihren Stories; ältere Artikel unter „Frühere Berichte“ verlinken deshalb auf das Original beim Anbieter. Nach einem Update passt der Service das Schema der Datenbank beim Start selbst an

Die Miniflux-API kann keine Tags oder eigenen Metadaten schreiben – deshalb bringt der Service eine eigene, JavaScript-freie Oberfläche mit:

| Pfad | Inhalt |
|------|--------|
| `/` | Top Stories aus mindestens zwei Feeds, gerankt nach Anzahl der Quellen und Aktualität – gezählt wird nur, was im Zeitfenster (24 Stunden) erschienen ist. Jedes Thema belegt einen Platz, verwandte Stories stehen darunter als „Mehr zum Thema“. 10 Plätze pro Seite (`?seite=2` usw.), insgesamt bis zu 100; `?rubrik=Sport` zeigt eine Rubrik, `?alle=1` auch gelesene Stories, `?auto=1` lädt die Seite alle 30 Minuten neu (für Always-on-Displays) |
| `/story/<id>` | Alle Artikel einer Story im Zeitfenster mit erster und letzter Meldung, gleichlautende Meldungen zusammengeklappt, darunter bis zu 20 ältere als „Frühere Berichte“ (mit Link auf das Original) und „Verwandte Stories“. `?ansicht=chronologisch` zeigt den Verlauf von der ersten Meldung an, nach Tagen gruppiert |
| `/story/<id>/gelesen` | Knopf „Story gelesen (N)“ (Formular, `POST`): markiert die ungelesenen Artikel der Story im Zeitfenster in Miniflux als gelesen |
| `/suche?q=…` | Suche in Titeln und Anrissen aller gespeicherten Stories (auch älterer, `storage.retention_days`), 2 bis 100 Zeichen; darunter ein Link zur Volltextsuche von Miniflux |
| `/api/stories` | Dieselben Daten als JSON (alle Stories einzeln, ohne Seiten und Themen; `?rubrik=` und `?alle=1` wie oben) |
| `/healthz` | Zustand als JSON (immer ohne Anmeldung): `ok` (`200`), solange der letzte erfolgreiche Lauf weniger als drei Intervalle zurückliegt; `starting` (`200`) nach dem Start, bis der erste Lauf gelingt, höchstens drei Intervalle lang und nur, solange seit dem Start kein Lauf gescheitert ist (ein gespeicherter Fehler von vor dem Neustart zählt nicht); sonst `stale` (`503`). `last_error` nennt den Grund des letzten Fehlschlags, `last_stats` die Statistik des letzten Laufs |
| `/static/…` | Manifest und Icons für den Startbildschirm (immer ohne Anmeldung, Icons holen Browser und Android teils ohne Zugangsdaten) |

Die Seiten sind für E-Ink gebaut: kurze Seiten statt langem Scrollen, nur absolute Uhrzeiten („Stand 18:32“, „Mi 14:53“ – relative Angaben wie „vor 5 Min.“ stimmen auf einem stehenden Bildschirm bald nicht mehr), jede Quelle zuerst mit einem Artikel statt mehrerer aus demselben Feed, und Links, die als ganze Zeile mindestens 44 px hoch antippbar sind. Die Uhrzeiten gelten in der Zeitzone `TZ` aus `.env` (Standard Europe/Berlin) oder `web.timezone` in `config.yaml`.

**Gelesen:** Eine Story, deren Artikel im Zeitfenster alle gelesen sind, verschwindet von der Startseite (die Statuszeile bietet „N gelesene zeigen“). „Story gelesen (N)“ markiert mit einem Tipp alle ungelesenen Artikel der Story in Miniflux als gelesen – statt neun fast gleicher Meldungen einzeln. Kommen danach neue Artikel hinzu, erscheint die Story wieder mit „N neu“. Was in Miniflux selbst gelesen wird, übernehmen die Top Stories alle 5 Minuten (`scheduling.status_sync_minutes`); auch eine so gelesene Story verschwindet und kommt mit neuen Artikeln zurück, dann aber ohne „N neu“ (aRSSe weiß nicht, ob Sie in Miniflux die ganze Story gelesen haben oder nur einen Artikel davon). Der Knopf braucht den `MINIFLUX_API_KEY`; ohne Anmeldung (`WEB_AUTH_MODE=none`) kann jeder, der den Port erreicht, Stories als gelesen markieren – fremde Webseiten können es nicht (siehe „Absicherung“).

**Fehler:** Scheitert ein Lauf, nennt die Statuszeile der Startseite den Grund, z.B. „Fehler seit 10:30: Miniflux lehnt den API-Key ab“, „MINIFLUX_API_KEY fehlt“, „Miniflux unter http://miniflux:8080 nicht erreichbar“ oder „Datenbank nicht beschreibbar“. Der Dienst versucht es nach 10 Sekunden erneut, danach mit doppeltem Abstand, höchstens alle 5 Minuten; der erste erfolgreiche Lauf entfernt die Meldung. Die Einzelheiten stehen im Log (`docker logs arsse-intelligence`).

**Rubriken:** Politik, Sport, Technik, Regional usw. kommen aus der Kategorie des Feeds in Miniflux. Für Feeds in der Standardkategorie „All“ entscheidet der Pfad der Artikel-URL (`tagesschau.de/ausland/…` → Politik, `…/sport/…` → Sport; `web.path_sections` in `config.yaml`); eine Story gehört zur Rubrik der meisten ihrer Artikel.

Links führen in Miniflux (`BASE_URL`), damit Gelesen-Status und Volltext erhalten bleiben. Zeigt `BASE_URL` auf `localhost`, verwenden die Links stattdessen die Adresse, unter der die Top Stories aufgerufen wurden, mit `MINIFLUX_PORT` (beim Start erscheint dazu eine Warnung im Log).

#### Konfiguration

Eigene Einstellungen gehören in `${DATA_PATH}/intelligence/config.yaml` (im Container `/app/data/config.yaml`, Unraid: `/mnt/user/appdata/arsse/intelligence/config.yaml`). Die Datei liegt neben der Story-Datenbank, bleibt bei Updates des Images und bei `git pull` erhalten und ist in Backups enthalten; `scripts/setup.sh` bzw. der erste Start legen sie mit auskommentierten Beispielen an. Tragen Sie dort nur ein, was vom Standard abweichen soll, und starten Sie danach neu (`docker compose restart intelligence`). Alle Einstellungen mit Erklärung stehen in [`intelligence/config.yaml`](intelligence/config.yaml) – diese Datei ist die Referenz im Image und wird nicht geändert.

Reihenfolge: Standardwerte < `intelligence/config.yaml` (im Image) < `${DATA_PATH}/intelligence/config.yaml` < Umgebungsvariablen (`.env` bzw. Unraid-Template). Abschnitte werden zusammengeführt: Es gilt jede Einstellung, die in Ihrer Datei steht, alle anderen behalten ihren Standard; Listen und Tabellen (z.B. `source_scores`, `exclude_patterns`) ersetzen den Standard ganz. Ein Beispiel, das nur Abweichungen enthält:

```yaml
# ${DATA_PATH}/intelligence/config.yaml
scheduling:
  interval_minutes: 15           # öfter clustern (Standard 30)

web:
  min_sources: 3                 # nur Stories aus mindestens drei Feeds
  exclude_patterns:              # ersetzt die Standardliste ganz, daher deren Muster mit aufführen
    - '^Wetter\b'
    - '^Lotto'

deduplication:
  duplicate_action: "none"       # Duplikate nicht als gelesen markieren
```

Welche Einstellungen es gibt und welche Standardwerte gelten (z.B. `clustering.threshold`, `deduplication.threshold`, `web.page_size`), steht mit Erklärung in der Referenz [`intelligence/config.yaml`](intelligence/config.yaml). Übernehmen Sie von dort nur, was Sie wirklich ändern: Jede Einstellung in Ihrer Datei hält ihren Wert fest, auch wenn eine spätere Version den Standard verbessert.

Umgebungsvariablen aus `.env` (z.B. `CLUSTERING_THRESHOLD`) überschreiben beide Dateien; auskommentierte bzw. leere Variablen tun das nicht. Für das Unraid-Template gibt es zusätzlich `LOOKBACK_HOURS`, `RETENTION_DAYS`, `WEB_MIN_SOURCES` und `WEB_MAX_STORIES`.

**Früher geänderte `intelligence/config.yaml`:** Ältere Versionen dieses README empfahlen, die Datei im Repository zu ändern. Das blockiert `git pull`, und das veröffentlichte Image enthält die Änderungen nicht. Übernehmen Sie sie vor dem Update:

```bash
cp intelligence/config.yaml ${DATA_PATH}/intelligence/config.yaml   # DATA_PATH aus .env
git checkout -- intelligence/config.yaml
git pull
```

Danach in der neuen Datei nur die geänderten Einstellungen stehen lassen, dann gelten künftige Verbesserungen der Standardwerte auch für Sie. `scripts/setup.sh` übernimmt eine geänderte `intelligence/config.yaml` automatisch, solange die eigene Datei fehlt oder noch die unveränderte Vorlage ist.

Ist die Änderung trotzdem noch in `intelligence/config.yaml` (z.B. nach `git stash` und `git stash pop` oder als eigener Commit), geht sie nicht verloren: `docker-compose.yml` bindet `./intelligence` nur lesend als `/app/legacy` ein, und Einstellungen, die dort geändert sind und in Ihrer eigenen Datei fehlen, gelten vorerst weiter. Das Log nennt sie dann bei jedem Start (`… differs from every shipped version of the reference in: clustering.threshold …`) – ziehen Sie sie wie oben um; eine spätere Version liest die Datei nicht mehr. Dasselbe gilt für ein selbst gebautes Image mit geänderter Referenz. Als geändert gilt die Datei nur, wenn sie keiner jemals veröffentlichten Fassung der Referenz entspricht (`intelligence/config.history.json`), und dann zählen nur die Abweichungen von der ähnlichsten Fassung: Dass Checkout und Image verschiedene Versionen sind (`git pull` folgt dem Hauptzweig, `ARSSE_VERSION=latest` der letzten Release-Version), ist keine Änderung. Einstellungen, mit denen die Konfiguration ungültig wäre, lässt der Dienst mit einer Warnung weg.

Anpassungen an `docker-compose.yml` selbst (Ports, Speicherlimit, zusätzliche Mounts) gehören in eine `docker-compose.override.yml` daneben: Compose liest sie automatisch, und `git pull` lässt sie in Ruhe. Beispiel:

```yaml
# docker-compose.override.yml
services:
  intelligence:
    mem_limit: 512m   # reicht bis etwa scheduling.max_entries: 3000
```

**Schwelle kalibrieren:** Der Standardwert 0.75 ist an ~500 echten Artikeln aus 13 deutschen Nachrichtenfeeds gemessen (Kurztexte aus RSS, kein Volltext). Mit anderen Feeds oder aktiviertem Volltext-Crawler lohnt ein Vergleich – das Werkzeug liest nur und schreibt nichts:

```bash
docker compose exec intelligence python evaluate.py 0.65 0.7 0.75 0.8
```

Es zeigt je Schwelle die Anzahl der Stories sowie die größten und einige zufällige Stories. Große Stories mit gemischten Themen = Schwelle zu hoch; viele zusammengehörige Artikel außerhalb von Stories = zu niedrig. Viele zufällige Paare aus zwei Artikeln: `min_pair_similarity` erhöhen (0.35 ist strenger).

Wer genauer messen will: `eval/export_corpus.py` speichert die Artikel des Zeitfensters, und `evaluate.py --corpus … --gold …` bewertet das Clustering gegen handmarkierte Paare (Precision/Recall, unsinnige Stories, Stabilität über mehrere Läufe). Die Messwerte am Referenzsatz und die Anleitung stehen in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) („Messungen am Referenzsatz“).

**Werbung und Rauschen:** Titel wie „Anzeige: …“, „(g+) …“, Podcasts oder Liveblogs werden nur dann Schlagzeile einer Story, wenn sie keinen anderen Artikel hat (`noise_title_patterns`), und der Wetterbericht erscheint nicht als Story (`web.exclude_patterns`). Werbung lässt sich zusätzlich schon in Miniflux verwerfen: *Einstellungen > Eintrags-Sperrregeln* mit `EntryTitle=(?i)^(Anzeige|heise-Angebot):` – dann fehlen diese Einträge aber auch in Miniflux selbst.

**Lokale Feeds:** Miniflux ruft seit 2.3 standardmäßig keine Feeds aus privaten Netzen ab. Für Feeds im Heimnetz `FETCHER_ALLOW_PRIVATE_NETWORKS=1` beim Miniflux-Container setzen.

### E-Ink Optimierung

Das Custom CSS für E-Ink-Displays berücksichtigt:

- **Keine Animationen**: Vermeidung von Ghosting
- **Hoher Kontrast**: Reines Schwarz auf Weiß
- **Serifen-Typografie**: Bessere Lesbarkeit
- **Große Touch-Targets**: Mobile-freundliche Bedienung
- **Pagination statt Scrolling**: Weniger Refreshes

Beide Themes (`css/eink-theme.css`, `css/color-theme.css`) werden in Miniflux unter *Einstellungen > Benutzerdefiniertes CSS* eingefügt. Sie verwenden die Schriften des Geräts (Literata, Charter oder Georgia) und laden nichts von fremden Servern – unter *Einstellungen > Externe Schriftart-Hosts* ist nichts einzutragen. Wer die ursprünglichen Web-Fonts möchte, entfernt die Kommentarzeichen um die `@import`-Zeile am Anfang der Datei und trägt dort `fonts.googleapis.com fonts.gstatic.com` ein; dann ruft jede Miniflux-Seite Google auf (IP-Adresse, Uhrzeit), und E-Ink-Geräte bauen die Seite langsamer auf.

## Verzeichnisstruktur

```
aRSSe/
├── docker-compose.yml      # Haupt-Stack-Definition
├── .env.example            # Umgebungsvariablen-Vorlage
├── intelligence/
│   ├── Dockerfile          # Python-Container (Basis-Image per Digest festgelegt)
│   ├── requirements.in     # Python-Abhängigkeiten (Versionsbereiche)
│   ├── requirements.txt    # daraus erzeugt: feste Versionen mit Hashes (pip-compile, nicht von Hand ändern)
│   ├── fetch_nltk_data.py  # Stopwörter aus festem nltk_data-Stand, mit Prüfsumme
│   ├── entrypoint.py       # Container-Start: Datenrechte setzen, Root-Rechte abgeben
│   ├── news_clustering.py  # Clustering-Logik und Einstiegspunkt
│   ├── evaluate.py         # Clustering-Schwelle an eigenen Feeds kalibrieren und messen
│   ├── eval/               # Mess-Werkzeuge: Korpus-Export, Metriken, Wiederholung, gold.json
│   ├── store.py            # SQLite-Story-Datenbank
│   ├── web.py              # Top-Stories-Oberfläche
│   ├── templates/          # HTML-Templates
│   ├── static/             # Manifest und Icons für den Startbildschirm
│   ├── config.py           # Konfigurationsmodul
│   ├── config.yaml         # Referenz aller Einstellungen (im Image, nicht ändern)
│   ├── config.stub.yaml    # Vorlage für DATA_PATH/intelligence/config.yaml
│   ├── config.history.json # alle Fassungen von config.yaml (scripts/config-history.py)
│   └── tests/              # pytest-Suite
├── css/
│   ├── eink-theme.css      # E-Ink-Theme für Miniflux
│   └── color-theme.css     # Farb-Theme für Miniflux
├── unraid/
│   ├── miniflux.xml        # Unraid-Template für Miniflux
│   └── arsse-intelligence.xml # Unraid-Template für den Intelligence Layer
├── scripts/
│   ├── setup.sh            # Initialisierungsskript
│   ├── backup.sh           # Backup von Datenbanken, Abos und .env
│   ├── make-icons.py       # Erzeugt die Icons in intelligence/static
│   ├── check-unraid-templates.py # Vergleicht die Templates mit docker-compose.yml
│   ├── config-history.py   # Erzeugt intelligence/config.history.json aus der Git-Historie
│   ├── test-setup.sh       # Test für setup.sh (ohne Docker)
│   ├── test-backup.sh      # Test für backup.sh (ohne Docker)
│   └── integration-test.sh # Stack-Test gegen echtes Miniflux
├── .github/                # CI (Tests), Release des Images, Dependabot
└── tests/integration/      # Feeds und Compose-Override für den Stack-Test
```

## E-Ink Integration

### Progressive Web App (PWA)

Miniflux funktioniert als PWA. Auf E-Ink-Android-Geräten (z.B. Boox Palma):

1. Öffnen Sie die Miniflux-URL im Browser (EinkBro oder Chrome)
2. Wählen Sie "Zum Startbildschirm hinzufügen"
3. Die App verhält sich dann wie eine native Anwendung

Die Top Stories lassen sich genauso auf den Startbildschirm legen (eigenes Icon, Name „aRSSe“). Als eigenständige App ohne Adressleiste installiert Chrome sie nur über HTTPS (siehe [Reverse Proxy](#reverse-proxy-https)); unter `http://tower:8081` entsteht eine Verknüpfung, die im Browser öffnet. Einen Service Worker gibt es nicht – die Seiten bleiben ohne JavaScript und brauchen eine Verbindung zum Server. Für ein Always-on-Display (z.B. ein E-Ink-Tablet an der Wand) die Adresse mit `?auto=1` öffnen: Die Seite lädt sich dann alle 30 Minuten neu. Alle Links innerhalb der Top Stories behalten den Parameter; wer eine Story antippt und stehen lässt, landet nach 30 Minuten wieder auf der Titelseite. Wer weitergeblättert hat, landet beim Neuladen auf der letzten Seite, wenn es inzwischen weniger Stories gibt.

### Native Apps

Alternativ können folgende Apps die Miniflux-API nutzen:

- **FeedMe** (Android): Volle Offline-Unterstützung
- **ReadYou** (Android): Material Design, E-Ink-freundlich
- **Reeder** (iOS/macOS): Native Miniflux-Integration

## Absicherung

Ohne weitere Einstellungen sind Miniflux (Port 8080) und die Top Stories (Port 8081) im ganzen Heimnetz erreichbar. Miniflux verlangt eine Anmeldung, die Top Stories nicht: Wer Port 8081 erreicht, sieht Ihre Abos und was Sie gelesen haben und kann mit „Story gelesen“ Artikel in Ihrem Miniflux als gelesen markieren – der Gast im WLAN ebenso wie ein Gerät im Netz. Beim Start steht dazu eine Warnung im Log.

### Top Stories absichern

**Mit Passwort** (am einfachsten, funktioniert auch auf E-Ink-Readern, die sich die Anmeldung merken) – in `.env`:

```bash
WEB_AUTH_MODE=basic
WEB_USERNAME=leser
WEB_PASSWORD=ein-langes-passwort
```

Danach `docker compose up -d intelligence`. Statt `WEB_PASSWORD` geht auch `WEB_PASSWORD_FILE` mit dem Pfad einer Datei im Container (siehe [Passwörter als Datei](#passwörter-und-api-key-als-datei-docker-secret)). `/healthz` bleibt ohne Anmeldung erreichbar, der Health Check von Docker braucht es, ebenso `/static/` (Manifest und Icons für den Startbildschirm, ohne Inhalte aus Ihren Feeds). Basic Auth überträgt das Passwort nur Base64-kodiert – außerhalb des Heimnetzes also nur über HTTPS (Reverse Proxy, siehe unten).

**Über den Reverse Proxy** (z.B. Authelia, Authentik oder `auth_basic` in nginx): Mit `WEB_AUTH_MODE=proxy` erwarten die Top Stories den angemeldeten Benutzer im Header `Remote-User` (`WEB_AUTH_PROXY_HEADER`, nur Buchstaben, Ziffern und `-` – Header mit `_` verwirft der Webserver) und glauben ihn nur von den Adressen in `WEB_TRUSTED_PROXIES`. Anfragen von anderen Adressen oder ohne den Header lehnt der Dienst mit `403` ab.

- Den Header setzt der Proxy, nginx z.B. mit `proxy_set_header Remote-User $remote_user;` (bei `auth_basic`, siehe Beispiel unten); Authelia und Authentik liefern ihn über `auth_request` und `auth_request_set` (siehe deren nginx-Anleitung).
- Nehmen Sie `/static/` von der Anmeldung am Proxy aus (bei nginx `location /static/` mit `auth_basic off;`, siehe Beispiel unten; bei Authelia und Authentik eine Regel ohne Anmeldung für diesen Pfad). Dort liegen nur Manifest und Icons für den Startbildschirm, und Android holt die Icons teils ohne Zugangsdaten ab. Das Manifest selbst lädt der Browser mit der Anmeldung.
- In `WEB_TRUSTED_PROXIES` gehört genau die Adresse, von der die Anfragen des Proxys im Container ankommen, kein ganzes Netz. Läuft nginx direkt auf dem Server, ist das nicht `127.0.0.1`, sondern wie bei `TRUSTED_PROXIES` das Gateway des `arsse-network` (Befehl unten); beim Proxy-Container dessen Adresse im `arsse-network`.
- Veröffentlichen Sie den Port dann nicht mehr im Netz (`INTELLIGENCE_PORT=127.0.0.1:8081` oder der Proxy im `arsse-network`, siehe unten). Das ist Pflicht: Bei einem im Netz veröffentlichten Port können auch Anfragen anderer Geräte vom Gateway kommen (z.B. per IPv6 über Dockers Port-Proxy), und die könnten den Header selbst schicken.

**DNS-Rebinding:** Eine fremde Webseite kann ihren Namen auf die IP-Adresse Ihres Servers umbiegen und die Top Stories dann über Ihren Browser auslesen. `WEB_ALLOWED_HOSTS=tower.local,192.168.1.10` beantwortet nur Anfragen an diese Namen (`localhost` und `127.0.0.1` gehen immer), alle anderen mit `400`. Platzhalter wie `*.example.com` gibt es nicht – jeden Namen einzeln eintragen; IPv6-Adressen gehen mit oder ohne eckige Klammern.

Unabhängig davon senden die Top Stories eine strikte Content-Security-Policy (kein JavaScript, keine fremden Inhalte, nicht in Frames einbettbar) und weitere Sicherheits-Header; Anfragen, die etwas verändern („Story gelesen“), nehmen sie nur von der eigenen Seite an (Schutz vor CSRF): Ohne Anmeldung ist das der einzige Schutz davor, dass eine fremde Webseite in Ihrem Browser Stories als gelesen markiert.

### Eigener Miniflux-Benutzer für die Top Stories

API-Keys von Miniflux gelten ohne Einschränkung für ihren Benutzer: Mit dem Key des Admins lassen sich Benutzer anlegen und Passwörter ändern. Der Intelligence-Container verarbeitet fremdes HTML aus den Feeds und braucht nur Lese- und Gelesen-markieren-Rechte. Empfohlen:

1. Als Admin unter *Einstellungen > Benutzer > Benutzer anlegen* einen Benutzer ohne Admin-Rechte anlegen, z.B. `leser`
2. Als `leser` anmelden und die Feeds abonnieren – bestehende Abos übertragen *Abonnements > Exportieren* (als Admin) und *Abonnements > Importieren* (als `leser`) per OPML-Datei
3. Als `leser` unter *Einstellungen > API-Schlüssel* einen Key erzeugen und als `MINIFLUX_API_KEY` eintragen
4. Zum Lesen `leser` verwenden, den Admin nur zur Verwaltung

Die Top Stories zeigen die Feeds und den Gelesen-Status des Benutzers, dem der Key gehört. Gehört er einem Admin, warnt der Dienst beim Start im Log. Statt `MINIFLUX_API_KEY` geht auch `MINIFLUX_API_KEY_FILE` (Pfad zu einer Datei im Container, siehe nächster Abschnitt) – dann taucht der Key nicht in `docker inspect` auf.

### Passwörter und API-Key als Datei (Docker Secret)

`WEB_PASSWORD_FILE` und `MINIFLUX_API_KEY_FILE` nennen eine Datei *im Container*; Docker muss sie erst hineinlegen. Am einfachsten mit einer `docker-compose.override.yml` neben der `docker-compose.yml` (wird automatisch mitgelesen):

```yaml
services:
  intelligence:
    secrets:
      - web_password
      - miniflux_api_key

secrets:
  web_password:
    file: ./secrets/web_password
  miniflux_api_key:
    file: ./secrets/miniflux_api_key
```

Dazu in `.env` `WEB_PASSWORD_FILE=/run/secrets/web_password` bzw. `MINIFLUX_API_KEY_FILE=/run/secrets/miniflux_api_key`. Der Dienst liest die Dateien erst nach dem Wechsel zu `PUID:PGID`, und ohne Docker Swarm behält Docker Besitzer und Rechte der Datei auf dem Server – sie muss also für `PUID` lesbar sein, sonst bricht der Container beim Start ab (`Permission denied` im Log):

```bash
mkdir -p secrets
printf '%s' 'ein-langes-passwort' > secrets/web_password
chmod 400 secrets/*
sudo chown 1000:1000 secrets/*   # PUID:PGID, Unraid: 99:100
```

### Reverse Proxy (HTTPS)

Für den Zugriff von außen gehört ein Reverse Proxy mit HTTPS vor beide Dienste. Die Top Stories brauchen einen eigenen Hostnamen – unter einem Unterpfad wie `/stories/` funktionieren ihre Links nicht.

```nginx
# Miniflux
server {
    listen 443 ssl;
    http2 on;  # nginx vor 1.25.1: stattdessen "listen 443 ssl http2;"
    server_name news.example.com;

    ssl_certificate /etc/letsencrypt/live/news.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/news.example.com/privkey.pem;

    # Prometheus-Metriken nie nach außen geben
    location = /metrics { return 404; }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Überschreiben statt anhängen: Miniflux nimmt die erste Adresse,
        # und die käme sonst vom Client ($proxy_add_x_forwarded_for)
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Top Stories
server {
    listen 443 ssl;
    http2 on;  # nginx vor 1.25.1: stattdessen "listen 443 ssl http2;"
    server_name stories.example.com;

    ssl_certificate /etc/letsencrypt/live/stories.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/stories.example.com/privkey.pem;

    # Anmeldung am Proxy (htpasswd -c /etc/nginx/arsse.htpasswd leser) –
    # oder stattdessen WEB_AUTH_MODE=basic in .env
    auth_basic "aRSSe";
    auth_basic_user_file /etc/nginx/arsse.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Nur mit WEB_AUTH_MODE=proxy: angemeldeten Benutzer weitergeben
        #proxy_set_header Remote-User $remote_user;
    }

    # Manifest und Icons für den Startbildschirm: Android holt die Icons
    # teils ohne Zugangsdaten ab (keine Inhalte aus Ihren Feeds)
    location /static/ {
        auth_basic off;
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Dazu in `.env`:

```bash
BASE_URL=https://news.example.com
# Nur noch über den Proxy erreichbar
MINIFLUX_PORT=127.0.0.1:8080
INTELLIGENCE_PORT=127.0.0.1:8081
# Adresse, von der die Anfragen des Proxys bei Miniflux ankommen (siehe unten)
TRUSTED_PROXIES=172.18.0.1/32
WEB_ALLOWED_HOSTS=stories.example.com
# Nur mit WEB_AUTH_MODE=proxy: dieselbe Adresse wie TRUSTED_PROXIES
#WEB_TRUSTED_PROXIES=172.18.0.1/32
```

- **`TRUSTED_PROXIES`:** Erst wenn Miniflux dem Proxy vertraut, glaubt es dessen `X-Forwarded-Proto: https` und markiert den Sitzungs-Cookie als `Secure`. Läuft nginx direkt auf dem Server, kommen seine Anfragen über die Docker-Portweiterleitung vom Gateway des `arsse-network`: `docker network inspect arsse-network -f '{{(index .IPAM.Config 0).Gateway}}'`. Danach die Anmeldung nur noch über die HTTPS-Adresse – den `Secure`-Cookie nimmt der Browser über `http://<server-ip>:8080` nicht an.
- **Proxy als Container** (SWAG, Nginx Proxy Manager): `localhost` ist dort der Proxy-Container selbst. Hängen Sie ihn ans `arsse-network` (`docker network connect arsse-network swag`) und verwenden Sie `proxy_pass http://arsse-miniflux:8080` bzw. `http://arsse-intelligence:8081`; dann brauchen Miniflux und Top Stories gar keine veröffentlichten Ports (`127.0.0.1:…` genügt). `TRUSTED_PROXIES` bzw. `WEB_TRUSTED_PROXIES` ist dann die Adresse des Proxys im `arsse-network`: `docker inspect -f '{{with index .NetworkSettings.Networks "arsse-network"}}{{.IPAddress}}{{end}}' swag`. Diese Adresse kann sich ändern, wenn der Proxy-Container neu erstellt wird (z.B. bei Updates unter Unraid) – danach erneut prüfen: Mit einer veralteten Adresse antworten die Top Stories (bei `WEB_AUTH_MODE=proxy`) mit `403`, und Miniflux setzt den `Secure`-Cookie stillschweigend nicht mehr. Achten Sie darauf, dass der Proxy den `Host`-Header weitergibt und `X-Forwarded-For` überschreibt statt anhängt (sonst kann ein Client Miniflux eine falsche IP-Adresse unterschieben, z.B. für fail2ban).
- **Metriken:** `/metrics` von Miniflux ist standardmäßig aus (`METRICS_COLLECTOR=0`). Wer es für Prometheus einschaltet, setzt zusätzlich `METRICS_USERNAME` und `METRICS_PASSWORD` – über den Proxy kommen alle Anfragen aus einem 172er-Netz, `METRICS_ALLOWED_NETWORKS` allein schützt dann nicht.
- **Zwei Faktoren:** z.B. Authelia oder Authentik am Reverse Proxy; die Top Stories übernehmen den dort angemeldeten Benutzer mit `WEB_AUTH_MODE=proxy`.

### Container

`docker-compose.yml` beschränkt die Container auf das Nötige:

- **Intelligence Layer:** schreibgeschütztes Dateisystem (beschreibbar sind nur `/app/data` und `/tmp` im Speicher), keine Linux-Berechtigungen außer denen, die der Start als root braucht, um das Datenverzeichnis an `PUID:PGID` zu übergeben (`CHOWN`, `DAC_OVERRIDE`, `SETUID`, `SETGID`); danach läuft der Dienst ohne jede Berechtigung und kann seinen eigenen Code nicht ändern. Dazu `no-new-privileges` und Grenzen für Speicher (1 GB, genug für `max_entries: 5000`), CPU (1 Kern) und Prozesse (128).
- **Miniflux:** schreibgeschützt, ohne Berechtigungen, `no-new-privileges`.
- **PostgreSQL:** `no-new-privileges` (der Start braucht root, um die Rechte des Datenverzeichnisses zu setzen).
- **Logs:** Docker behält je Container höchstens 3 × 10 MB (`x-logging`); `logging.file` in `config.yaml` rotiert bei 5 MB (drei ältere Dateien).

Grenzen ändern Sie in einer `docker-compose.override.yml` (siehe [Konfiguration](#konfiguration)). Die Unraid-Templates setzen dieselben Einschränkungen unter *Extra Parameters*.

## Unraid ohne Compose

Ohne das Compose-Manager-Plugin laufen die drei Container auch über die Docker-Seite von Unraid. Die Templates liegen in `unraid/`; der Intelligence Layer kommt als fertiges Image aus der GitHub Container Registry.

Das Template nutzt `ghcr.io/zwaetschge/arsse-intelligence:latest`, das erst mit der ersten Release-Version (Tag `vX.Y.Z`) erscheint. Gibt es noch keine, tragen Sie unter *Repository* `…:edge` ein (Stand des Hauptzweigs) oder nehmen den Weg über Compose, der das Image notfalls selbst baut.

1. **Netzwerk anlegen** (einmalig, im Unraid-Terminal): `docker network create arsse`. Im Standardnetzwerk `bridge` finden sich Container nicht über ihre Namen. Außerdem unter *Settings > Docker* (erweiterte Ansicht, Docker dafür kurz anhalten) *Preserve user defined networks* auf *Yes* stellen – sonst löscht Unraid das Netzwerk beim nächsten Neustart von Docker oder des Servers, und die Container starten nicht mehr („network arsse not found“).
2. **Templates holen:**
   ```bash
   cd /boot/config/plugins/dockerMan/templates-user
   wget -O my-arsse-miniflux.xml https://raw.githubusercontent.com/zwaetschge/aRSSe/HEAD/unraid/miniflux.xml
   wget -O my-arsse-intelligence.xml https://raw.githubusercontent.com/zwaetschge/aRSSe/HEAD/unraid/arsse-intelligence.xml
   ```
3. **PostgreSQL:** *Docker > Add Container* ohne Template: Name `arsse-db`, Repository `postgres:15-alpine`, Network Type `Custom: arsse`, Variablen `POSTGRES_USER=miniflux`, `POSTGRES_PASSWORD=<Passwort>`, `POSTGRES_DB=miniflux`, Pfad `/var/lib/postgresql/data` → `/mnt/user/appdata/arsse/postgresql` (besser auf dem Cache, z.B. `/mnt/cache/appdata/…`), Extra Parameters `--security-opt=no-new-privileges:true`.
4. **Miniflux:** *Add Container*, Template `arsse-miniflux`. In `DATABASE_URL` `CHANGE_ME` durch das Postgres-Passwort ersetzen, `ADMIN_PASSWORD` und `BASE_URL` (z.B. `http://192.168.1.10:8080`) eintragen.
5. **API-Key:** In Miniflux einen Benutzer ohne Admin-Rechte anlegen, als dieser Feeds abonnieren und unter *Einstellungen > API-Schlüssel* einen Key erzeugen (siehe [Eigener Miniflux-Benutzer](#eigener-miniflux-benutzer-für-die-top-stories)).
6. **Intelligence Layer:** *Add Container*, Template `arsse-intelligence`: `MINIFLUX_API_KEY`, `MINIFLUX_PUBLIC_URL` (wie `BASE_URL`) eintragen; `PUID` 99 und `PGID` 100 passen für Unraid. Die Top Stories erscheinen unter `http://<unraid-ip>:8081`, eigene Einstellungen stehen in `/mnt/user/appdata/arsse/intelligence/config.yaml`. Die weiteren Variablen (Zugriffsschutz, Zeitfenster, Mindestzahl der Quellen …) zeigt *Show more settings*.

Updates holt Unraid wie bei anderen Containern (*Check for Updates*). Für Backups hält das Plugin *Appdata Backup* die Container an und sichert `/mnt/user/appdata/arsse` samt Datenbanken und `config.yaml` in einem konsistenten Zustand.

## Backup und Updates

### Backup

`scripts/backup.sh` sichert den laufenden Stack, ohne ihn anzuhalten, nach `${DATA_PATH}/backups/<Datum_Uhrzeit>/` (anderes Ziel: `BACKUP_DIR` in `.env` oder als Argument):

| Datei | Inhalt |
|-------|--------|
| `miniflux.dump` | Miniflux-Datenbank (`pg_dump -Fc`: Benutzer, Feeds, Artikel, Lesestatus) |
| `feeds.opml` | Abonnements des Benutzers von `MINIFLUX_API_KEY`, für jeden Feedreader |
| `arsse.db` | Story-Datenbank (SQLite-Online-Backup, konsistent auch während eines Laufs) |
| `config.yaml` | Eigene Einstellungen des Intelligence Layers (falls vorhanden) |
| `env` | Kopie der `.env` mit allen Passwörtern (Rechte 600) |

`miniflux.dump` und `env` sind Pflicht; scheitert einer der anderen Teile (z.B. läuft der Intelligence Layer gerade nicht), meldet das Skript eine Warnung und behält die Sicherung ohne ihn. Sicherungen, die älter als `BACKUP_KEEP_DAYS` Tage sind (Standard 14), löscht das Skript danach – nur seine eigenen (Verzeichnisname mit Zeitstempel und Markierungsdatei `.arsse-backup`), andere Ordner in `BACKUP_DIR` bleiben unberührt. Ein relatives `BACKUP_DIR` in `.env` gilt wie `DATA_PATH` ab dem Projektverzeichnis, ein relatives Argument ab dem aktuellen Verzeichnis. Die Sicherungen enthalten Passwörter – legen Sie eine Kopie auf ein anderes Laufwerk. Auf Unraid planen Sie das Skript mit dem Plugin *User Scripts*, z.B. täglich:

```bash
#!/bin/bash
/mnt/user/appdata/aRSSe/scripts/backup.sh   # Pfad Ihres Repositorys
```

Liegt die Compose-Datei nicht im Repository (z.B. im Compose Manager), nennen Sie sie mit `COMPOSE_FILE=/pfad/docker-compose.yml`, die `.env` mit `ENV_FILE=/pfad/.env` und den Projektnamen mit `COMPOSE_PROJECT_NAME=…`.

**Wiederherstellen** (mit dem Verzeichnis einer Sicherung als `B`):

```bash
cp "$B/env" .env                    # nur falls .env verloren ist
docker compose stop miniflux intelligence
docker compose exec -T db pg_restore -U miniflux -d miniflux --clean --if-exists < "$B/miniflux.dump"
# Story-Datenbank (optional: ohne sie baut der nächste Lauf die Stories neu auf)
rm -f ${DATA_PATH}/intelligence/arsse.db-wal ${DATA_PATH}/intelligence/arsse.db-shm
cp "$B/arsse.db" ${DATA_PATH}/intelligence/arsse.db
cp "$B/config.yaml" ${DATA_PATH}/intelligence/config.yaml
docker compose up -d
```

Auf einem neuen Server zuerst nur die Datenbank starten (`docker compose up -d --wait db`), dann `pg_restore` ohne `--clean` und erst danach `docker compose up -d` – sonst legt Miniflux schon leere Tabellen an. Die Rechte der kopierten Dateien setzt der Intelligence-Container beim Start selbst.

### Updates

```bash
scripts/backup.sh      # Miniflux migriert seine Datenbank nur vorwärts
git pull               # docker-compose.yml, Skripte, Templates
docker compose pull    # neue Images (Miniflux, Intelligence Layer)
docker compose up -d
```

Solange es noch keine Release-Version gibt (oder Sie das Image selbst bauen), meldet `docker compose pull` das Intelligence-Image als nicht gefunden. Beim ersten Mal baut `docker compose up -d` es dann aus dem lokalen Code; danach findet Compose dieses lokal gebaute Image und baut es nicht neu. Aktualisieren Sie in diesem Fall mit `docker compose up -d --build`, oder stellen Sie `ARSSE_VERSION=edge` ein, sobald der Hauptzweig veröffentlicht wird.

**Erstes Update von einer älteren Version** (ohne `scripts/backup.sh` und ohne `${DATA_PATH}/intelligence/config.yaml`):

1. Wer `intelligence/config.yaml` selbst geändert hat, übernimmt die Änderungen zuerst wie unter [Konfiguration](#konfiguration) beschrieben (sonst verweigert `git pull` das Update).
2. `git pull`, dann `scripts/backup.sh` – das Skript sichert auch den noch laufenden alten Container.
3. `scripts/setup.sh` ausführen (legt die eigene `config.yaml` an und übernimmt übrig gebliebene Änderungen), dann `docker compose pull` und `docker compose up -d`.
4. Im Log (`docker compose logs intelligence`) prüfen, ob noch Einstellungen aus `./intelligence/config.yaml` genannt werden (`differs from every shipped version of the reference`); sie gelten vorerst weiter, gehören aber nach `${DATA_PATH}/intelligence/config.yaml`.

### PostgreSQL-Hauptversion wechseln

Eine neue Hauptversion (z.B. 15 → 17) startet nicht mit den Datendateien der alten; einfach das Image zu ändern lässt die Datenbank in einer Neustart-Schleife hängen. Der Weg führt über Dump und Restore:

```bash
scripts/backup.sh                  # B = das neue Verzeichnis unter ${DATA_PATH}/backups
docker compose stop miniflux intelligence db
mv ${DATA_PATH}/postgresql ${DATA_PATH}/postgresql-15.bak
# in .env: POSTGRES_MAJOR=17
#   ab 18 zusätzlich POSTGRES_MOUNT=/var/lib/postgresql (neue Verzeichnisstruktur)
docker compose up -d --wait db
docker compose exec -T db pg_restore -U miniflux -d miniflux < "$B/miniflux.dump"
docker compose up -d
```

Läuft alles, kann `postgresql-15.bak` weg. Zurück geht es, indem Sie das alte Verzeichnis zurückbenennen und `POSTGRES_MAJOR` wieder auf 15 setzen.

## Troubleshooting

### Clustering funktioniert nicht

1. Prüfen Sie die Statuszeile der Startseite („Fehler seit …“) oder `curl http://<unraid-ip>:8081/healthz` (`last_error`: Grund des letzten Fehlschlags, `last_stats`: Statistik des letzten Laufs)
2. Prüfen Sie die Logs: `docker logs arsse-intelligence`
3. Stories entstehen erst, wenn mehrere Feeds über dasselbe Thema berichten – abonnieren Sie mehrere überlappende Quellen
4. Zu wenige oder zu große Stories: `threshold` mit `evaluate.py` kalibrieren (höher = größere, aber unschärfere Stories); zu viele zufällige Zweier-Stories: `min_pair_similarity` erhöhen
5. `Cannot open story database`: Der Container konnte `${DATA_PATH}/intelligence` nicht an `PUID:PGID` übergeben (z.B. NFS-Freigabe mit `root_squash`, oder eine ältere `docker-compose.yml` mit `user:`) – dann `chown PUID:PGID` auf das Verzeichnis selbst ausführen
6. Alle Links der Top Stories zeigen auf `localhost` oder die falsche Adresse: `BASE_URL` in `.env` setzen und `docker compose up -d` ausführen
7. `Datenbank stammt von neuerer aRSSe-Version`: Das Image ist älter als die Datenbank (z.B. nach einem Rückschritt auf eine ältere Version). Entweder wieder die neuere Version starten, ein Backup von `arsse.db` aus der Zeit vor dem Update einspielen oder `${DATA_PATH}/intelligence/arsse.db*` löschen – die Datenbank ist nur ein Zwischenspeicher und wird beim nächsten Lauf aus Miniflux neu aufgebaut. Dabei ändern sich die Story-IDs, und aRSSe vergisst, welche Duplikate es schon als gelesen markiert hat: Duplikate im aktuellen Zeitfenster, die Sie wieder auf ungelesen gesetzt haben, werden einmal erneut als gelesen markiert
8. Der Container startet nicht und das Log nennt eine Einstellung (z.B. `scheduling.batch_size must be between 1 and 1000`): den Wert in `${DATA_PATH}/intelligence/config.yaml` bzw. `.env` korrigieren
9. Top Stories antworten mit `400`: Der aufgerufene Hostname fehlt in `WEB_ALLOWED_HOSTS`. Mit `403` bei `WEB_AUTH_MODE=proxy`: Die Anfrage kam nicht von einer Adresse in `WEB_TRUSTED_PROXIES` oder ohne Benutzer-Header – das Log nennt die Adresse

### E-Ink-Darstellung fehlerhaft

1. Deaktivieren Sie JavaScript-Animationen im Browser
2. Aktivieren Sie "A2 Refresh Mode" auf dem Gerät
3. Prüfen Sie, ob das Custom CSS korrekt geladen wurde

### Miniflux startet nicht

1. Prüfen Sie PostgreSQL: `docker logs arsse-db`
2. Warten Sie auf den Health Check (ca. 30 Sekunden)
3. Prüfen Sie die DATABASE_URL in `.env`

## Entwicklung

```bash
cd intelligence
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.txt   # dieselben Versionen wie im Image
pip install -r requirements-dev.txt
python fetch_nltk_data.py
pytest
```

Abhängigkeiten ändern: `requirements.in` anpassen und `requirements.txt` neu erzeugen (nicht von Hand ändern; Dependabot aktualisiert beide Dateien) (Python 3.11, alle Pakete müssen als Wheel für amd64 und arm64 vorliegen):

```bash
pip-compile --generate-hashes --output-file=requirements.txt requirements.in
```

Dependabot schlägt wöchentlich Updates für Python-Pakete, Basis-Image, Miniflux und die GitHub Actions vor; jeder Vorschlag durchläuft alle Tests. PostgreSQL verfolgt er nicht, weil `docker-compose.yml` die Version über `POSTGRES_MAJOR` wählt (Variablen im Image-Namen überspringt Dependabot): Kleine Updates bringt `docker compose pull` innerhalb der Hauptversion, den Wechsel der Hauptversion machen Sie von Hand (siehe [Backup und Updates](#backup-und-updates)). Dasselbe gilt für das eigene Image (`ARSSE_VERSION`).

**Release:** Jeder Push auf den Hauptzweig veröffentlicht nach bestandenen Tests `ghcr.io/zwaetschge/arsse-intelligence:edge` und `:sha-<commit>`, ein Tag `vX.Y.Z` zusätzlich `:X.Y.Z`, `:X.Y` und `:latest` (`.github/workflows/release.yml`, amd64 und arm64). Pull Requests veröffentlichen nie etwas. `:latest` gibt es erst mit dem ersten Tag: Nach dem Zusammenführen einmal `git tag v1.0.0 && git push origin v1.0.0` ausführen, damit `docker compose pull` und das Unraid-Template ein Image finden. Nach der ersten Veröffentlichung ist das Paket in der GitHub Container Registry noch privat: unter *Package settings* einmalig auf *Public* stellen, sonst scheitern `docker compose pull` und das Unraid-Template ohne Anmeldung.

Integrationstest mit echtem Miniflux (braucht Docker, kollidiert nicht mit einem laufenden Stack: eigene Namen, Ports und ein eigenes Image `arsse-it-intelligence:test`, immer aus dem lokalen Code gebaut):

```bash
scripts/integration-test.sh
# wie Unraid: Daten für 99:100, Verzeichnis legt Docker an
IT_PUID=99 IT_PGID=100 IT_PRECREATE_DATA=0 scripts/integration-test.sh
```

`scripts/test-setup.sh` prüft `setup.sh`, `scripts/test-backup.sh` prüft `backup.sh` mit einem Docker-Stub (ohne Docker; `PYTHON=` wählt das Python mit den Abhängigkeiten). `scripts/check-unraid-templates.py` vergleicht die Unraid-Templates mit `docker-compose.yml` (braucht nur die Docker-CLI).

## Lizenz

MIT License - Siehe [LICENSE](LICENSE) für Details.

## Danksagungen

- [Miniflux](https://miniflux.app/) - Der minimalistische RSS-Reader
- [Scikit-Learn](https://scikit-learn.org/) - Machine Learning für Python
- Die Unraid-Community für Inspiration und Support

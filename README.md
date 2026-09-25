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

- Unraid 6.x oder höher
- Docker und Docker Compose
- Mindestens 4 GB verfügbarer RAM
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
| `/healthz` | `200`, solange der letzte erfolgreiche Lauf weniger als drei Intervalle zurückliegt (immer ohne Anmeldung) |
| `/static/…` | Manifest und Icons für den Startbildschirm (immer ohne Anmeldung, Icons holen Browser und Android teils ohne Zugangsdaten) |

Die Seiten sind für E-Ink gebaut: kurze Seiten statt langem Scrollen, nur absolute Uhrzeiten („Stand 18:32“, „Mi 14:53“ – relative Angaben wie „vor 5 Min.“ stimmen auf einem stehenden Bildschirm bald nicht mehr), jede Quelle zuerst mit einem Artikel statt mehrerer aus demselben Feed, und Links, die als ganze Zeile mindestens 44 px hoch antippbar sind. Die Uhrzeiten gelten in der Zeitzone `TZ` aus `.env` (Standard Europe/Berlin) oder `web.timezone` in `config.yaml`.

**Gelesen:** Eine Story, deren Artikel im Zeitfenster alle gelesen sind, verschwindet von der Startseite (die Statuszeile bietet „N gelesene zeigen“). „Story gelesen (N)“ markiert mit einem Tipp alle ungelesenen Artikel der Story in Miniflux als gelesen – statt neun fast gleicher Meldungen einzeln. Kommen danach neue Artikel hinzu, erscheint die Story wieder mit „N neu“. Was in Miniflux selbst gelesen wird, übernehmen die Top Stories alle 5 Minuten (`scheduling.status_sync_minutes`). Der Knopf braucht den `MINIFLUX_API_KEY`; ohne Anmeldung (`WEB_AUTH_MODE=none`) kann jeder, der den Port erreicht, Stories als gelesen markieren – fremde Webseiten können es nicht (siehe „Absicherung“).

**Rubriken:** Politik, Sport, Technik, Regional usw. kommen aus der Kategorie des Feeds in Miniflux. Für Feeds in der Standardkategorie „All“ entscheidet der Pfad der Artikel-URL (`tagesschau.de/ausland/…` → Politik, `…/sport/…` → Sport; `web.path_sections` in `config.yaml`); eine Story gehört zur Rubrik der meisten ihrer Artikel.

Links führen in Miniflux (`BASE_URL`), damit Gelesen-Status und Volltext erhalten bleiben. Zeigt `BASE_URL` auf `localhost`, verwenden die Links stattdessen die Adresse, unter der die Top Stories aufgerufen wurden, mit `MINIFLUX_PORT` (beim Start erscheint dazu eine Warnung im Log).

**Konfiguration:**

```yaml
# intelligence/config.yaml
clustering:
  threshold: 0.75                # max. durchschnittliche Kosinus-Distanz einer Story
  min_pair_similarity: 0.30      # Mindest-Ähnlichkeit einer Story aus nur zwei Artikeln
  topic_threshold: 0.9           # Themen für „Mehr zum Thema“ (0 = aus)
  stemming: true
  # noise_title_patterns: Liste von Titelmustern (Werbung, Podcasts, Liveblogs …),
  # die keine Schlagzeile werden, solange die Story andere Artikel hat

deduplication:
  threshold: 0.85                # Duplikat-Schwellenwert (Text ohne Titel)
  min_body_tokens: 25            # kürzere Texte verschiedener Feeds sind nie Duplikate (außer gleiche URL und gleicher Titel)
  duplicate_action: "mark_read"  # oder "none"
  mark_read_scope: "visible"     # oder "all" (auch Stories, die die Startseite nicht zeigt)

scheduling:
  interval_minutes: 30           # Ausführungsintervall
  status_sync_minutes: 5         # in Miniflux Gelesenes übernehmen (0 = aus)

web:
  page_size: 10                  # Stories pro Seite (WEB_PAGE_SIZE)
  max_stories: 100               # insgesamt, über alle Seiten
  exclude_patterns: ['^Wetter\b']  # Stories nur aus solchen Titeln nicht anzeigen
```

Umgebungsvariablen aus `.env` (z.B. `CLUSTERING_THRESHOLD`) überschreiben `config.yaml`; auskommentierte bzw. leere Variablen tun das nicht.

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
│   ├── Dockerfile          # Python-Container
│   ├── requirements.txt    # Python-Abhängigkeiten
│   ├── entrypoint.py       # Container-Start: Datenrechte setzen, Root-Rechte abgeben
│   ├── news_clustering.py  # Clustering-Logik und Einstiegspunkt
│   ├── evaluate.py         # Clustering-Schwelle an eigenen Feeds kalibrieren und messen
│   ├── eval/               # Mess-Werkzeuge: Korpus-Export, Metriken, Wiederholung, gold.json
│   ├── store.py            # SQLite-Story-Datenbank
│   ├── web.py              # Top-Stories-Oberfläche
│   ├── templates/          # HTML-Templates
│   ├── static/             # Manifest und Icons für den Startbildschirm
│   ├── config.py           # Konfigurationsmodul
│   ├── config.yaml         # Service-Konfiguration
│   └── tests/              # pytest-Suite
├── css/
│   ├── eink-theme.css      # E-Ink-Theme für Miniflux
│   └── color-theme.css     # Farb-Theme für Miniflux
├── unraid/
│   └── miniflux.xml        # Unraid CA Template
├── scripts/
│   ├── setup.sh            # Initialisierungsskript
│   ├── make-icons.py       # Erzeugt die Icons in intelligence/static
│   ├── test-setup.sh       # Test für setup.sh (ohne Docker)
│   └── integration-test.sh # Stack-Test gegen echtes Miniflux
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

Ohne weitere Einstellungen sind Miniflux (Port 8080) und die Top Stories (Port 8081) im ganzen Heimnetz erreichbar. Miniflux verlangt eine Anmeldung, die Top Stories nicht: Wer Port 8081 erreicht, sieht Ihre Abos und was Sie gelesen haben – der Gast im WLAN ebenso wie ein Gerät im Netz. Beim Start steht dazu eine Warnung im Log.

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

## Troubleshooting

### Clustering funktioniert nicht

1. Prüfen Sie den Status: `curl http://<unraid-ip>:8081/healthz` (enthält die Statistik des letzten Laufs)
2. Prüfen Sie die Logs: `docker logs arsse-intelligence`
3. Stories entstehen erst, wenn mehrere Feeds über dasselbe Thema berichten – abonnieren Sie mehrere überlappende Quellen
4. Zu wenige oder zu große Stories: `threshold` mit `evaluate.py` kalibrieren (höher = größere, aber unschärfere Stories); zu viele zufällige Zweier-Stories: `min_pair_similarity` erhöhen
5. `Cannot open story database`: Der Container konnte `${DATA_PATH}/intelligence` nicht an `PUID:PGID` übergeben (z.B. NFS-Freigabe mit `root_squash`, oder eine ältere `docker-compose.yml` mit `user:`) – dann `chown PUID:PGID` auf das Verzeichnis selbst ausführen
6. Alle Links der Top Stories zeigen auf `localhost` oder die falsche Adresse: `BASE_URL` in `.env` setzen und `docker compose up -d` ausführen
7. `Datenbank stammt von neuerer aRSSe-Version`: Das Image ist älter als die Datenbank (z.B. nach einem Rückschritt auf eine ältere Version). Entweder wieder die neuere Version starten, ein Backup von `arsse.db` aus der Zeit vor dem Update einspielen oder `${DATA_PATH}/intelligence/arsse.db*` löschen – die Datenbank ist nur ein Zwischenspeicher und wird beim nächsten Lauf aus Miniflux neu aufgebaut. Dabei ändern sich die Story-IDs, und aRSSe vergisst, welche Duplikate es schon als gelesen markiert hat: Duplikate im aktuellen Zeitfenster, die Sie wieder auf ungelesen gesetzt haben, werden einmal erneut als gelesen markiert
8. Der Container startet nicht und das Log nennt eine Einstellung (z.B. `scheduling.batch_size must be between 1 and 1000`): den Wert in `config.yaml` bzw. `.env` korrigieren
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
pip install -r requirements-dev.txt
python -c "import nltk; nltk.download('stopwords')"
pytest
```

Integrationstest mit echtem Miniflux (braucht Docker, kollidiert nicht mit einem laufenden Stack):

```bash
scripts/integration-test.sh
# wie Unraid: Daten für 99:100, Verzeichnis legt Docker an
IT_PUID=99 IT_PGID=100 IT_PRECREATE_DATA=0 scripts/integration-test.sh
```

`scripts/test-setup.sh` prüft `setup.sh` mit einem Docker-Stub (ohne Docker).

## Lizenz

MIT License - Siehe [LICENSE](LICENSE) für Details.

## Danksagungen

- [Miniflux](https://miniflux.app/) - Der minimalistische RSS-Reader
- [Scikit-Learn](https://scikit-learn.org/) - Machine Learning für Python
- Die Unraid-Community für Inspiration und Support

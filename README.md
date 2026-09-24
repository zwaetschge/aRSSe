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

Erzeugen Sie in Miniflux unter *Einstellungen > API-Schlüssel* einen Key, tragen Sie ihn als `MINIFLUX_API_KEY` in `.env` ein und starten Sie `docker compose up -d intelligence`. Die Top Stories sind dann unter `http://<unraid-ip>:8081` erreichbar.

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

1. **Extraction**: Abruf aller Artikel der letzten 24 Stunden via Miniflux API (gelesen und ungelesen, seitenweise nach Artikel-ID – auch bei gleichen Veröffentlichungszeiten kommt kein Artikel doppelt)
2. **Preprocessing**: HTML entfernen, Normalisierung, Stopwords, Stemming (Snowball)
3. **Vectorization**: TF-IDF über Uni- und Bigramme
4. **Clustering**: Agglomeratives Clustering (Average Linkage) gruppiert Artikel zum selben Ereignis zu einer *Story*
5. **Deduplication**: Near-Duplicates (z.B. identische Agenturmeldungen verschiedener Feeds, verglichen ohne Titel) und doppelt gelieferte Artikel innerhalb einer Story
6. **Persistence**: Stories landen in einer lokalen SQLite-Datenbank (`data/intelligence/arsse.db`) mit über Läufe hinweg stabilen IDs; Duplikate werden optional in Miniflux als gelesen markiert – standardmäßig nur in Stories, die die Startseite zeigt (doppelt gelieferte Artikel überall), und jedes nur einmal: Wer ein Duplikat wieder auf ungelesen setzt, behält es ungelesen. Artikel im Zeitfenster, die es in Miniflux nicht mehr gibt (z.B. nach „Verlauf leeren“ oder dem Abbestellen eines Feeds), verschwinden beim nächsten Lauf aus ihren Stories; ältere Artikel unter „Frühere Berichte“ verlinken deshalb auf das Original beim Anbieter. Nach einem Update passt der Service das Schema der Datenbank beim Start selbst an

Die Miniflux-API kann keine Tags oder eigenen Metadaten schreiben – deshalb bringt der Service eine eigene, JavaScript-freie Oberfläche mit:

| Pfad | Inhalt |
|------|--------|
| `/` | Top Stories aus mindestens zwei Feeds, gerankt nach Anzahl der Quellen und Aktualität – gezählt wird nur, was im Zeitfenster (24 Stunden) erschienen ist |
| `/story/<id>` | Alle Artikel einer Story im Zeitfenster, darunter bis zu 20 ältere als „Frühere Berichte“ (mit Link auf das Original) |
| `/api/stories` | Dieselben Daten als JSON |
| `/healthz` | `200`, solange der letzte erfolgreiche Lauf weniger als drei Intervalle zurückliegt |

Links führen in Miniflux (`BASE_URL`), damit Gelesen-Status und Volltext erhalten bleiben. Zeigt `BASE_URL` auf `localhost`, verwenden die Links stattdessen die Adresse, unter der die Top Stories aufgerufen wurden, mit `MINIFLUX_PORT` (beim Start erscheint dazu eine Warnung im Log).

**Konfiguration:**

```yaml
# intelligence/config.yaml
clustering:
  threshold: 0.75                # max. durchschnittliche Kosinus-Distanz einer Story
  stemming: true

deduplication:
  threshold: 0.85                # Duplikat-Schwellenwert (Text ohne Titel)
  min_body_tokens: 25            # kürzere Texte verschiedener Feeds sind nie Duplikate
  duplicate_action: "mark_read"  # oder "none"
  mark_read_scope: "visible"     # oder "all" (auch Stories, die die Startseite nicht zeigt)

scheduling:
  interval_minutes: 30           # Ausführungsintervall
```

Umgebungsvariablen aus `.env` (z.B. `CLUSTERING_THRESHOLD`) überschreiben `config.yaml`; auskommentierte bzw. leere Variablen tun das nicht.

**Schwelle kalibrieren:** Der Standardwert 0.75 ist an ~500 echten Artikeln aus 13 deutschen Nachrichtenfeeds gemessen (Kurztexte aus RSS, kein Volltext). Mit anderen Feeds oder aktiviertem Volltext-Crawler lohnt ein Vergleich – das Werkzeug liest nur und schreibt nichts:

```bash
docker compose exec intelligence python evaluate.py 0.65 0.7 0.75 0.8
```

Es zeigt je Schwelle die Anzahl der Stories sowie die größten und einige zufällige Stories. Große Stories mit gemischten Themen = Schwelle zu hoch; viele zusammengehörige Artikel außerhalb von Stories = zu niedrig.

**Lokale Feeds:** Miniflux ruft seit 2.3 standardmäßig keine Feeds aus privaten Netzen ab. Für Feeds im Heimnetz `FETCHER_ALLOW_PRIVATE_NETWORKS=1` beim Miniflux-Container setzen.

### E-Ink Optimierung

Das Custom CSS für E-Ink-Displays berücksichtigt:

- **Keine Animationen**: Vermeidung von Ghosting
- **Hoher Kontrast**: Reines Schwarz auf Weiß
- **Serifen-Typografie**: Bessere Lesbarkeit
- **Große Touch-Targets**: Mobile-freundliche Bedienung
- **Pagination statt Scrolling**: Weniger Refreshes

Beide Themes (`css/eink-theme.css`, `css/color-theme.css`) werden in Miniflux unter *Einstellungen > Benutzerdefiniertes CSS* eingefügt. Sie laden Google Fonts – die Content-Security-Policy von Miniflux blockiert das, bis Sie unter *Einstellungen > Externe Schriftart-Hosts* `fonts.googleapis.com fonts.gstatic.com` eintragen. Ohne diesen Eintrag greifen die System-Fallback-Schriften.

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
│   ├── evaluate.py         # Clustering-Schwelle an eigenen Feeds kalibrieren
│   ├── store.py            # SQLite-Story-Datenbank
│   ├── web.py              # Top-Stories-Oberfläche
│   ├── templates/          # HTML-Templates
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

### Native Apps

Alternativ können folgende Apps die Miniflux-API nutzen:

- **FeedMe** (Android): Volle Offline-Unterstützung
- **ReadYou** (Android): Material Design, E-Ink-freundlich
- **Reeder** (iOS/macOS): Native Miniflux-Integration

## Sicherheit

### Reverse Proxy Setup

Für externen Zugriff wird ein Reverse Proxy empfohlen:

```nginx
server {
    listen 443 ssl http2;
    server_name news.example.com;

    ssl_certificate /etc/letsencrypt/live/news.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/news.example.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Authentifizierung

- Miniflux bietet eigene Benutzerverwaltung
- API-Keys für externe Anwendungen
- Optional: Authelia/2FA am Reverse Proxy

## Troubleshooting

### Clustering funktioniert nicht

1. Prüfen Sie den Status: `curl http://<unraid-ip>:8081/healthz` (enthält die Statistik des letzten Laufs)
2. Prüfen Sie die Logs: `docker logs arsse-intelligence`
3. Stories entstehen erst, wenn mehrere Feeds über dasselbe Thema berichten – abonnieren Sie mehrere überlappende Quellen
4. Zu wenige oder zu große Stories: `threshold` mit `evaluate.py` kalibrieren (höher = größere, aber unschärfere Stories)
5. `Cannot open story database`: Der Container konnte `${DATA_PATH}/intelligence` nicht an `PUID:PGID` übergeben (z.B. NFS-Freigabe mit `root_squash`, oder eine ältere `docker-compose.yml` mit `user:`) – dann `chown PUID:PGID` auf das Verzeichnis selbst ausführen
6. Alle Links der Top Stories zeigen auf `localhost` oder die falsche Adresse: `BASE_URL` in `.env` setzen und `docker compose up -d` ausführen
7. `Datenbank stammt von neuerer aRSSe-Version`: Das Image ist älter als die Datenbank (z.B. nach einem Rückschritt auf eine ältere Version). Entweder wieder die neuere Version starten, ein Backup von `arsse.db` aus der Zeit vor dem Update einspielen oder `${DATA_PATH}/intelligence/arsse.db*` löschen – die Datenbank ist nur ein Zwischenspeicher und wird beim nächsten Lauf aus Miniflux neu aufgebaut (nur die Story-IDs ändern sich)
8. Der Container startet nicht und das Log nennt eine Einstellung (z.B. `scheduling.batch_size must be between 1 and 1000`): den Wert in `config.yaml` bzw. `.env` korrigieren

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

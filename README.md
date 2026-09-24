# aRSSe - Autonomer RSS-basierter News-Aggregator

Ein selbstgehosteter Nachrichten-Aggregator für Unraid-Systeme, der die Kernfunktionalität von Google News repliziert. Optimiert für E-Ink-Displays und Mobile-First-Paradigmen.

## Architektur

Das System besteht aus fünf logischen Schichten:

| Schicht | Komponente | Technologie | Funktion |
|---------|------------|-------------|----------|
| Ingestion | Miniflux | Go, Docker | RSS/Atom-Abruf, Parsing, Full-Content Scraping |
| Storage | PostgreSQL | SQL | Persistente Speicherung von Artikeln und Metadaten |
| Intelligence | Python-Service | Python, Scikit-Learn, SQLite | TF-IDF, DBSCAN Clustering, Deduplizierung |
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
# Bearbeiten Sie .env mit Ihren Einstellungen
```

### 3. Stack starten

```bash
docker-compose up -d
```

### 4. Miniflux aufrufen

Öffnen Sie `http://<unraid-ip>:8080` und melden Sie sich mit den in `.env` konfigurierten Zugangsdaten an.

### 5. Top Stories aktivieren

Erzeugen Sie in Miniflux unter *Einstellungen > API-Schlüssel* einen Key, tragen Sie ihn als `MINIFLUX_API_KEY` in `.env` ein und starten Sie `docker compose up -d intelligence`. Die Top Stories sind dann unter `http://<unraid-ip>:8081` erreichbar.

Alternativ erledigt `scripts/setup.sh` die Schritte 2–4 inklusive Passwortgenerierung und Verzeichnisrechten.

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

1. **Extraction**: Abruf aller Artikel der letzten 24 Stunden via Miniflux API (gelesen und ungelesen, paginiert)
2. **Preprocessing**: HTML entfernen, Normalisierung, Stopwords, Stemming (Snowball)
3. **Vectorization**: TF-IDF über Uni- und Bigramme
4. **Clustering**: DBSCAN gruppiert Artikel zum selben Ereignis zu einer *Story*
5. **Deduplication**: Near-Duplicates (z.B. identische Agenturmeldungen) innerhalb einer Story
6. **Persistence**: Stories landen in einer lokalen SQLite-Datenbank (`data/intelligence/arsse.db`) mit über Läufe hinweg stabilen IDs; Duplikate werden optional in Miniflux als gelesen markiert

Die Miniflux-API kann keine Tags oder eigenen Metadaten schreiben – deshalb bringt der Service eine eigene, JavaScript-freie Oberfläche mit:

| Pfad | Inhalt |
|------|--------|
| `/` | Top Stories, gerankt nach Anzahl der Quellen und Aktualität |
| `/story/<id>` | Alle Artikel einer Story |
| `/api/stories` | Dieselben Daten als JSON |
| `/healthz` | `200`, solange der letzte erfolgreiche Lauf weniger als drei Intervalle zurückliegt |

Links führen in Miniflux (`BASE_URL`), damit Gelesen-Status und Volltext erhalten bleiben.

**Konfiguration:**

```yaml
# intelligence/config.yaml
clustering:
  eps: 0.4                       # DBSCAN Epsilon (Kosinus-Distanz)
  min_samples: 2                 # Minimum Artikel pro Story
  stemming: true

deduplication:
  threshold: 0.85                # Duplikat-Schwellenwert
  duplicate_action: "mark_read"  # oder "none"

scheduling:
  interval_minutes: 30           # Ausführungsintervall
```

Umgebungsvariablen aus `.env` (z.B. `CLUSTERING_EPS`) überschreiben `config.yaml`; auskommentierte bzw. leere Variablen tun das nicht.

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
│   ├── news_clustering.py  # Clustering-Logik und Einstiegspunkt
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
└── scripts/
    └── setup.sh            # Initialisierungsskript
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
4. Zu wenige oder zu große Stories: `eps` anpassen (höher = größere, aber unschärfere Stories)
5. `Cannot open story database`: Das Datenverzeichnis gehört nicht `PUID:PGID` – `chown` auf `${DATA_PATH}/intelligence` ausführen

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

## Lizenz

MIT License - Siehe [LICENSE](LICENSE) für Details.

## Danksagungen

- [Miniflux](https://miniflux.app/) - Der minimalistische RSS-Reader
- [Scikit-Learn](https://scikit-learn.org/) - Machine Learning für Python
- Die Unraid-Community für Inspiration und Support

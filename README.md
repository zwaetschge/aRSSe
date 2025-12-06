# aRSSe - Autonomer RSS-basierter News-Aggregator

Ein selbstgehosteter Nachrichten-Aggregator für Unraid-Systeme, der die Kernfunktionalität von Google News repliziert. Optimiert für E-Ink-Displays und Mobile-First-Paradigmen.

## Architektur

Das System besteht aus fünf logischen Schichten:

| Schicht | Komponente | Technologie | Funktion |
|---------|------------|-------------|----------|
| Ingestion | Miniflux | Go, Docker | RSS/Atom-Abruf, Parsing, Full-Content Scraping |
| Storage | PostgreSQL | SQL | Persistente Speicherung von Artikeln und Metadaten |
| Intelligence | Python Bot | Python, Scikit-Learn | TF-IDF Vektorisierung, DBSCAN Clustering, Deduplizierung |
| Presentation | WebApp | HTML5, CSS3 | E-Ink-optimierte Darstellung |
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

### Intelligence Layer (Python Bot)

Der Python-basierte Clustering-Service operiert zyklisch und führt folgende Schritte aus:

1. **Extraction**: Abruf ungelesener Artikel via Miniflux API
2. **Preprocessing**: Textnormalisierung (Stopwords, Stemming)
3. **Vectorization**: TF-IDF Transformation
4. **Clustering**: DBSCAN für thematische Gruppierung
5. **Deduplication**: Erkennung von Near-Duplicates
6. **Tagging**: Rückschreiben der Cluster-Tags in Miniflux

**Konfiguration:**

```yaml
# intelligence/config.yaml
clustering:
  eps: 0.4              # DBSCAN Epsilon (Ähnlichkeitsschwelle)
  min_samples: 2        # Minimum Artikel pro Cluster

deduplication:
  threshold: 0.85       # Duplikat-Schwellenwert

scheduling:
  interval_minutes: 30  # Bot-Ausführungsintervall
```

### E-Ink Optimierung

Das Custom CSS für E-Ink-Displays berücksichtigt:

- **Keine Animationen**: Vermeidung von Ghosting
- **Hoher Kontrast**: Reines Schwarz auf Weiß
- **Serifen-Typografie**: Bessere Lesbarkeit
- **Große Touch-Targets**: Mobile-freundliche Bedienung
- **Pagination statt Scrolling**: Weniger Refreshes

## Verzeichnisstruktur

```
aRSSe/
├── docker-compose.yml      # Haupt-Stack-Definition
├── .env.example            # Umgebungsvariablen-Vorlage
├── intelligence/
│   ├── Dockerfile          # Python-Container
│   ├── requirements.txt    # Python-Abhängigkeiten
│   ├── news_clustering.py  # Clustering-Logik
│   ├── config.py           # Konfigurationsmodul
│   └── config.yaml         # Bot-Konfiguration
├── css/
│   └── eink-theme.css      # E-Ink-optimiertes Theme
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

1. Prüfen Sie die Logs: `docker logs arsse-intelligence`
2. Stellen Sie sicher, dass genügend Artikel vorhanden sind (min. 10)
3. Passen Sie `eps` in der Konfiguration an (höher = weniger Cluster)

### E-Ink-Darstellung fehlerhaft

1. Deaktivieren Sie JavaScript-Animationen im Browser
2. Aktivieren Sie "A2 Refresh Mode" auf dem Gerät
3. Prüfen Sie, ob das Custom CSS korrekt geladen wurde

### Miniflux startet nicht

1. Prüfen Sie PostgreSQL: `docker logs arsse-db`
2. Warten Sie auf den Health Check (ca. 30 Sekunden)
3. Prüfen Sie die DATABASE_URL in `.env`

## Lizenz

MIT License - Siehe [LICENSE](LICENSE) für Details.

## Danksagungen

- [Miniflux](https://miniflux.app/) - Der minimalistische RSS-Reader
- [Scikit-Learn](https://scikit-learn.org/) - Machine Learning für Python
- Die Unraid-Community für Inspiration und Support

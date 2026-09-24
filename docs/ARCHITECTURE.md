# aRSSe Architektur

## Übersicht

aRSSe ist ein selbstgehosteter Nachrichten-Aggregator, der die Kernfunktionalität von Google News repliziert. Das System basiert auf einer 5-Schichten-Architektur:

```
┌─────────────────────────────────────────────────────────────┐
│                    Access Layer (Nginx)                      │
│                   SSL, Authentifizierung                     │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                 Presentation Layer                           │
│      Top-Stories-Seite (Flask) + Miniflux-Custom-CSS        │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│               Intelligence Layer (Python)                    │
│   TF-IDF, DBSCAN, Deduplizierung, SQLite-Story-Speicher     │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                  Storage Layer (PostgreSQL)                  │
│               Artikel, Metadaten, Konfiguration             │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                  Ingestion Layer (Miniflux)                  │
│          RSS/Atom Parsing, Full-Content Extraction          │
└─────────────────────────────────────────────────────────────┘
```

## Komponenten im Detail

### 1. Ingestion Layer (Miniflux)

**Technologie:** Go, Docker

**Aufgaben:**
- Abruf von RSS/Atom-Feeds
- URL-Discovery und Feed-Parsing
- Full-Content Extraction via Readability
- Custom Scraper Rules für komplexe Seiten

**Konfiguration:**
- `POLLING_FREQUENCY`: Abrufintervall (Standard: 15 Min.)
- `POLLING_SCHEDULER`: Scheduling-Strategie
- `FETCH_YOUTUBE_WATCH_TIME`: YouTube-Integration

### 2. Storage Layer (PostgreSQL)

**Technologie:** PostgreSQL 15, Alpine Linux

**Schema:**
- `entries`: Artikel mit Volltext
- `feeds`: Feed-Konfigurationen
- `categories`: Kategorisierung
- `users`: Benutzerverwaltung

**Optimierung:**
- SSD-Cache empfohlen
- Regelmäßige Bereinigung via CLEANUP_*

### 3. Intelligence Layer (Python)

**Technologie:** Python 3.11, Scikit-Learn, NLTK, SQLite, Flask/Waitress

**Algorithmen:**

#### TF-IDF Vektorisierung
```python
TfidfVectorizer(
    max_features=5000,
    tokenizer=tokenize,   # NLTK-Stopwords + Snowball-Stemmer (deutsch)
    ngram_range=(1, 2),
    min_df=2,
    sublinear_tf=True
)
```

#### DBSCAN Clustering
```python
DBSCAN(
    eps=0.4,           # Ähnlichkeitsschwelle
    min_samples=2,     # Min. Artikel pro Cluster
    metric='cosine'    # Kosinus-Distanz
)
```

#### Deduplizierung
- Paarweise Cosine Similarity innerhalb einer Story
- Eigene Term-Frequenz-Vektoren über das volle Vokabular: Das Clustering verwirft seltene Terme (`min_df`), und genau diese unterscheiden zwei Berichte zum selben Thema
- Schwellenwert: 0.85 (konfigurierbar)
- Kanonisierung: längster, priorisierter oder neuester Artikel
- Duplikate werden optional per `PUT /v1/entries` in Miniflux als gelesen markiert

#### Story-Speicher
Die Miniflux-API kann Einträge nur in Titel, Inhalt und Status ändern – Tags oder
eigene Metadaten lassen sich nicht zurückschreiben. Stories liegen deshalb in einer
SQLite-Datenbank im Datenverzeichnis des Containers:

| Tabelle | Inhalt |
|---------|--------|
| `entries` | Kopie der Artikel-Metadaten (Titel, Feed, URL, Snippet) |
| `stories` | Story-ID, Schlagzeilen-Artikel, erstmals/zuletzt gesehen |
| `story_entries` | Zuordnung Artikel → Story, Duplikat-Flag |
| `meta` | Zeitpunkt und Statistik des letzten Laufs |

**Stabile Story-IDs:** DBSCAN nummeriert Cluster bei jedem Lauf neu. Ein neuer Cluster
übernimmt daher die ID der bisherigen Story, mit der er die meisten Artikel teilt;
größere Cluster wählen zuerst. Nur wirklich neue Themen bekommen eine neue ID.

**Ranking:** `Anzahl Quellen / (1 + Alter des neuesten Artikels in Stunden / 12)`

### 4. Presentation Layer

**Top Stories (Port 8081):** Server-seitig gerenderte Seiten ohne JavaScript und ohne
externe Ressourcen – funktioniert auf E-Ink-Readern ebenso wie im Desktop-Browser,
Dark Mode über `prefers-color-scheme`.

**Miniflux-Themes:** `css/eink-theme.css` und `css/color-theme.css` als
benutzerdefiniertes CSS. Miniflux liefert Manifest und Service Worker selbst mit,
die PWA-Installation läuft über Miniflux.

**E-Ink-Optimierung:**
- Keine Animationen/Transitions
- Hoher Kontrast (Schwarz/Weiß)
- Serifen-Typografie
- Große Touch-Targets (min. 44px)

### 5. Access Layer

**Technologie:** Nginx, Let's Encrypt

**Funktionen:**
- SSL-Terminierung
- Reverse Proxy
- Optional: Basic Auth / Authelia

## Datenfluss

```
                     ┌──────────────┐
                     │  RSS Feeds   │
                     └──────┬───────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                     Miniflux                            │
│  ┌─────────┐  ┌──────────┐  ┌─────────────────────┐   │
│  │ Fetcher │─▶│  Parser  │─▶│ Content Extractor   │   │
│  └─────────┘  └──────────┘  └─────────────────────┘   │
└────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                    PostgreSQL                           │
│  ┌─────────┐  ┌─────────┐  ┌─────────────────────┐    │
│  │ entries │  │  feeds  │  │      users          │    │
│  └─────────┘  └─────────┘  └─────────────────────┘    │
└────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│               Intelligence Layer                        │
│  ┌───────────┐  ┌──────────┐  ┌─────────────────┐     │
│  │ Vectorize │─▶│ Cluster  │─▶│   Deduplicate   │     │
│  └───────────┘  └──────────┘  └─────────────────┘     │
│                                        │               │
│                                        ▼               │
│                              ┌─────────────────┐       │
│                              │ SQLite + Status │       │
│                              └─────────────────┘       │
└────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│               Top Stories (Port 8081)                   │
│  ┌────────────────────────────────────────────────┐   │
│  │         Clustered, Deduplicated Stories        │   │
│  └────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────┘
```

## Vergleich mit Google News

| Feature | Google News | aRSSe |
|---------|-------------|-------|
| Ingestion | Web Crawling | RSS/Atom Feeds |
| Clustering | Transformer/BERT | TF-IDF + DBSCAN |
| Deduplizierung | SimHash | Cosine Similarity |
| Ranking | ML + Nutzerverhalten | Quellenanzahl + Aktualität |
| Personalisierung | Deep Learning | Feed-Auswahl, Quellen-Scores |
| Skalierung | Global | Lokal (< 100k Artikel) |

## Erweiterungsmöglichkeiten

### LLM-Integration (Ollama)

```python
# Cluster-Zusammenfassung via lokalem LLM
import requests

def summarize_cluster(articles):
    prompt = f"Fasse diese Nachrichtenmeldungen zusammen:\n{articles}"
    response = requests.post(
        "http://ollama:11434/api/generate",
        json={"model": "llama2", "prompt": prompt}
    )
    return response.json()["response"]
```

### pgvector für Vektor-Suche

```sql
CREATE EXTENSION vector;

ALTER TABLE entries
ADD COLUMN embedding vector(384);

-- Ähnlichkeitssuche
SELECT * FROM entries
ORDER BY embedding <=> '[query_vector]'
LIMIT 10;
```

## Ressourcen-Anforderungen

| Komponente | CPU | RAM | Storage |
|------------|-----|-----|---------|
| Miniflux | 1 Core | 256 MB | - |
| PostgreSQL | 1 Core | 512 MB | SSD empfohlen |
| Intelligence | 2 Cores | 1-2 GB | - |
| **Gesamt** | **4 Cores** | **4 GB** | **10+ GB** |

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
│                 Presentation Layer (WebApp)                  │
│              E-Ink optimiertes CSS, PWA                      │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│               Intelligence Layer (Python)                    │
│        TF-IDF, DBSCAN Clustering, Deduplizierung            │
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

**Technologie:** Python 3.11, Scikit-Learn, NLTK

**Algorithmen:**

#### TF-IDF Vektorisierung
```python
TfidfVectorizer(
    max_features=5000,
    stop_words='german',
    ngram_range=(1, 2)
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
- Paarweise Cosine Similarity innerhalb Clusters
- Schwellenwert: 0.85 (konfigurierbar)
- Kanonisierung: längster/priorisierter Artikel

### 4. Presentation Layer

**Technologie:** HTML5, CSS3

**E-Ink-Optimierung:**
- Keine Animationen/Transitions
- Hoher Kontrast (Schwarz/Weiß)
- Serifen-Typografie
- Große Touch-Targets (min. 48px)

**PWA-Funktionen:**
- Manifest für Homescreen-Installation
- Service Worker für UI-Caching

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
│                              │   Tag/Update    │       │
│                              └─────────────────┘       │
└────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                   E-Ink WebApp                          │
│  ┌────────────────────────────────────────────────┐   │
│  │         Clustered, Deduplicated Feed           │   │
│  └────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────┘
```

## Vergleich mit Google News

| Feature | Google News | aRSSe |
|---------|-------------|-------|
| Ingestion | Web Crawling | RSS/Atom Feeds |
| Clustering | Transformer/BERT | TF-IDF + DBSCAN |
| Deduplizierung | SimHash | Cosine Similarity |
| Ranking | ML + Nutzerverhalten | Konfigurierbare Scores |
| Personalisierung | Deep Learning | Tag-basiert |
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

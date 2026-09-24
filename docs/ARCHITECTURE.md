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
│ TF-IDF, Average Linkage, Deduplizierung, SQLite-Speicher    │
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

#### Vorverarbeitung
HTML wird nur bis 200 000 Zeichen geparst, Titel werden auf 500 Zeichen gekürzt. Echte
Artikel liegen weit darunter; ein defekter oder feindseliger Feed mit mehreren MB pro
Eintrag kostete sonst bei jedem Lauf Sekunden und Hunderte MB Speicher.
Eingebettete `data:`-URIs (Miniflux behält z. B. `data:image/*`) werden vorher entfernt:
Sie enthalten keinen Text, würden die 200 000 Zeichen aber allein füllen.

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

#### Agglomeratives Clustering (Average Linkage)
```python
AgglomerativeClustering(
    n_clusters=None,
    metric='precomputed',      # Kosinus-Distanzmatrix
    linkage='average',
    distance_threshold=0.75
)
```
Cluster aus nur einem Artikel sind keine Story. Average Linkage verlangt, dass eine
Story *im Durchschnitt* allen ihren Artikeln nahe ist. Das vorher verwendete DBSCAN verband
dagegen Artikel über Ketten einzelner Nachbarn – ein gemeinsames Stichwort wie „Trump“
reichte, um Gipfeltreffen und Pressestreit zu einer Story zu verschmelzen.

**Kalibrierung** an 519 Artikeln aus 13 deutschen Feeds (24 h, RSS-Kurztexte):

| Verfahren | Stories mit ≥ 2 Quellen | Größte Story | Befund |
|-----------|------------------------:|-------------:|--------|
| DBSCAN eps=0.4 | 17 | 6 | sauber, aber 85 % der Artikel ohne Story |
| DBSCAN eps=0.6 | 45 | 10 | sauber |
| DBSCAN eps=0.8 | 51 | 135 | Verkettung zu Sammel-Clustern |
| Average Linkage 0.7 | 68 | 7 | sauber |
| **Average Linkage 0.75** | **90** | **10** | **sauber (Stichprobe)** |
| Average Linkage 0.8 | 102 | 14 | erste falsche Paare |

Laufzeit bei 2000 Artikeln: ca. 1 s, ca. 260 MB RAM.

Die Top-Stories-Seite zeigt nur Stories aus mindestens zwei Feeds (`web.min_sources`):
Werbeblöcke („Anzeige: … Tiefstpreis“) oder Serien einer Redaktion ähneln sich
untereinander, sind aber keine Nachrichtenlage.

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

**Abruf:** Die Artikel des Zeitfensters kommen seitenweise nach Artikel-ID absteigend
(`order=id`, ab der zweiten Seite `before_entry_id` = kleinste ID der Vorseite). Seiten
nach `published_at` mit `offset` überlappten, sobald viele Artikel dieselbe Zeit tragen
(volle Minuten, Feeds ohne Datum) oder Miniflux während des Abrufs neue Artikel speicherte –
ein doppelter Artikel ließ jeden Lauf scheitern. Neue Artikel bekommen immer höhere IDs
und verschieben keine Seite. Greift `max_entries`, bleiben die zuletzt gespeicherten Artikel.
Liefert eine Seite nur bereits bekannte IDs, ignoriert der Server `before_entry_id`; dann
bleibt es bei der ersten Seite, und der Abruf gilt (wie bei `max_entries`) als unvollständig.

**Datumsangaben:** `entries.published_at` ist höchstens der Zeitpunkt, zu dem Miniflux den
Artikel gespeichert hat (`created_at`), bzw. der des ersten Abrufs. Feeds mit falscher
Zeitzone oder vorausdatierten Einträgen würden sonst nie altern und ihre Story tagelang
oben halten. Das Datum des Feeds steht unverändert in `published_at_raw`. Gespeichert wird
immer in UTC mit ganzen Sekunden (`2026-09-24T08:15:00+00:00`), damit SQL die Werte als
Text vergleichen kann.

**Schema-Versionen:** `PRAGMA user_version` hält die Schema-Version. `store.MIGRATIONS`
ist eine nur wachsende Liste; Eintrag *i* hebt Version *i* auf *i + 1*. Beim Start laufen
alle fehlenden Migrationen samt Versionswechsel in einer Transaktion – schlägt eine fehl,
bleibt die Datenbank unverändert. Datenbanken ohne Version (vor Einführung) gelten als
Version 1. Ist die Datenbank neuer als der Code, startet der Dienst nicht
(„Datenbank stammt von neuerer aRSSe-Version“). Eine Änderung, die sich nicht per SQL
nachziehen lässt, wird als `REBUILD` eingetragen: Die alte Datei wandert nach
`arsse.db.v<N>.bak` und die Datenbank entsteht neu – sie ist ein Zwischenspeicher, der
nächste Lauf holt alles wieder aus Miniflux, nur Story-IDs und „zuerst gesehen“ gehen verloren.

**Lesen:** Die Weboberfläche liest Story und Artikel in einer Lesetransaktion. Ein Lauf,
der dazwischen speichert, kann daher keine halbe Story (und keinen Fehler 500) erzeugen.

**Stabile Story-IDs:** Das Clustering nummeriert Cluster bei jedem Lauf neu. Jedes Paar
aus neuem Cluster und bisheriger Story mit gemeinsamen Artikeln wird bewertet, die Paare
werden vom besten an vergeben, solange weder Cluster noch Story schon vergeben sind:

1. Story und Cluster sind beide sichtbar (mindestens `web.min_sources` Feeds im Zeitfenster).
   Eine ID, die auf der Startseite stand, bleibt dort; eine unsichtbare Serie eines einzelnen
   Feeds kann sie weder übernehmen, wenn beide für einen Lauf verschmelzen, noch behalten,
   wenn sie sich wieder trennen.
2. Mehr gemeinsame Artikel: Die eigentliche Fortsetzung behält die ID, nicht ein Nebenthema,
   das einen Artikel mitgenommen hat.
3. Der Cluster enthält den bisherigen Schlagzeilen-Artikel – bei einer Teilung in gleich
   große Hälften folgt die ID also dem Titel, den der Nutzer kannte.
4. Danach sichtbare vor unsichtbaren, größere vor kleineren Clustern.

Nur Cluster ohne passende Story bekommen eine neue ID. Bei einer Wiederholung des Korpus
mit 30-Minuten-Läufen (24 h und 6 h Zeitfenster) bekam keine sichtbare beste Fortsetzung
eine neue ID, während ein Cluster mit weniger gemeinsamen Artikeln die alte behielt.

**Zeitfenster:** Artikel, die aus dem Zeitfenster (`lookback_hours`) gefallen sind, bleiben
ihrer Story zugeordnet, zählen aber nicht mehr: Quellen- und Artikelzahl, Ranking und
`web.min_sources` beziehen sich nur auf Artikel im Fenster. Eine Story ohne Artikel im
Fenster erscheint nicht mehr (auch nicht unter `/story/<id>`). Die Story-Seite listet ältere
Artikel unter „Frühere Berichte“ (höchstens `web.earlier_articles_max`, Standard 20). Liegt der
bisherige Schlagzeilen-Artikel außerhalb des Fensters, steht der neueste Artikel oben.

**Gelöschte Artikel:** „Verlauf leeren“ in Miniflux löscht gelesene Artikel – auch die
Duplikate, die aRSSe selbst als gelesen markiert –, und ein abbestellter Feed nimmt seine
Artikel mit. Ihre Links führen danach ins Leere (404). War der Abruf vollständig, entfernt
jeder Lauf daher gespeicherte Artikel, die nach dem Beginn des Zeitfensters (plus 60 s
Sicherheitsabstand) erschienen sind, aber nicht mehr geliefert wurden: Miniflux hätte sie
liefern müssen, denn gespeichert ist höchstens das Datum, nach dem Miniflux filtert. War der
Abruf wegen `max_entries` unvollständig, gilt das nur ab der kleinsten gelieferten Artikel-ID.
Stories, denen dadurch alle Artikel fehlen, verschwinden.

**Aufbewahrung:** `storage.retention_days` gilt pro Artikel: Ältere Artikel verlassen ihre
Story, auch wenn diese weiterläuft; danach werden Stories gelöscht, die so lange nicht mehr
aufgetaucht sind oder keine Artikel mehr haben.

**Ranking:** `Anzahl Quellen / (1 + Alter des neuesten Artikels in Stunden / 12)`, beides
über die Artikel im Zeitfenster

#### Container-Start
Docker legt ein fehlendes Bind-Mount-Verzeichnis als root an. Deshalb startet der
Container als root: `entrypoint.py` legt `/app/data` an, übergibt das Verzeichnis und
die Dateien darin (`arsse.db*`, Log) an `PUID:PGID` – nur wenn der Besitzer abweicht –,
löscht die Zusatzgruppen, wechselt Gruppe und Benutzer und startet dann den Dienst per
`exec`. Läuft der Container bereits ohne Root-Rechte (ältere Compose-Dateien mit `user:`),
startet der Entrypoint den Dienst unverändert. Der Rechtewechsel ist in Python
geschrieben, weil `setpriv`/`gosu` im Basis-Image nicht garantiert sind.

### 4. Presentation Layer

**Top Stories (Port 8081):** Server-seitig gerenderte Seiten ohne JavaScript und ohne
externe Ressourcen – funktioniert auf E-Ink-Readern ebenso wie im Desktop-Browser,
Dark Mode über `prefers-color-scheme`. Artikel-Links führen nach Miniflux (`BASE_URL`).
Zeigt `BASE_URL` auf eine Loopback-Adresse, setzt die Oberfläche die Links pro Anfrage
aus dem aufgerufenen Host und `MINIFLUX_PORT` zusammen, da der Reader nie der Server ist.

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
| Clustering | Transformer/BERT | TF-IDF + Average Linkage |
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

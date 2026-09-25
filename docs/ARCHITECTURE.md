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
Als Vorschautext (Snippet, höchstens 280 Zeichen) dient der Klartext ohne angehängte
Teaser-Links (`[ mehr ]` bei Tagesschau und MDR, `mehr...` bei taz, `Weiterlesen`); ein
nacktes „mehr“ nur nach Satzende, damit „gibt es nicht mehr“ stehen bleibt. Texte unter
20 Zeichen, das wörtliche `None` mancher Feeds und eine bloße Wiederholung des Titels
ergeben kein Snippet.

#### TF-IDF Vektorisierung
```python
TfidfVectorizer(
    max_features=5000,
    tokenizer=tokenize,   # NLTK-Stopwords + Snowball-Stemmer (deutsch)
    ngram_range=(1, clustering.ngram_max),   # Standard 1: nur einzelne Wörter
    min_df=2,
    sublinear_tf=True
)
```
Der Text ist der Titel (doppelt gewichtet) plus der Artikeltext ohne HTML. Einträge ohne
Titel, deren Text mit „+++“ beginnt (Nachrichten-Ticker wie beim MDR), bleiben draußen: Sie
streifen alle Themen des Tages und zogen im Referenzsatz fremde Artikel in die größte Story
(ohne den einen Ticker steigt R von 0.641 auf 0.668).

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

**Zwei-Artikel-Stories:** Bei zwei Artikeln ist der Durchschnitt nur das eine Paar, und
0.75 verlangt dafür lediglich eine Kosinus-Ähnlichkeit von 0.25. Die erreichen schon zwei
kurze Anrisstexte mit einem einzigen gemeinsamen seltenen Wort („Razzia“, „beendet“) – mit
`min_df=2` fallen alle Wörter weg, die nur ein Artikel enthält, sodass das gemeinsame Wort
fast den ganzen Vektor ausmacht. Eine Story aus genau zwei Artikeln braucht deshalb
zusätzlich `clustering.min_pair_similarity` (Standard 0.30). Größere Stories sind davon nicht
betroffen.

**Themen:** Ein Ereignis zerfällt oft in mehrere Stories, weil die Berichte verschiedene
Seiten beleuchten (Klopps Debüt, seine Aufstellung, der gegnerische Trainer). Ein zweiter
Average-Linkage-Durchgang über dieselbe Distanzmatrix mit der großzügigeren
`clustering.topic_threshold` (Standard 0.9, `CLUSTERING_TOPIC_THRESHOLD`, muss über
`threshold` liegen, 0 = aus; liegt nur der Standard nicht darüber, etwa bei
`CLUSTERING_THRESHOLD=0.9` aus der Zeit vor den Themen, schaltet eine Warnung im Log die
Themen ab, statt den Start zu verweigern) ordnet jede Story einem Thema zu (das Thema der meisten ihrer
Artikel). `topic_key` ist die kleinste Artikel-ID unter den Stories des Themas. Die Stories
selbst bleiben unverändert – anders als der verworfene Durchgang, der Stories
zusammenlegte (unten); das Thema steuert nur die Anzeige: Die Startseite zeigt je Thema die
bestplatzierte Story und darunter die übrigen als „Mehr zum Thema“. Am Referenzsatz (letzter
Lauf, 63 angezeigte Stories) fasst 0.9 34 Stories zu 15 Themen zusammen, 13 davon passend
(Klopp-Debüt mit Aufstellung und Xavi, Pressezugang zum Weißen Haus, Olympia-Bewerbung,
Konjunkturprognose …), 2 nicht (ein Hamburger Kriminalfall mit einem Fußballer-Rücktritt,
zwei Ermittlungen ohne Bezug); die Startseite hat 44 statt 63 Plätze. 0.85 fasst nur 17
Stories zu 8 Themen zusammen, 0.95 schon 41 zu 13. Der zweite Durchgang kostet so viel wie
der erste (0.4 s für beide bei 519 Artikeln).

**Erste Kalibrierung** (Stichprobe von Hand, damals mit Wortpaaren) an 519 Artikeln aus
13 deutschen Feeds (24 h, RSS-Kurztexte):

| Verfahren | Stories mit ≥ 2 Quellen | Größte Story | Befund |
|-----------|------------------------:|-------------:|--------|
| DBSCAN eps=0.4 | 17 | 6 | sauber, aber 85 % der Artikel ohne Story |
| DBSCAN eps=0.6 | 45 | 10 | sauber |
| DBSCAN eps=0.8 | 51 | 135 | Verkettung zu Sammel-Clustern |
| Average Linkage 0.7 | 68 | 7 | sauber |
| **Average Linkage 0.75** | **90** | **10** | **sauber (Stichprobe)** |
| Average Linkage 0.8 | 102 | 14 | erste falsche Paare |

Die Stichprobe übersah viel: Nach den handmarkierten Daten (unten) waren 25 dieser 90 Stories
unsinnig.

**Messungen am Referenzsatz:** Dieselben 519 Artikel sind von Hand markiert: 59 Ereignisse
(dieselbe konkrete Meldung, muss zusammen) und 27 neutrale Themen (verwandt oder
Sammelbeitrag, weder gefordert noch Fehler); alle übrigen Paare dürfen nicht zusammen.
`evaluate.py --gold` misst damit die paarweise Precision (P: Anteil der Artikelpaare in einer
Story, die wirklich zusammengehören) und den Recall (R: Anteil der zusammengehörigen Paare, die
eine Story bilden), und teilt die angezeigten Stories (≥ 2 Quellen) ein in *sauber* (kein
falsches Paar), *gemischt* (echte Story mit Eindringling) und *unsinnig* (kein echtes Paar
über Feeds hinweg). Schwelle jeweils 0.75:

| Variante | P | R | F1 | Stories | sauber / gemischt / unsinnig |
|----------|--:|--:|---:|--------:|-----------------------------:|
| Wortpaare, ohne Untergrenze (bisher) | 0.839 | 0.486 | 0.616 | 90 | 62 / 3 / 25 |
| Wortpaare, Untergrenze 0.30 | 0.909 | 0.475 | 0.624 | 68 | 55 / 3 / 10 |
| Einzelwörter, ohne Untergrenze | 0.917 | 0.675 | 0.777 | 83 | 65 / 1 / 17 |
| **Einzelwörter, Untergrenze 0.30 (Standard)** | **0.955** | **0.668** | **0.786** | **64** | **55 / 1 / 8** |
| Einzelwörter, Untergrenze 0.35 | 0.980 | 0.659 | 0.788 | 54 | 50 / 1 / 3 |
| Einzelwörter, Untergrenze 0.40 | 0.990 | 0.652 | 0.786 | 48 | 47 / 1 / 0 |

- Wortpaare verschlechtern Precision *und* Recall. In 20 von 20 Zufallsstichproben (je 80 %) lagen Einzelwörter bei beiden vorn
  (P 0.865 ± 0.019 statt 0.817 ± 0.024, R 0.657 ± 0.045 statt 0.525 ± 0.038)
- Die Untergrenze 0.30 entfernt von 57 Zwei-Artikel-Stories aus verschiedenen Feeds 9 der 17
  unsinnigen und 7 der 14 nur themenverwandten, aber auch 3 der 26 echten (Ähnlichkeit 0.27
  bis 0.29: Clankontakte der Linkspartei, E-Roller-Urteil, Angriff in Düsseldorf). 0.35 kostet
  zwei weitere echte (Klopp-Aufstellung 0.32, Explosionen in der Ukraine 0.33) und ist die
  Wahl für eine strengere Startseite
- Die Schwelle bleibt 0.75: Mit Einzelwörtern und Untergrenze 0.30 ergibt 0.70 P 0.941,
  R 0.468; 0.80 ergibt P 0.878, R 0.718, aber 10 unsinnige und 5 gemischte Stories

Wiederholung mit 25 Läufen im Abstand von 30 Minuten über die echte Story-Datenbank
(`evaluate.py --replay 25`): Anteil der Stories einer Startseite, die im nächsten Lauf unter
derselben ID stehen, davon mit geänderter Schlagzeile, ganz verschwunden, Überlappung der
Top 10 und Rausch-Schlagzeilen (siehe unten) auf der letzten Startseite:

| Variante | ID behalten | Schlagzeile geändert | verschwunden | Top 10 | Rausch-Schlagzeilen |
|----------|------------:|---------------------:|-------------:|-------:|--------------------:|
| Stand vor diesen Änderungen | 89.1 % | 5.1 % | 10.3 % | 5.96 | 6 / 50 |
| **Standard** | **92.5 %** | **1.3 %** | **7.1 %** | **6.38** | **0 / 50** |
| Untergrenze 0.35 | 95.5 % | 1.2 % | 4.4 % | 6.96 | 0 / 50 |

Gemessen und verworfen (Wortpaare, Schwelle 0.75, falls nicht anders angegeben):

- Ohne Stemming: P 0.818, R 0.409 (statt 0.839 / 0.486)
- Ohne NLTK-Stopwords: R 0.357; eine zusätzliche Liste typischer Nachrichtenwörter („sagt“,
  „Prozent“, „Millionen“) senkt R mit Einzelwörtern und Untergrenze 0.30 von 0.641 auf 0.595
  und bringt 11 statt 8 unsinnige Stories; `max_df` 0.2 ändert nichts
- Titel einfach statt doppelt: R 0.568 statt 0.648 (Einzelwörter); dreifach: keine
  Verbesserung (0.845 / 0.482); nur Titel: P 0.417
- Textbausteine entfernen („[ mehr ]“ der Tagesschau, taz und MDR, der Anrisstext „None“ der
  Zeit): ändert eine einzige Story. Nur die ersten 400 Zeichen des Textes: kein Unterschied,
  fast alle Feeds liefern ohnehin nur Anrisstexte
- Werbung, Podcasts, Videos, Wetter usw. (43 Einträge) ganz aus dem Clustering nehmen
  (Einzelwörter, Untergrenze 0.30): P/R praktisch gleich (0.952 / 0.634 statt 0.953 / 0.641),
  nur 2 unsinnige Stories weniger. Sie werden deshalb nur als Schlagzeile zurückgestellt
  (siehe Kanonisierung); nur der Ticker ohne Titel bleibt draußen
- Zeichen-n-Gramme (3-5 Zeichen): P 0.931, R 0.673 – ähnlich gut, aber mit 45 000 statt
  2 000 Merkmalen dreimal so langsam und nur mit neu kalibrierter Schwelle nutzbar
- Zweiter Durchgang, der Stories mit ähnlichem Schwerpunkt zusammenlegt (Einzelwörter,
  Untergrenze 0.30): bei Kosinus ≥ 0.4 keine Änderung, bei 0.35 R + 0.03, 4 Stories weniger
- Andere Ranking-Formeln (Median- statt neuestes Alter, Quellen hoch 1.5, ohne Zeitabzug,
  mit Zusammenhalt der Story): NDCG@10 gleich oder schlechter als die bestehende Formel
- Schlagzeile = der Artikel nächst dem Schwerpunkt der Story: wechselt in 36-38 % der Läufe
  die Schlagzeile (Ähnlichkeiten verschieben sich mit jedem neuen Artikel)

Laufzeit bei 2000 Artikeln: Vektorisierung und Clustering 0.30 s (mit Wortpaaren 0.33 s),
mit HTML-Parsing und Duplikaterkennung ca. 1.4 s; ca. 260 MB RAM.

**Eigene Messungen:** `eval/export_corpus.py` speichert die Artikel des Zeitfensters aus
Miniflux (`eval/corpus.json`, von git und dem Image ausgenommen: Die Texte gehören den
Verlagen). `eval/gold.json` enthält nur die Markierungen des Referenzsatzes, nach URL, ohne
Text; der Korpus dazu liegt nicht im Repository. Für eigene Feeds braucht es eigene
Markierungen im selben Format:

```bash
cd intelligence
python eval/export_corpus.py --out eval/corpus.json      # MINIFLUX_URL, MINIFLUX_API_KEY
python evaluate.py --corpus eval/corpus.json --gold eval/gold.json --replay 25
python evaluate.py --corpus eval/corpus.json --gold eval/gold.json 0.7 0.75 0.8 \
    --ngram-max 2 --min-pair-similarity 0 --show-junk
```

Ohne `--gold` gibt `evaluate.py` statt der Tabelle nur Stories aus (wie ohne `--corpus`);
`--replay` misst auch dann die Stabilität. Stories, von denen kein Artikelpaar markiert ist,
zählt die Tabelle weder als sauber noch als gemischt oder unsinnig und nennt ihre Zahl
darunter. Die Unit-Tests prüfen den Ablauf an einem kleinen erfundenen Satz
(`tests/fixtures/eval_*.json`).

Die Top-Stories-Seite zeigt nur Stories aus mindestens zwei Feeds (`web.min_sources`):
Werbeblöcke („Anzeige: … Tiefstpreis“) oder Serien einer Redaktion ähneln sich
untereinander, sind aber keine Nachrichtenlage.

#### Deduplizierung
Innerhalb einer Story gilt ein Paar als Duplikat, wenn (in dieser Reihenfolge):

1. es derselbe Artikel zweimal ist: gleiche URL (Schema und Host ohne Groß-/Kleinschreibung,
   ohne `#…`, `utm_*`, `wt_mc` und abschließenden `/`), gleicher Titel, gleicher oder fast
   gleicher Text (Cosine Similarity ab `deduplication.threshold`) – auch aus verschiedenen
   Feeds und ohne Mindestlänge. Feeds liefern Artikel manchmal doppelt, und Miniflux speichert
   beide (im Kalibrierkorpus 9 Paare der Tagesschau). Ergänzt die Redaktion später einen Satz,
   bleibt es derselbe Artikel. Die URL allein reicht nicht: Einträge ohne Link bekommen in
   Miniflux die Adresse der Website;
2. sonst nie, wenn beide aus demselben Feed stammen: Sendungen und Serien tragen denselben
   Titel und denselben Anrisstext („tagesschau“ mit „[ mehr ]“), sind aber verschiedene Beiträge;
3. bei verschiedenen Feeds, wenn die Texte **ohne Titel** eine Cosine Similarity von mindestens
   `deduplication.threshold` (0.85) erreichen und beide mindestens
   `deduplication.min_body_tokens` Wörter (25) haben. Mit Titel entschied bei kurzen
   Anrisstexten die gleiche Überschrift allein.

- Eigene Term-Frequenz-Vektoren über das volle Vokabular: Das Clustering verwirft seltene Terme (`min_df`), und genau diese unterscheiden zwei Berichte zum selben Thema
- Kanonisierung: längster (Textlänge ohne HTML), priorisierter oder neuester Artikel; Artikel mit
  Titel vor solchen ohne, danach Titel, die keinem `clustering.noise_title_patterns` entsprechen
  (Werbung „Anzeige:“/„heise-Angebot:“, Paywall „(g+)“/„heise+“/„SPIEGEL+“/„F+“/„SZ Plus“,
  Podcasts, Liveblogs und -ticker, „Video:“, Wetter, „News des Tages“, „Was jetzt?“,
  Briefings). Solche Artikel bleiben Teil der Story, stehen aber nur oben, wenn es keinen
  anderen gibt: Im Referenzsatz waren 10 von 90 Schlagzeilen Werbung, Podcasts oder Ähnliches,
  weil `longest` gerade lange Werbetexte bevorzugt. Bei Gleichstand gewinnt die kleinere Artikel-ID (zuerst gespeichert) –
  sonst tauschten identische Kopien je nach Reihenfolge der API-Antwort die Rollen, und beide
  wurden nach und nach als gelesen markiert. Aus demselben Grund bleibt ein Artikel, den aRSSe
  schon als gelesen markiert hat, nie anstelle einer ungelesenen Kopie stehen, auch wenn
  Miniflux seinen Text später aktualisiert und er dadurch der längste wird
- Gruppen: Der beste noch freie Artikel bleibt stehen und nimmt alle freien Artikel auf, die
  Duplikate *von ihm* sind; die übrigen bilden eigene Gruppen. Jedes Duplikat wurde also mit dem
  Artikel verglichen, der ungelesen bleibt. Das gilt auch über mehrere Läufe: Kopien kommen
  nacheinander an, und ein Artikel, der für ein früher markiertes Duplikat stehen geblieben ist,
  wird nicht nachträglich als Duplikat einer neuen, besseren Kopie markiert, wenn das markierte
  Duplikat dieser neuen Kopie nicht ähnlich genug ist – sonst verschwände dessen eigener Text
  (etwa ein exklusiver Satz) ganz aus den ungelesenen Artikeln. Dann bleiben ausnahmsweise zwei
  ähnliche Kopien ungelesen. Als doppelt gelieferter Artikel (Fall 1) zählt ein
  Duplikat, das derselbe Artikel ist wie ein besser eingestuftes Mitglied seiner Gruppe
- Duplikate werden optional per `PUT /v1/entries` in Miniflux als gelesen markiert, mit
  `mark_read_scope: visible` (Standard) nur in Stories aus mindestens `web.min_sources` Feeds und
  doppelt gelieferte Artikel (Fall 1) überall. Die Tabelle `auto_marked` merkt sich, was aRSSe
  markiert hat: Setzt der Nutzer ein Duplikat wieder auf ungelesen, bleibt es dabei. Einträge
  verfallen `lookback_hours` + 24 h nach dem letzten Abruf, der den Artikel noch enthielt –
  nicht nach dem Markieren: Miniflux übernimmt Datumsangaben in der Zukunft unverändert, und
  solche Artikel bleiben bis zu diesem Datum im Abruffenster. Beim Update auf Schema 3 wird die
  Tabelle mit allen Duplikaten gefüllt, die bereits gelesen sind: Ältere Versionen markierten
  jedes ungelesene Duplikat in jedem Lauf und wählten die bleibende Kopie anders (bei
  Gleichstand die höchste ID, `longest` zählte HTML). Ohne diese Übernahme ließe der erste Lauf
  nach dem Update die schon markierte Kopie stehen und markierte die andere auch

#### Story-Speicher
Die Miniflux-API kann Einträge nur in Titel, Inhalt und Status ändern – Tags oder
eigene Metadaten lassen sich nicht zurückschreiben. Stories liegen deshalb in einer
SQLite-Datenbank im Datenverzeichnis des Containers:

| Tabelle | Inhalt |
|---------|--------|
| `entries` | Kopie der Artikel-Metadaten (Titel, Feed, URL, Snippet, Status, Rubrik) |
| `stories` | Story-ID, Schlagzeilen-Artikel, erstmals/zuletzt gesehen, Thema (`topic_key`) |
| `story_entries` | Zuordnung Artikel → Story, Duplikat-Flag |
| `meta` | Zeitpunkt und Statistik des letzten Laufs, Zeitpunkt seines Abrufs |
| `auto_marked` | Von aRSSe als gelesen markierte Duplikate (werden nie erneut markiert) |
| `user_read` | Vom Nutzer mit „Story gelesen“ markierte Artikel |

**Abruf:** Die Artikel des Zeitfensters kommen seitenweise nach Artikel-ID absteigend
(`order=id`, ab der zweiten Seite `before_entry_id` = kleinste ID der Vorseite). Seiten
nach `published_at` mit `offset` überlappten, sobald viele Artikel dieselbe Zeit tragen
(volle Minuten, Feeds ohne Datum) oder Miniflux während des Abrufs neue Artikel speicherte –
ein doppelter Artikel ließ jeden Lauf scheitern. Neue Artikel bekommen immer höhere IDs
und verschieben keine Seite. Greift `max_entries`, bleiben die zuletzt gespeicherten Artikel.
Liefert eine Seite nur bereits bekannte IDs, ignoriert der Server `before_entry_id`; dann
bleibt es bei der ersten Seite, und der Abruf gilt (wie bei `max_entries`) als unvollständig.
Mit `globally_visible=true` (`miniflux.respect_hide_globally`, Standard an) liefert Miniflux
keine Artikel aus Feeds oder Kategorien, die der Nutzer aus den globalen Listen ausgeblendet
hat; gespeicherte Artikel daraus entfernt der nächste Lauf wie gelöschte.

**Rubriken:** `entries.section` ist der Titel der Miniflux-Kategorie des Feeds
(`feed.category.title`), außer der Standardkategorie „All“/„Alle“, die nichts aussagt. Sonst
entscheidet der URL-Pfad ohne den letzten Abschnitt (der benennt den Artikel):
`web.path_sections` ordnet reguläre Ausdrücke für einen ganzen Abschnitt Rubriken zu, das
erste passende Muster gewinnt (Regional und Sport vor dem breiten Politik, damit
`tagesschau.de/inland/regional/bayern/…` Regional ist). Fehlen Kategorie und passender Pfad,
hat der Artikel keine Rubrik. Die Rubrik einer Story ist die ihrer meisten Artikel im
Zeitfenster, bei Gleichstand die des neuesten. Am Referenzsatz (alle Feeds in „All“) tragen
so 49 der 63 Stories eine Rubrik; Heise und Golem (`/news/…`) bekommen ihre nur über eine
Miniflux-Kategorie wie „Technik“.

**Gelesen:** `entries.status` kommt aus Miniflux. Eine Story, deren Artikel im Zeitfenster
alle gelesen sind, zeigt die Startseite nur mit `?alle=1`. „Story gelesen“ schickt die IDs
ihrer ungelesenen Artikel im Fenster an Miniflux (`PUT /v1/entries`), setzt sie lokal auf
gelesen und trägt sie in `user_read` ein – getrennt von `auto_marked`: Die dort
verzeichneten Duplikate stellt die Kanonisierung hinten an, vom Nutzer Gelesenes nicht. Hat
eine Story Artikel aus `user_read` und wieder ungelesene, sind diese „neu“ (Anzeige „N neu“).
Ein Lauf, der vor dem Tippen abgerufen, aber danach gespeichert hat, überschreibt den Status
nicht (`user_read.read_at` gegen den Abrufzeitpunkt).

**Status-Abgleich:** Zwischen zwei Läufen (30 Minuten) fragt ein eigener Thread alle
`scheduling.status_sync_minutes` (Standard 5, 0 = aus) nach Artikeln des Zeitfensters, die in
Miniflux seit dem letzten Abgleich gelesen wurden (`status=read`, `changed_after`, seitenweise
nach ID, höchstens `max_entries`), und setzt nur gespeicherte Artikel auf gelesen. Miniflux
setzt `changed_at` bei jeder Statusänderung. Die Abfragen überlappen um 60 s (ganze
Sekunden, Uhren zweier Container); nach einem Lauf beginnt der nächste Abgleich wieder bei
dessen Abrufzeitpunkt (`meta.last_fetch_at`), weil der Lauf ältere Stände gespeichert haben
kann. Vor dem ersten Lauf gibt es nichts abzugleichen. Fehler landen im Log, die Wartezeit
verdoppelt sich bis zum Achtfachen; der Abgleich hält weder Clustering noch Weboberfläche auf.

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
nächste Lauf holt alles wieder aus Miniflux. Verloren gehen nur Story-IDs und
`auto_marked`: Duplikate im aktuellen Zeitfenster, die der Nutzer wieder auf ungelesen
gesetzt hat, werden dann noch einmal als gelesen markiert. Dasselbe gilt, wenn `arsse.db`
gelöscht oder ein älteres Backup eingespielt wird.

**Lesen:** Die Weboberfläche liest Story und Artikel in einer Lesetransaktion. Ein Lauf,
der dazwischen speichert, kann daher keine halbe Story (und keinen Fehler 500) erzeugen.

**Stabile Story-IDs:** Das Clustering nummeriert Cluster bei jedem Lauf neu. Jedes Paar
aus neuem Cluster und bisheriger Story mit gemeinsamen Artikeln wird bewertet, die Paare
werden vom besten an vergeben, solange weder Cluster noch Story schon vergeben sind:

1. Story und Cluster sind beide sichtbar (mindestens `web.min_sources` Feeds im Zeitfenster).
   Eine ID, die auf der Startseite stand, bleibt dort; eine unsichtbare Serie eines einzelnen
   Feeds kann sie weder übernehmen, wenn beide für einen Lauf verschmelzen, noch behalten,
   wenn sie sich wieder trennen.
2. Unter solchen Paaren: mehr gemeinsame Artikel. Die eigentliche Fortsetzung behält die ID,
   nicht ein Nebenthema, das einen Artikel mitgenommen hat.
3. Die Story war sichtbar und der Cluster enthält ihren Schlagzeilen-Artikel. Setzt kein
   sichtbarer Cluster sie fort, bleibt die ID beim Titel, den der Nutzer kannte, statt an eine
   größere unsichtbare Serie zu gehen – auch wenn das Ereignis für einen Lauf nur noch einen
   Feed hat.
4. Mehr gemeinsame Artikel, dann der Cluster mit dem bisherigen Schlagzeilen-Artikel – bei
   einer Teilung in gleich große Hälften folgt die ID also dem Titel.
5. Danach sichtbare vor unsichtbaren, größere vor kleineren Clustern.

Nur Cluster ohne passende Story bekommen eine neue ID. Bei einer Wiederholung des Korpus
mit 30-Minuten-Läufen (24 h und 6 h Zeitfenster) bekam keine sichtbare beste Fortsetzung
eine neue ID, während ein Cluster mit weniger gemeinsamen Artikeln die alte behielt.

**Zeitfenster:** Artikel, die aus dem Zeitfenster (`lookback_hours`) gefallen sind, bleiben
ihrer Story zugeordnet, zählen aber nicht mehr: Quellen- und Artikelzahl, Ranking und
`web.min_sources` beziehen sich nur auf Artikel im Fenster. Eine Story ohne Artikel im
Fenster erscheint nicht mehr (auch nicht unter `/story/<id>`). Die Story-Seite listet ältere
Artikel unter „Frühere Berichte“ (höchstens `web.earlier_articles_max`, Standard 20). Liegt der
bisherige Schlagzeilen-Artikel außerhalb des Fensters, steht der neueste Artikel oben, der
kein Duplikat ist. Die Startseite verlinkt die Story-Seite, sobald es mehr Artikel gibt, als
sie zeigt, oder frühere Berichte.

**Gelöschte Artikel:** „Verlauf leeren“ in Miniflux löscht gelesene Artikel – auch die
Duplikate, die aRSSe selbst als gelesen markiert –, und ein abbestellter Feed nimmt seine
Artikel mit. Ihre Links führen danach ins Leere (404). War der Abruf vollständig, entfernt
jeder Lauf daher gespeicherte Artikel, die nach dem Beginn des Zeitfensters (plus 60 s
Sicherheitsabstand) erschienen sind, aber nicht mehr geliefert wurden: Miniflux hätte sie
liefern müssen, denn gespeichert ist höchstens das Datum, nach dem Miniflux filtert. War der
Abruf wegen `max_entries` unvollständig, gilt das nur ab der kleinsten gelieferten Artikel-ID.
Stories, denen dadurch alle Artikel fehlen, verschwinden. Ältere Artikel liefert Miniflux
nicht mehr, ob es sie noch gibt, lässt sich also nicht prüfen: Sie bleiben bis zur
Aufbewahrungsgrenze gespeichert, und „Frühere Berichte“ verlinkt sie deshalb auf das Original
beim Anbieter statt auf Miniflux (ohne gültige URL nur als Text).

**Aufbewahrung:** `storage.retention_days` gilt pro Artikel: Ältere Artikel verlassen ihre
Story, auch wenn diese weiterläuft; danach werden Stories gelöscht, die so lange nicht mehr
aufgetaucht sind oder keine Artikel mehr haben.

**Schlagzeile:** Mit `canonical_strategy: longest` (Standard) bleibt die Schlagzeile einer
Story, solange ihr Artikel noch dazugehört, kein Duplikat ist (Duplikate werden als gelesen
markiert) und nicht als Rauschen gilt – auch wenn inzwischen ein längerer Artikel
dazugekommen ist. Sonst wechselte der Titel mit jedem längeren Bericht, und der E-Ink-Reader
müsste die ganze Seite neu aufbauen. Mit `newest` und `source_priority` gilt das nicht: Dort
ist der Wechsel gewollt, der neueste Bericht bzw. die bessere Quelle übernimmt die
Schlagzeile.

**Ranking:** `Anzahl Quellen / (1 + Alter des neuesten Artikels in Stunden / 12)`, beides
über die Artikel im Zeitfenster. Stories, deren Artikel im Fenster alle einem der
`web.exclude_patterns` entsprechen (Standard: `^Wetter\b`, der Wetterbericht mehrerer Sender),
erscheinen nicht auf der Startseite; ihre Story-Seite bleibt erreichbar.

**Werbung schon in Miniflux ausfiltern (optional):** Miniflux kann Einträge beim Abruf
verwerfen (*Einstellungen > Eintrags-Sperrregeln* für alle Feeds oder dasselbe Feld in den
Einstellungen eines Feeds). Mit der Regel

```
EntryTitle=(?i)^(Anzeige|heise-Angebot):
```

landen Werbeeinträge (im 24-Stunden-Referenzsatz 10 bei Golem, 4 bei Heise) gar nicht erst
in Miniflux.
Das ist eine bewusste Entscheidung des Nutzers und deshalb nicht voreingestellt: Die Einträge
fehlen dann auch in der normalen Miniflux-Ansicht.

#### Container-Start
Docker legt ein fehlendes Bind-Mount-Verzeichnis als root an. Deshalb startet der
Container als root: `entrypoint.py` legt `/app/data` an, übergibt das Verzeichnis und
die Dateien darin (`arsse.db*`, Log) an `PUID:PGID` – nur wenn der Besitzer abweicht –,
löscht die Zusatzgruppen, wechselt Gruppe und Benutzer und startet dann den Dienst per
`exec`. Läuft der Container bereits ohne Root-Rechte (ältere Compose-Dateien mit `user:`),
startet der Entrypoint den Dienst unverändert. Der Rechtewechsel ist in Python
geschrieben, weil `setpriv`/`gosu` im Basis-Image nicht garantiert sind.

Compose startet den Container mit schreibgeschütztem Dateisystem (`read_only`, nur
`/app/data` und ein `tmpfs` unter `/tmp` sind beschreibbar), `no-new-privileges` und
ohne Linux-Berechtigungen außer `CHOWN` und `DAC_OVERRIDE` (Datenverzeichnis übergeben)
sowie `SETUID`/`SETGID` (Benutzer wechseln). Nach dem Wechsel zu `PUID:PGID` hat der
Dienst keine Berechtigung mehr. Der Code gehört root, nur `/app/data` gehört `PUID`;
den Bytecode übersetzt schon der Image-Bau (`PYTHONDONTWRITEBYTECODE=1` zur Laufzeit).
Grenzen: 1 GB Speicher, 1 CPU, 128 Prozesse; `OPENBLAS_NUM_THREADS=1` hält den
BLAS-Threadpool klein (gemessen ohne Laufzeitnachteil). Die Speichergrenze deckt den
Höchstwert `max_entries` = 5000 (gemessen ~790 MB Spitze: ~176 MB nach den Imports plus
gut drei dichte n×n-Matrizen); `test_memory_limit_fits_the_largest_allowed_run` prüft
Compose-Datei und Unraid-Template gegen `MAX_ENTRIES_LIMIT`.

#### Konfiguration in Schichten
`load_config` liest nacheinander die Standardwerte der Dataclasses, `/app/config.yaml`
(im Image, dokumentierte Referenz), `/app/legacy/config.yaml` (nur für den Umstieg, s.u.),
`/app/data/config.yaml` (eigene Einstellungen im Datenvolume, legt der erste Start aus
`config.stub.yaml` an) und die Umgebungsvariablen. Jede Datei setzt nur die Einstellungen, die sie nennt
(`_apply_section` je Abschnitt); Listen und Tabellen ersetzen den Wert ganz. Die
Referenz entspricht genau den Standardwerten (Test `test_shipped_config_equals_the_defaults`);
weicht `/app/config.yaml` davon ab, wurde sie vor dem Image-Bau geändert oder von einer
älteren Compose-Datei eingebunden – der Dienst nennt dann die betroffenen Einstellungen
im Log und verweist auf die eigene Datei.

Ältere Versionen banden `./intelligence/config.yaml` als `/app/config.yaml` ein, und das
README empfahl, sie zu ändern. Damit solche Änderungen mit dem veröffentlichten Image
nicht stillschweigend verschwinden, bindet `docker-compose.yml` `./intelligence` nur
lesend als `/app/legacy` ein. Checkout und Image sind oft verschiedene Versionen
(`git pull` folgt dem Hauptzweig, `ARSSE_VERSION=latest` der letzten Release-Version),
daher gilt `/app/legacy/config.yaml` nur als geändert, wenn sie keiner jemals
veröffentlichten Fassung der Referenz entspricht. Diese Fassungen stehen flach
(`flatten_reference`) in `config.history.json` – im Image neben `/app/config.yaml`,
und ein neuerer Checkout bringt seine eigene mit (`/app/legacy/config.history.json`).
`scripts/config-history.py` erzeugt die Datei aus der Git-Geschichte;
`test_current_reference_is_in_the_history` schlägt fehl, solange eine geänderte
`config.yaml` dort fehlt. Als Änderungen zählen nur die Abweichungen von der ähnlichsten
Fassung (`_read_legacy_edits`), nicht die Standardwerte einer anderen Version. Sie gelten
als Schicht zwischen Referenz und eigener Datei (`_finish_legacy`): Was die eigene Datei
setzt, gewinnt; die übrigen Änderungen gelten weiter und stehen bei jedem Start in einer
Warnung mit den Schritten zum Umzug. Fehlt die Datei, ist sie eine unveränderte Fassung,
nicht lesbar (Rechte, kaputtes YAML) oder wäre die Konfiguration mit ihren Werten
ungültig, wird sie übersprungen – Fehler in der eigenen Datei oder `.env` stoppen den
Dienst weiterhin, die alte Datei nie. `scripts/backup.sh`, `evaluate.py` und
`eval/export_corpus.py` laden die Einstellungen genauso.

#### Fehler und Health Check
Scheitert ein Lauf (oder schon das Anlegen des Miniflux-Clients), speichert
`record_error` in der Tabelle `meta` den Eintrag `last_error` = `{at, kind, message}` mit
einer deutschen Meldung ohne technische Einzelheiten (`/healthz` ist ohne Anmeldung
erreichbar): `AccessUnauthorized` → „Miniflux lehnt den API-Key ab“ (`auth`), fehlender Key
→ „MINIFLUX_API_KEY fehlt“ (`config`), andere Miniflux- und Netzwerkfehler → „Miniflux
unter <URL> nicht erreichbar“ (`connection`), `sqlite3.OperationalError` → „Datenbank
nicht beschreibbar“ (`database`), alles andere nur mit dem Namen der Ausnahme (`internal`).
`at` bleibt, solange derselbe Fehler wiederkehrt („Fehler seit 10:30“), `last` ist der
jüngste Fehlschlag; ein erfolgreicher Lauf löscht den Eintrag. Lässt sich die Datenbank
selbst nicht schreiben, hält `StoryStore.unsaved_error` den Eintrag im Speicher;
Startseite und `/healthz` lesen `StoryStore.last_error()`, das ihn dann vorzieht. Wiederholt wird nach 10 s, danach mit doppeltem Abstand bis
höchstens `min(Intervall, 5 min)`.

`/healthz` antwortet `ok` (200), solange der letzte Erfolg weniger als drei Intervalle
zurückliegt; `starting` (200), solange seit dem Start noch kein Lauf gelungen ist, keiner
gescheitert ist und der Dienst weniger als drei Intervalle läuft; sonst `stale` (503).
„Seit dem Start“ heißt: `last` (ältere Einträge: `at`) liegt nicht vor dem Zeitpunkt, den
`main()` noch vor dem Scheduler nimmt. Ein Fehler von vor einem Neustart (z.B. der falsche
Key, den der Benutzer gerade korrigiert hat) steht weiter in `last_error`, bis der nächste
Lauf ihn löscht oder ersetzt, beendet `starting` aber nicht.
Der Docker-Health-Check des Images fragt `/healthz` ab.

#### Build und Release
`requirements.txt` (pip-compile mit Hashes aus `requirements.in`) legt jede
Abhängigkeit samt Prüfsumme fest; das Image installiert sie mit `--require-hashes` und
nur als Wheels (für amd64 und arm64 vorhanden). Das Basis-Image ist per Digest
festgelegt, die NLTK-Stopwörter kommen aus einem festen nltk_data-Commit mit SHA-256
(`fetch_nltk_data.py`), die GitHub Actions sind per Commit-SHA festgelegt; Dependabot
schlägt Updates vor. `.github/workflows/release.yml` baut nach allen Tests für
`linux/amd64` und `linux/arm64` und veröffentlicht `ghcr.io/zwaetschge/arsse-intelligence`:
`edge` und `sha-<commit>` für jeden Push auf den Standardzweig, `X.Y.Z`, `X.Y` und
`latest` für Tags `vX.Y.Z` – nie für Pull Requests.

### 4. Presentation Layer

**Top Stories (Port 8081):** Server-seitig gerenderte Seiten ohne JavaScript und ohne
externe Ressourcen – funktioniert auf E-Ink-Readern ebenso wie im Desktop-Browser,
Dark Mode über `prefers-color-scheme`. Artikel-Links führen nach Miniflux (`BASE_URL`).
Zeigt `BASE_URL` auf eine Loopback-Adresse, setzt die Oberfläche die Links pro Anfrage
aus dem aufgerufenen Host und `MINIFLUX_PORT` zusammen, da der Reader nie der Server ist.

Für E-Ink ausgelegt, wo eine Seite stundenlang stehen bleibt und jedes Scrollen einen
Teil-Refresh mit Geisterbildern kostet:
- *Seiten:* `web.page_size` Plätze pro Seite (Standard 10, `?seite=N`), insgesamt höchstens
  `web.max_stories` (Standard 100); jedes Thema belegt einen Platz. `store.front_page`
  liefert die Seite und die Gesamtzahl aus derselben Rangliste; eine Seite außerhalb von 1…N
  (oder kein Zahlwert) ergibt `404`. Seite 1 des Referenzsatzes ist mit den Knöpfen „Story
  gelesen“ rund 24 KB groß. Oben und unten führen
  44 px hohe Links „← Zurück“/„Weiter →“ weiter, die Statuszeile lautet z. B.
  „Stand 18:32 · Seite 1 von 7 · 63 Stories“.
- *Uhrzeiten:* nur absolut, weil relative Angaben („vor 5 Min.“) auf einem stehenden
  Bildschirm unbemerkt falsch werden: `14:53` für heute, `Mi 14:53` innerhalb einer Woche,
  sonst `17.09. 14:53`, jeweils als `<time datetime="…">`. Zeitzone: `web.timezone`, sonst
  `TZ` (docker-compose übergibt `Europe/Berlin`), sonst Europe/Berlin; die Zeitzonendaten
  bringt das Paket `tzdata` mit, weil `python:3.11-slim` keine hat. Eine Story-Seite nennt
  „Erste Meldung 14:53 (taz) · zuletzt 18:10 (Deutschlandfunk)“ – ältester und neuester
  Artikel im Zeitfenster, nicht der Zeitpunkt des Clustering-Laufs.
- *Berichterstattung:* Unter der Schlagzeile bekommt zuerst jede andere Quelle einen Platz
  (ihr neuester Artikel, `store.select_coverage`), der Feed der Schlagzeile und weitere
  Artikel derselben Feeds nur die übrigen der `web.articles_per_story` Plätze. Duplikate
  stehen dort nicht, sondern als „+2 gleichlautende Meldungen“ in der Meta-Zeile; auf der
  Story-Seite zusammengeklappt (`<details>`) unter „Gleichlautende Meldungen“.
- *Story-Seite:* „Nach Aktualität“ (neueste zuerst, je Artikel ein eigener 44-px-Link
  „Original“ zum Anbieter) oder `?ansicht=chronologisch` (älteste zuerst, nach Tagen
  gruppiert, mit einer Zeile Snippet je Artikel).
- *Artikel ohne Titel:* Ein Artikel ohne Titel erscheint als „MDR: Landtag wählt neuen
  Präsidenten …“ (Feed und die ersten 80 Zeichen) statt „(ohne Titel)“. Ticker ohne Titel,
  deren Text mit „+++“ beginnt, erreichen keine Story (siehe Clustering oben).
- *Bedienung:* Jede Zeile der Berichterstattung ist ein Block-Link mit Quelle und Uhrzeit,
  mindestens 44 px hoch. Titel sind dünn unterstrichen; bereits geöffnete Artikel
  erscheinen grau (ohne Farben auf Graustufen-Displays sonst nicht zu unterscheiden;
  `:visited` darf nur Farben ändern, eine gepunktete Unterstreichung ignorieren die
  Browser). „Gleichlautende Meldungen“ behält sein Aufklapp-Dreieck. Pfeile sind für
  Screenreader ausgeblendet (`aria-hidden`).
- *Always-on:* `?auto=1` lädt die Seite alle 30 Minuten neu (`<meta http-equiv=refresh>`,
  kein JavaScript). Alle Links auf die eigenen Seiten behalten den Parameter (Kopfzeile,
  Seiten, Story-Seiten und „Zurück zu Top Stories“, `nav()` in `web.py`); eine Story-Seite
  mit `?auto=1` lädt nach 30 Minuten wieder die Titelseite (`url=/?auto=1`), damit ein
  Wand-Display nicht bei einer angetippten Story stehen bleibt. Gibt es die Seite nach dem
  Neuladen nicht mehr (Stories sind aus dem Zeitfenster gefallen), leitet `?auto=1` auf
  die letzte vorhandene Seite bzw. von einer Story auf die Titelseite um statt auf eine
  404 ohne Neuladen; ohne `?auto=1` bleibt es bei 404.
- *Themen:* Unter der bestplatzierten Story eines Themas stehen bis zu drei weitere als
  „Mehr zum Thema: Schlagzeile (3 Quellen)“ (je ein 44-px-Link zur Story-Seite), bei mehr
  verweist „Alle N Artikel · M verwandte“ auf die Story-Seite, die alle unter „Verwandte
  Stories“ listet. `/api/stories` bleibt eine flache Liste mit `topic_key`.
- *Rubriken:* Eine Zeile „Alle · Politik (16) · Panorama (8) · Sport (6) …“ (44-px-Links,
  meiste zuerst, gezählt wie die Seite der Rubrik) führt zu `?rubrik=Name`; auch
  `/api/stories?rubrik=` filtert. Eine leere Rubrik ist eine leere Seite, keine 404.
- *Gelesen:* Gelesene Stories fehlen (`?alle=1` zeigt sie mit „gelesen“, die Statuszeile
  bietet „N gelesene zeigen“: so viele Plätze kämen mit `?alle=1` auf dieser Rubrik-Seite
  hinzu, gezählt wie die Seiten, also je Thema einmal), und die bestplatzierte ungelesene Story führt ihr Thema an.
  Jede Story mit ungelesenen Artikeln hat einen Knopf „Story gelesen (N)“: ein Formular
  ohne JavaScript (`POST /story/<id>/gelesen`, Feld `next` = aufrufende Seite, auf der
  Startseite weggelassen). Die Antwort ist `303` auf `next`, wenn es mit genau einem `/`
  beginnt und keine Steuerzeichen oder `\` enthält, sonst auf `/` – nie auf eine fremde Seite.
  Gibt es die Seite danach nicht mehr (die einzige Story der letzten Seite ist gelesen), geht
  es auf die neue letzte Seite statt auf eine 404.
  Ohne `MINIFLUX_API_KEY` fehlt der Knopf (`503`); ist Miniflux nicht erreichbar, bleibt alles
  ungelesen (`502`). Eine nach „Story gelesen“ wieder erscheinende Story trägt „N neu“
  (ihre ungelesenen Artikel); nach Lesen in Miniflux fehlt die Angabe, denn dort lässt sich
  „ganze Story gelesen“ nicht von „einen Artikel gelesen“ unterscheiden – sonst stünde nach
  einem gelesenen Artikel „9 neu“ da.
- *Suche:* Das Suchfeld im Kopf führt zu `/suche?q=…` (2 bis 100 Zeichen, sonst nur das
  Formular): `LIKE` über Titel und Anriss aller gespeicherten Artikel (Aufbewahrung, nicht
  nur Zeitfenster), `%` und `_` als Zeichen, Groß-/Kleinschreibung auch bei Umlauten egal
  (`casefold` als SQL-Funktion). Treffer werden zu Stories zusammengefasst, wie die
  Startseite gerankt (über alle Artikel der Story) und seitenweise gezeigt, je Story die
  passenden Artikel. Artikel außerhalb des Zeitfensters verlinken auf das Original, und nur
  Stories mit Artikeln im Fenster haben „Zur Story“. Darunter: „In Miniflux suchen“
  (`BASE_URL/search?q=…`, Volltext über alles).
- *Startbildschirm:* `intelligence/static/` enthält `manifest.webmanifest` (`start_url` `/`,
  `display: standalone`), PNG-Icons in 192 und 512 px, ein SVG-Icon und `favicon.ico`
  (auch unter `/favicon.ico`). Die Icons erzeugt `scripts/make-icons.py` nur mit der
  Standardbibliothek. Einen Service Worker gibt es nicht, die Seiten bleiben ohne
  JavaScript. Der Manifest-Link trägt `crossorigin="use-credentials"`: Ohne das holen
  Browser das Manifest ohne Zugangsdaten, und eine Anmeldung am Reverse Proxy (die auch
  `/static/` abdeckt) beantwortet es mit 401 – die Seite ist dann nicht installierbar.
  `/static/` ist trotzdem ohne Anmeldung erreichbar, weil Android die Icons teils ohne
  Zugangsdaten abruft; `/favicon.ico` und alle Seiten verlangen sie.

**Zugriffsschutz der Top Stories** (`web.auth`, `web.allowed_hosts`): Ein `before_request`-
Hook prüft jede Anfrage in dieser Reihenfolge:
1. *Host:* Ist `web.allowed_hosts` gesetzt, bekommt jeder andere Hostname `400` (Schutz vor
   DNS-Rebinding); `localhost`, `127.0.0.1` und `::1` gehen immer.
2. *Anmeldung:* `/healthz` (Docker-Health-Check) und `/static/` sind ausgenommen.
   `basic` vergleicht Benutzer und Passwort in konstanter Zeit (`hmac.compare_digest`) und
   antwortet sonst mit `401` und `WWW-Authenticate: Basic realm="aRSSe"`. `proxy` glaubt
   den Header (`Remote-User`) nur, wenn die TCP-Gegenstelle in `trusted_proxies` liegt,
   sonst `403`. `none` (Standard) lässt alles durch und warnt beim Start im Log.
   `load_config` lehnt ab, was nie funktionieren oder alle hereinlassen würde:
   `trusted_proxies` mit Präfix `/0`, Header-Namen mit `_` (waitress verwirft solche
   Header) und Platzhalter in `allowed_hosts`. IDN-Namen werden dort zu Punycode, IP-
   Adressen (auch IPv6 ohne Klammern) zu ihrer Kurzform – so, wie Browser sie senden.
3. *CSRF:* Für alles außer GET/HEAD/OPTIONS muss `Sec-Fetch-Site` `same-origin` oder
   `none` sein; ältere Browser ohne diesen Header müssen `Origin` bzw. `Referer` mit dem
   aufgerufenen Host senden (`require_same_origin`). Basic Auth allein schützt nicht, weil
   Browser gemerkte Zugangsdaten auch an fremde Formulare anhängen. „Story gelesen“, die
   einzige Route, die etwas verändert (in Miniflux), prüft das zusätzlich selbst; mit
   `web.auth.mode: none` ist es ihr einziger Schutz.

Jede Antwort trägt `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
…; frame-ancestors 'none'` (die Seiten haben kein JavaScript, nur einen `<style>`-Block),
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`
und `Cross-Origin-Resource-Policy: same-origin`. HSTS setzt der Reverse Proxy.

**Miniflux-Benutzer:** API-Keys von Miniflux haben keine Rechte-Einschränkung. Beim ersten
Lauf fragt der Dienst `GET /v1/me` ab und warnt, wenn der Key einem Admin gehört; ist
Miniflux nicht erreichbar, wiederholt er das beim nächsten Lauf, ohne den Lauf zu stören.

**Miniflux-Themes:** `css/eink-theme.css` und `css/color-theme.css` als
benutzerdefiniertes CSS. Miniflux liefert Manifest und Service Worker selbst mit,
die PWA-Installation läuft über Miniflux. Die Themes verwenden die Schriften des Geräts
(Literata, Charter, Georgia bzw. die Systemschrift) und laden nichts von fremden Servern;
die Google-Fonts-Zeile ist auskommentiert und nur ein Angebot – aktiviert, ruft jede
Miniflux-Seite Google auf, und Miniflux muss die Font-Hosts in seine CSP aufnehmen.

**E-Ink-Optimierung:**
- Keine Animationen/Transitions
- Hoher Kontrast (Schwarz/Weiß)
- Serifen-Typografie
- Große Touch-Targets (min. 44px)

### 5. Access Layer

**Technologie:** Nginx, Let's Encrypt

**Funktionen:**
- SSL-Terminierung, HSTS
- Reverse Proxy mit eigenem Hostnamen je Dienst (die Top Stories laufen nicht unter
  einem Unterpfad)
- Optional: Basic Auth / Authelia (Top Stories dann mit `WEB_AUTH_MODE=proxy`)
- Miniflux vertraut `X-Forwarded-Proto` nur von `TRUSTED_PROXIES`
  (`TRUSTED_REVERSE_PROXY_NETWORKS`) und setzt erst dann `Secure`-Cookies;
  `/metrics` ist standardmäßig aus (`METRICS_COLLECTOR=0`)

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
| Intelligence | 1 Core (Grenze) | ~270 MB, Grenze 1 GB | wenige MB (SQLite) |
| **Gesamt** | **2 Cores** | **~1 GB** | **10+ GB** |

Gemessen am Intelligence Layer: 722 echte Artikel brauchen 0,74 s und 177 MB, 2000
Artikel (`scheduling.max_entries`) 2,3 s CPU und 265 MB (davon ~140 MB für die geladenen
Bibliotheken); die SQLite-Datei hat dann ~1 MB. Mit `OPENBLAS_NUM_THREADS=1` laufen 2
statt 8 Threads, ohne messbaren Zeitverlust.

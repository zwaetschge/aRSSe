"""
Local story database for the aRSSe Intelligence Layer.

Miniflux cannot store tags or custom metadata via its API, so clustering
results live in a small SQLite database that the web interface reads.

Story IDs are stable across clustering runs: a new cluster inherits the
ID of the previous story it continues (see _match_story_ids). Only
articles inside the lookback window count for a story's sources and
rank; older ones stay attached as earlier coverage until retention.

The schema is versioned with ``PRAGMA user_version``; see MIGRATIONS.
"""

import json
import logging
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

logger = logging.getLogger('arsse-intelligence')

# Feeds are untrusted input; real titles stay far below this
MAX_TITLE_CHARS = 500

# Default for web.earlier_articles_max: older articles shown on a story page
EARLIER_ARTICLES_MAX = 20

# Entries dated at most this far past the fetch cutoff are never pruned as
# deleted: Miniflux filters with a strict '>' on whole seconds
PRUNE_MARGIN = timedelta(seconds=60)

# Schema of the first release. Databases created before schema versioning
# (user_version 0 with these tables present) are treated as version 1.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY,
    feed_id      INTEGER,
    feed_title   TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    snippet      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'unread'
);

CREATE TABLE IF NOT EXISTS stories (
    id                TEXT PRIMARY KEY,
    headline_entry_id INTEGER NOT NULL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS story_entries (
    entry_id     INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    story_id     TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    is_duplicate INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_story_entries_story ON story_entries(story_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# published_at becomes the clamped date (see _entry_row) in the fixed format
# of db_timestamp(); the date as delivered by Miniflux moves to
# published_at_raw. Version 1 stored UTC isoformat, sometimes with
# microseconds, which the UPDATE cuts off.
SCHEMA_V2 = """
ALTER TABLE entries ADD COLUMN created_at TEXT;
ALTER TABLE entries ADD COLUMN published_at_raw TEXT;
UPDATE entries SET published_at = substr(published_at, 1, 19) || '+00:00'
    WHERE length(published_at) > 25;
"""

# Duplicates aRSSe marked read in Miniflux. A duplicate is marked only once:
# if the user sets it back to unread, that decision stands. save_run never
# touches this table; marked_at moves forward while the entry is still
# fetched (refresh_auto_marked), and cleanup drops rows once it is not.
#
# Earlier versions marked every unread duplicate read in each run and then
# stored it as read, and picked the canonical copy differently (ties went to
# the highest ID, 'longest' counted HTML). Without seeding, the first run
# after the upgrade could keep the copy already marked read and mark the
# other one too. A duplicate the user read themselves is seeded as well;
# that only makes an unread copy the canonical one, which is harmless.
# marked_at uses the format of db_timestamp().
SCHEMA_V3 = """
CREATE TABLE auto_marked (
    entry_id  INTEGER PRIMARY KEY,
    marked_at TEXT NOT NULL
);
INSERT INTO auto_marked (entry_id, marked_at)
    SELECT e.id, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM entries e
    JOIN story_entries se ON se.entry_id = e.id
    WHERE se.is_duplicate = 1 AND e.status = 'read';
"""


class _Rebuild:
    def __repr__(self) -> str:
        return 'REBUILD'


# Marks a schema change that cannot be migrated in place. The store only
# caches what Miniflux holds, so an existing database is moved aside to
# arsse.db.v<N>.bak and rebuilt; only story IDs, first_seen and the record
# of duplicates marked read (auto_marked) are lost.
REBUILD = _Rebuild()

# Append-only: MIGRATIONS[i] turns schema version i into version i + 1.
# Never edit or reorder a released entry; add a new one (SQL script or
# REBUILD) instead. Every script runs in one transaction together with the
# version bump, so a failing migration leaves the database untouched.
MIGRATIONS = [
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
]


class StoreTooNewError(RuntimeError):
    """The database was written by a newer aRSSe version."""


@dataclass
class FetchResult:
    """Entries of one fetch from Miniflux and how complete it is."""
    entries: list
    # published_after sent to Miniflux
    cutoff: datetime
    # False when max_entries cut the fetch short (only the newest IDs came back)
    complete: bool = True
    # Smallest entry ID fetched, None for an empty fetch
    min_id: Optional[int] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ClusterResult:
    """One cluster produced by a clustering run."""
    entry_ids: list
    headline_entry_id: int
    duplicate_ids: set
    # Duplicates that are the same item listed twice (same URL and title,
    # same or nearly the same text as a better-ranked copy that joined the
    # same group); a subset of duplicate_ids
    copy_ids: set = field(default_factory=set)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoryStore:
    """SQLite-backed storage for stories and their articles."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        rebuild_from = self._migrate()
        if rebuild_from is not None:
            self._move_aside(rebuild_from)
            self._migrate()

    def _migrate(self) -> Optional[int]:
        """
        Bring the schema to the latest version in one transaction.

        Returns:
            The current version if a pending migration is REBUILD (nothing
            was changed then), else None.

        Raises:
            StoreTooNewError: The database is newer than this code.
        """
        latest = len(MIGRATIONS)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            # IMMEDIATE: a second process waits instead of migrating twice
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > latest:
                    raise StoreTooNewError(
                        f"Datenbank stammt von neuerer aRSSe-Version (Schema {version}, "
                        f"diese Version kennt bis {latest}) – Backup einspielen oder "
                        f"{os.path.basename(self.db_path)} löschen ({self.db_path})")
                if version == 0 and _has_table(conn, 'entries'):
                    version = 1  # created before schema versioning
                pending = MIGRATIONS[version:]
                if version > 0 and any(m is REBUILD for m in pending):
                    conn.execute("ROLLBACK")
                    return version
                for migration in pending:
                    if migration is not REBUILD:  # a new database needs no rebuild
                        for statement in _statements(migration):
                            conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {latest}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        if pending:
            logger.info("Story database %s migrated from schema %d to %d",
                        self.db_path, version, latest)
        return None

    def _move_aside(self, version: int) -> None:
        """Rename the database to <db>.v<version>.bak so it can be rebuilt."""
        backup = f"{self.db_path}.v{version}.bak"
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            # Fold the WAL into the main file so the backup is complete
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        os.replace(self.db_path, backup)
        for suffix in ('-wal', '-shm'):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass
        logger.warning("Story database schema %d cannot be migrated in place; moved "
                       "it to %s and starting with an empty database (stories are "
                       "rebuilt from Miniflux in the next run)", version, backup)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection; one per operation keeps threads independent."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save_run(self, entries: list, clusters: list,
                 fetch: Optional[FetchResult] = None, min_sources: int = 1) -> dict:
        """
        Persist the result of a clustering run.

        Args:
            entries: Miniflux entry dicts that were part of this run.
            clusters: ClusterResult objects found in this run.
            fetch: How the entries were fetched. Its fetched_at caps future
                publication dates (defaults to now); with it, stored
                articles that Miniflux no longer returns are removed.
            min_sources: Feeds a story needs to be shown (web.min_sources);
                story IDs that were visible are kept in preference.

        Returns:
            Mapping of cluster index to the story ID it was stored under.
        """
        now = _now()
        fetched_at = fetch.fetched_at if fetch else datetime.now(timezone.utc)
        rows = [_entry_row(e, fetched_at) for e in entries]
        feed_of = {row['id']: _feed_key(row) for row in rows}

        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO entries (id, feed_id, feed_title, title, url,
                                        published_at, published_at_raw, created_at,
                                        snippet, status)
                   VALUES (:id, :feed_id, :feed_title, :title, :url,
                           :published_at, :published_at_raw, :created_at,
                           :snippet, :status)
                   ON CONFLICT(id) DO UPDATE SET
                       feed_title = excluded.feed_title,
                       title = excluded.title,
                       url = excluded.url,
                       -- Miniflux never changes published_at; without a
                       -- created_at the cap would move with every fetch
                       published_at = MIN(COALESCE(entries.published_at,
                                                   excluded.published_at),
                                          excluded.published_at),
                       published_at_raw = excluded.published_at_raw,
                       created_at = excluded.created_at,
                       snippet = excluded.snippet,
                       status = excluded.status""",
                rows,
            )

            conn.execute("CREATE TEMP TABLE IF NOT EXISTS run_ids (id INTEGER PRIMARY KEY)")
            conn.execute("DELETE FROM temp.run_ids")
            conn.executemany("INSERT OR IGNORE INTO temp.run_ids (id) VALUES (?)",
                             [(row['id'],) for row in rows])

            if fetch is not None:
                _prune_deleted(conn, fetch)

            previous = _previous_stories(conn, min_sources)

            # Articles seen in this run get re-assigned from scratch
            conn.execute("""DELETE FROM story_entries
                            WHERE entry_id IN (SELECT id FROM temp.run_ids)""")

            assigned = _match_story_ids(clusters, previous, feed_of, min_sources)
            for idx, cluster in enumerate(clusters):
                story_id = assigned[idx]
                conn.execute(
                    """INSERT INTO stories (id, headline_entry_id, first_seen, last_seen)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           headline_entry_id = excluded.headline_entry_id,
                           last_seen = excluded.last_seen""",
                    (story_id, cluster.headline_entry_id, now, now),
                )
                conn.executemany(
                    "INSERT INTO story_entries (entry_id, story_id, is_duplicate) "
                    "VALUES (?, ?, ?)",
                    [(eid, story_id, int(eid in cluster.duplicate_ids))
                     for eid in cluster.entry_ids],
                )

            # Stories that lost all their articles are gone
            conn.execute("""DELETE FROM stories WHERE id NOT IN
                            (SELECT DISTINCT story_id FROM story_entries)""")
            conn.execute("DROP TABLE temp.run_ids")

        return assigned

    def record_auto_marked(self, entry_ids: list) -> None:
        """Record that aRSSe marked entries as read in Miniflux."""
        marked_at = db_timestamp(datetime.now(timezone.utc))
        with self._connect() as conn:
            conn.executemany("UPDATE entries SET status = 'read' WHERE id = ?",
                             [(i,) for i in entry_ids])
            conn.executemany("INSERT OR REPLACE INTO auto_marked (entry_id, marked_at) "
                             "VALUES (?, ?)", [(i, marked_at) for i in entry_ids])

    def refresh_auto_marked(self, fetched_ids: list) -> None:
        """
        Keep the records of marked entries that the last fetch returned.

        Sets marked_at to now for them, so cleanup counts from the last
        fetch rather than from marking: Miniflux does not clamp dates, and
        an entry dated days ahead stays in the lookback window until then.
        """
        now = db_timestamp(datetime.now(timezone.utc))
        with self._connect() as conn:
            marked = {row['entry_id'] for row in conn.execute("SELECT entry_id FROM auto_marked")}
            conn.executemany("UPDATE auto_marked SET marked_at = ? WHERE entry_id = ?",
                             [(now, i) for i in marked.intersection(fetched_ids)])

    def auto_marked_ids(self) -> set:
        """IDs of entries that aRSSe marked as read before; they are never marked again."""
        with self._connect() as conn:
            return {row['entry_id'] for row in conn.execute("SELECT entry_id FROM auto_marked")}

    def cleanup(self, retention_days: int, lookback_hours: int = 24) -> None:
        """
        Remove articles and stories older than the retention period.

        Retention applies per article: a story that keeps running for weeks
        loses its old articles, and only then the story itself. Records of
        entries marked read are kept for lookback_hours plus a day after
        the last fetch that returned the entry (see refresh_auto_marked).
        The window only moves forward, so such an entry is not fetched
        again; the extra day is a margin, e.g. for runs cut off at
        max_entries.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        marked_cutoff = now - timedelta(hours=lookback_hours + 24)
        with self._connect() as conn:
            conn.execute("DELETE FROM auto_marked WHERE marked_at < ?",
                         (db_timestamp(marked_cutoff),))
            conn.execute("""DELETE FROM story_entries WHERE entry_id IN
                            (SELECT id FROM entries WHERE published_at < ?)""",
                         (db_timestamp(cutoff),))
            conn.execute("DELETE FROM stories WHERE last_seen < ?", (cutoff.isoformat(),))
            conn.execute("""DELETE FROM stories WHERE id NOT IN
                            (SELECT DISTINCT story_id FROM story_entries)""")
            conn.execute("""DELETE FROM entries
                            WHERE id NOT IN (SELECT entry_id FROM story_entries)""")

    def set_meta(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (key, json.dumps(value)))

    def get_meta(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def top_stories(self, max_age_hours: int, limit: int, min_sources: int = 1,
                    earlier_max: int = EARLIER_ARTICLES_MAX) -> list:
        """
        Return ranked stories with their articles.

        Only articles of the last max_age_hours count: a story needs
        min_sources feeds among them, and ranking favours stories covered
        by many distinct sources and decays with the age of the newest
        article. Older articles are listed in 'earlier_articles'.
        """
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=max_age_hours)
        with self._connect() as conn:
            # One read snapshot: a run committing in between must not show
            # an article under two stories
            conn.execute("BEGIN")
            story_rows = conn.execute(
                "SELECT * FROM stories WHERE last_seen >= ?", (since.isoformat(),)
            ).fetchall()
            stories = [self._load_story(conn, row, since, earlier_max) for row in story_rows]

        stories = [s for s in stories
                   if s['articles'] and s['source_count'] >= min_sources]
        for story in stories:
            newest = parse_date(story['articles'][0]['published_at'])
            age_hours = (now - newest).total_seconds() / 3600 if newest else max_age_hours
            story['score'] = story['source_count'] / (1 + max(age_hours, 0) / 12)

        stories.sort(key=lambda s: s['score'], reverse=True)
        return stories[:limit]

    def get_story(self, story_id: str, max_age_hours: int,
                  earlier_max: int = EARLIER_ARTICLES_MAX) -> Optional[dict]:
        """
        Return one story, or None if it does not exist (any more).

        Like top_stories, only articles of the last max_age_hours count;
        a story without any is over and not found.
        """
        since = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        with self._connect() as conn:
            conn.execute("BEGIN")  # story row and articles from one snapshot
            row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
            story = self._load_story(conn, row, since, earlier_max) if row else None
        return story if story and story['articles'] else None

    @staticmethod
    def _load_story(conn: sqlite3.Connection, row: sqlite3.Row, since: datetime,
                    earlier_max: int) -> dict:
        """
        Load a story; counts cover only articles published since 'since'.

        Older articles that are still attached go to 'earlier_articles'
        (newest first, at most earlier_max). Nothing checks whether they
        still exist in Miniflux (see _prune_deleted).
        """
        all_articles = [dict(a) for a in conn.execute(
            """SELECT e.*, se.is_duplicate FROM story_entries se
               JOIN entries e ON e.id = se.entry_id
               WHERE se.story_id = ?
               ORDER BY e.published_at DESC, e.id DESC""",
            (row['id'],),
        ).fetchall()]
        cut = db_timestamp(since)
        articles = [a for a in all_articles if (a['published_at'] or '') >= cut]
        earlier = [a for a in all_articles if (a['published_at'] or '') < cut]
        # The stored headline may have left the window: the newest article
        # stands in, but not a duplicate that aRSSe marked read in Miniflux
        headline = next((a for a in articles if a['id'] == row['headline_entry_id']), None) \
            or next((a for a in articles if not a['is_duplicate']),
                    articles[0] if articles else None)
        return {
            'id': row['id'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
            'headline': headline,
            'articles': articles,
            'article_count': len(articles),
            'source_count': len({_feed_key(a) for a in articles}),
            'earlier_articles': earlier[:max(earlier_max, 0)],
            'earlier_count': len(earlier),
        }


def _entry_row(entry: dict, fetched_at: datetime) -> dict:
    """
    Turn a Miniflux entry into a row of the entries table.

    Feeds may date articles in the future (wrong time zone, scheduled
    items). Such an article would never age and pin its story to the top,
    so published_at is capped at the time Miniflux ingested the entry
    (created_at) and at the fetch time; the feed's date is kept in
    published_at_raw.
    """
    feed = entry.get('feed') or {}
    raw = entry.get('published_at')
    created = parse_date(entry.get('created_at'))
    dates = [d for d in (parse_date(raw), created, fetched_at) if d]
    return {
        'id': entry['id'],
        'feed_id': entry.get('feed_id') or feed.get('id'),
        'feed_title': feed.get('title') or '',
        'title': (entry.get('title') or '')[:MAX_TITLE_CHARS],
        'url': entry.get('url') or '',
        'published_at': db_timestamp(min(dates)),
        'published_at_raw': raw if isinstance(raw, str) else None,
        'created_at': db_timestamp(created) if created else None,
        'snippet': entry.get('_snippet', ''),
        'status': entry.get('status', 'unread'),
    }


def _feed_key(article) -> object:
    """Identify an article's source by feed ID: two subscriptions may share a title."""
    return article['feed_id'] or article['feed_title']


def _prune_deleted(conn: sqlite3.Connection, fetch: FetchResult) -> int:
    """
    Remove stored articles that no longer exist in Miniflux.

    Flush history, archiving and removed feeds delete entries in Miniflux;
    their links would return 404. The stored published_at is at most the
    date Miniflux filters on, so an article dated after the fetch cutoff
    that is missing from the run must have been returned if it still
    existed. A truncated fetch only holds the newest IDs, so there only
    IDs from min_id on are checked. Older articles cannot be checked and
    stay until retention; the story page therefore links them to the
    publisher, not to Miniflux. Expects the run's IDs in temp.run_ids.

    Returns:
        Number of articles removed.
    """
    condition = "published_at > :after AND id NOT IN (SELECT id FROM temp.run_ids)"
    if not fetch.complete:
        if fetch.min_id is None:
            return 0
        condition += " AND id >= :min_id"
    params = {'after': db_timestamp(fetch.cutoff + PRUNE_MARGIN), 'min_id': fetch.min_id}
    conn.execute(f"""DELETE FROM story_entries WHERE entry_id IN
                     (SELECT id FROM entries WHERE {condition})""", params)
    removed = conn.execute(f"DELETE FROM entries WHERE {condition}", params).rowcount
    if removed:
        logger.info("Removed %d articles that no longer exist in Miniflux", removed)
    return removed


@dataclass
class _PreviousStory:
    """A stored story as far as this run's articles are concerned."""
    entry_ids: set = field(default_factory=set)
    headline_entry_id: Optional[int] = None
    feeds: set = field(default_factory=set)
    visible: bool = False


def _previous_stories(conn: sqlite3.Connection, min_sources: int) -> dict:
    """
    Map story ID to the stored stories holding articles of this run.

    Only the run's articles (temp.run_ids) are loaded: they are the ones a
    cluster can share, and they decide whether the story was visible
    (at least min_sources feeds inside the window).
    """
    previous = defaultdict(_PreviousStory)
    for row in conn.execute(
            """SELECT se.story_id, se.entry_id, s.headline_entry_id,
                      e.feed_id, e.feed_title
               FROM story_entries se
               JOIN temp.run_ids r ON r.id = se.entry_id
               JOIN stories s ON s.id = se.story_id
               JOIN entries e ON e.id = se.entry_id"""):
        story = previous[row['story_id']]
        story.entry_ids.add(row['entry_id'])
        story.headline_entry_id = row['headline_entry_id']
        story.feeds.add(_feed_key(row))
    for story in previous.values():
        story.visible = len(story.feeds) >= min_sources
    return dict(previous)


def _match_story_ids(clusters: list, previous: dict, feed_of: dict,
                     min_sources: int) -> dict:
    """
    Decide which cluster continues which stored story.

    Every pair of cluster and previous story that share articles is
    ranked, and pairs are taken greedily, best first, while neither side
    is taken yet. A pair ranks higher, in this order, if:

    1. both the story and the cluster are visible (min_sources feeds): an
       ID the user has seen stays on the front page. Without this, a
       hidden single-feed series absorbs a real story whenever the two
       merge for one run, and keeps its ID when they split again;
    2. among such pairs, they share more articles: the real continuation
       keeps the ID, not a side topic that took one article along;
    3. the story was visible and the cluster holds its headline article.
       If no visible cluster continues it, the ID stays with the title
       the user saw rather than going to a larger hidden series, even
       when the event itself is down to one feed for a run;
    4. they share more articles, then the cluster holds the headline
       (so on an even split the ID follows the title the user saw);
    5. the cluster is visible, then larger, then earlier in the list.

    Clusters left without a story get a new ID.

    Returns:
        Mapping of cluster index to story ID.
    """
    story_of = {eid: sid for sid, story in previous.items() for eid in story.entry_ids}
    candidates = []
    for idx, cluster in enumerate(clusters):
        members = set(cluster.entry_ids)
        # An article missing from entries counts as a source of its own
        visible = len({feed_of.get(eid, eid) for eid in members}) >= min_sources
        votes = Counter(story_of[eid] for eid in members if eid in story_of)
        for story_id, overlap in votes.items():
            story = previous[story_id]
            both = story.visible and visible
            has_headline = story.headline_entry_id in members
            key = (both, overlap if both else 0, story.visible and has_headline,
                   overlap, has_headline, visible, len(members), -idx)
            candidates.append((key, story_id, idx))
    candidates.sort(reverse=True)

    assigned = {}
    taken = set()
    for _, story_id, idx in candidates:
        if idx not in assigned and story_id not in taken:
            assigned[idx] = story_id
            taken.add(story_id)
    for idx in range(len(clusters)):
        if idx not in assigned:
            assigned[idx] = uuid.uuid4().hex[:12]
    return assigned


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _statements(script: str) -> Iterator[str]:
    """Split an SQL script into single statements (execute() takes only one)."""
    statement = ''
    for part in script.split(';'):
        statement += part + ';'
        # A ';' inside a string literal or comment does not end the statement
        if sqlite3.complete_statement(statement):
            if statement.strip(' \t\n;'):
                yield statement
            statement = ''
    if statement.strip(' \t\n;'):
        yield statement  # incomplete: let SQLite report the syntax error


def db_timestamp(value: datetime) -> str:
    """
    Format a date the way the entries table stores it.

    Always UTC, whole seconds and '+00:00' (e.g. 2026-09-24T08:15:00+00:00),
    so dates can be compared as strings in SQL.
    """
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

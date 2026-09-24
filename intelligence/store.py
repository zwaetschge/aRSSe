"""
Local story database for the aRSSe Intelligence Layer.

Miniflux cannot store tags or custom metadata via its API, so clustering
results live in a small SQLite database that the web interface reads.

Story IDs are stable across clustering runs: a new cluster inherits the
ID of the previous story that shares the most articles with it.

The schema is versioned with ``PRAGMA user_version``; see MIGRATIONS.
"""

import json
import logging
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

logger = logging.getLogger('arsse-intelligence')

# Feeds are untrusted input; real titles stay far below this
MAX_TITLE_CHARS = 500

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


class _Rebuild:
    def __repr__(self) -> str:
        return 'REBUILD'


# Marks a schema change that cannot be migrated in place. The store only
# caches what Miniflux holds, so an existing database is moved aside to
# arsse.db.v<N>.bak and rebuilt; only story IDs and first_seen are lost.
REBUILD = _Rebuild()

# Append-only: MIGRATIONS[i] turns schema version i into version i + 1.
# Never edit or reorder a released entry; add a new one (SQL script or
# REBUILD) instead. Every script runs in one transaction together with the
# version bump, so a failing migration leaves the database untouched.
MIGRATIONS = [
    SCHEMA_V1,
    SCHEMA_V2,
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
                 fetch: Optional[FetchResult] = None) -> dict:
        """
        Persist the result of a clustering run.

        Args:
            entries: Miniflux entry dicts that were part of this run.
            clusters: ClusterResult objects found in this run.
            fetch: How the entries were fetched; its fetched_at caps
                future publication dates (defaults to now).

        Returns:
            Mapping of cluster index to the story ID it was stored under.
        """
        now = _now()
        fetched_at = fetch.fetched_at if fetch else datetime.now(timezone.utc)
        run_ids = [e['id'] for e in entries]
        assigned = {}

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
                [_entry_row(e, fetched_at) for e in entries],
            )

            previous = _story_lookup(conn, run_ids)

            # Articles seen in this run get re-assigned from scratch
            conn.executemany("DELETE FROM story_entries WHERE entry_id = ?",
                             [(i,) for i in run_ids])

            # Largest clusters pick their inherited story ID first
            order = sorted(range(len(clusters)),
                           key=lambda i: len(clusters[i].entry_ids), reverse=True)
            claimed = set()
            for idx in order:
                cluster = clusters[idx]
                story_id = _inherit_story_id(cluster.entry_ids, previous, claimed)
                claimed.add(story_id)
                assigned[idx] = story_id

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

        return assigned

    def mark_read(self, entry_ids: list) -> None:
        """Record that entries were marked as read in Miniflux."""
        with self._connect() as conn:
            conn.executemany("UPDATE entries SET status = 'read' WHERE id = ?",
                             [(i,) for i in entry_ids])

    def cleanup(self, retention_days: int) -> None:
        """Remove stories and articles older than the retention period."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM stories WHERE last_seen < ?", (cutoff,))
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

    def top_stories(self, max_age_hours: int, limit: int, min_sources: int = 1) -> list:
        """
        Return ranked stories with their articles.

        Ranking favours stories covered by many distinct sources and
        decays with the age of the newest article.
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        with self._connect() as conn:
            # One read snapshot: a run committing in between must not show
            # an article under two stories
            conn.execute("BEGIN")
            story_rows = conn.execute(
                "SELECT * FROM stories WHERE last_seen >= ?", (since,)
            ).fetchall()
            stories = [self._load_story(conn, row) for row in story_rows]

        now = datetime.now(timezone.utc)
        for story in stories:
            newest = parse_date(story['articles'][0]['published_at']) if story['articles'] else None
            age_hours = (now - newest).total_seconds() / 3600 if newest else max_age_hours
            story['score'] = story['source_count'] / (1 + max(age_hours, 0) / 12)

        stories = [s for s in stories if s['source_count'] >= min_sources]
        stories.sort(key=lambda s: s['score'], reverse=True)
        return stories[:limit]

    def get_story(self, story_id: str) -> Optional[dict]:
        """Return one story, or None if it does not exist (any more)."""
        with self._connect() as conn:
            conn.execute("BEGIN")  # story row and articles from one snapshot
            row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
            story = self._load_story(conn, row) if row else None
        return story if story and story['articles'] else None

    @staticmethod
    def _load_story(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        articles = [dict(a) for a in conn.execute(
            """SELECT e.*, se.is_duplicate FROM story_entries se
               JOIN entries e ON e.id = se.entry_id
               WHERE se.story_id = ?
               ORDER BY e.published_at DESC""",
            (row['id'],),
        ).fetchall()]
        headline = next((a for a in articles if a['id'] == row['headline_entry_id']),
                        articles[0] if articles else None)
        return {
            'id': row['id'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
            'headline': headline,
            'articles': articles,
            'article_count': len(articles),
            # Feed IDs, not titles: two subscriptions may share a display name
            'source_count': len({a['feed_id'] or a['feed_title'] for a in articles}),
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


def _story_lookup(conn: sqlite3.Connection, entry_ids: list) -> dict:
    """Map entry ID to its current story ID."""
    lookup = {}
    for chunk_start in range(0, len(entry_ids), 500):
        chunk = entry_ids[chunk_start:chunk_start + 500]
        placeholders = ','.join('?' * len(chunk))
        for row in conn.execute(
                f"SELECT entry_id, story_id FROM story_entries "
                f"WHERE entry_id IN ({placeholders})", chunk):
            lookup[row['entry_id']] = row['story_id']
    return lookup


def _inherit_story_id(entry_ids: list, previous: dict, claimed: set) -> str:
    """Pick the previous story sharing most articles, or mint a new ID."""
    votes = Counter(previous[e] for e in entry_ids if e in previous)
    for story_id, _ in votes.most_common():
        if story_id not in claimed:
            return story_id
    return uuid.uuid4().hex[:12]


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

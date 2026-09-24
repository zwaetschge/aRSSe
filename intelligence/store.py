"""
Local story database for the aRSSe Intelligence Layer.

Miniflux cannot store tags or custom metadata via its API, so clustering
results live in a small SQLite database that the web interface reads.

Story IDs are stable across clustering runs: a new cluster inherits the
ID of the previous story that shares the most articles with it.
"""

import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

SCHEMA = """
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
        with self._connect() as conn:
            conn.executescript(SCHEMA)

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

    def save_run(self, entries: list, clusters: list) -> dict:
        """
        Persist the result of a clustering run.

        Args:
            entries: Miniflux entry dicts that were part of this run.
            clusters: ClusterResult objects found in this run.

        Returns:
            Mapping of cluster index to the story ID it was stored under.
        """
        now = _now()
        run_ids = [e['id'] for e in entries]
        assigned = {}

        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO entries (id, feed_id, feed_title, title, url,
                                        published_at, snippet, status)
                   VALUES (:id, :feed_id, :feed_title, :title, :url,
                           :published_at, :snippet, :status)
                   ON CONFLICT(id) DO UPDATE SET
                       feed_title = excluded.feed_title,
                       title = excluded.title,
                       url = excluded.url,
                       published_at = excluded.published_at,
                       snippet = excluded.snippet,
                       status = excluded.status""",
                [_entry_row(e) for e in entries],
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
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
            return self._load_story(conn, row) if row else None

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
            'source_count': len({a['feed_title'] for a in articles}),
        }


def _entry_row(entry: dict) -> dict:
    feed = entry.get('feed') or {}
    return {
        'id': entry['id'],
        'feed_id': entry.get('feed_id') or feed.get('id'),
        'feed_title': feed.get('title', ''),
        'title': entry.get('title', ''),
        'url': entry.get('url', ''),
        'published_at': _normalize_date(entry.get('published_at')),
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


def _normalize_date(value: Optional[str]) -> Optional[str]:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


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

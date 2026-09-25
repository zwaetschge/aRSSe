import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402
from store import StoryStore  # noqa: E402

FEEDS = {
    1: {'id': 1, 'title': 'Tagesschau', 'site_url': 'https://www.tagesschau.de'},
    2: {'id': 2, 'title': 'Spiegel', 'site_url': 'https://www.spiegel.de'},
    3: {'id': 3, 'title': 'Zeit', 'site_url': 'https://www.zeit.de'},
    4: {'id': 4, 'title': 'Heise', 'site_url': 'https://www.heise.de'},
}


def make_entry(entry_id, feed_id, title, content, hours_ago=1, status='unread'):
    published = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        'id': entry_id,
        'feed_id': feed_id,
        'feed': FEEDS[feed_id],
        'title': title,
        'content': f'<p>{content}</p>',
        'url': f'https://example.org/{entry_id}',
        'published_at': published.isoformat().replace('+00:00', 'Z'),
        'status': status,
    }


BUDGET = ('Der Bundestag hat den Bundeshaushalt für das kommende Jahr beschlossen. '
          'Finanzminister verteidigt die Schuldenbremse, die Opposition kritisiert '
          'Kürzungen bei Bildung und Infrastruktur im Haushalt. Der Bundesrat '
          'muss dem Haushalt im Dezember noch zustimmen.')
CHIP = ('Ein neuer Prozessor mit deutlich höherer Rechenleistung wurde vorgestellt. '
        'Der Chiphersteller verspricht effizientere Grafikeinheiten und längere '
        'Akkulaufzeit für Notebooks mit dem neuen Prozessor.')


def sample_entries():
    return [
        make_entry(1, 1, 'Bundestag beschließt Haushalt', BUDGET),
        make_entry(2, 2, 'Bundestag beschließt Haushalt', BUDGET),  # duplicate of 1
        make_entry(3, 3, 'Haushalt: Bundestag stimmt zu',
                   'Nach langer Debatte stimmt der Bundestag dem Haushalt zu. '
                   'Die Schuldenbremse bleibt, die Opposition kritisiert Kürzungen.'),
        make_entry(4, 4, 'Neuer Prozessor vorgestellt', CHIP),
        make_entry(5, 2, 'Chiphersteller zeigt neuen Prozessor',
                   'Der neue Prozessor soll Notebooks schneller machen. Erste Benchmarks '
                   'zur Rechenleistung und Akkulaufzeit folgen in wenigen Wochen.'),
        make_entry(6, 1, 'Wetter: Sonne am Wochenende',
                   'Meteorologen erwarten sommerliche Temperaturen und viel Sonnenschein.'),
    ]


class FakeClient:
    """
    Minimal stand-in for miniflux.Client with the real method signatures.

    get_entries filters, sorts and pages like Miniflux 2.3.3 (including
    keyset paging with before_entry_id/after_entry_id, changed_after and
    globally_visible, which leaves out feeds and categories with
    hide_globally). update_entries sets changed_at like Miniflux. after_page,
    if set, is called after every page, e.g. to simulate Miniflux storing
    new entries while the clusterer pages. error, if set, is raised by
    get_entries and update_entries.
    """

    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.marked = []
        self.after_page = None
        self.error = None
        self.user = {'id': 2, 'username': 'leser', 'is_admin': False}

    def me(self):
        if isinstance(self.user, Exception):
            raise self.user
        return dict(self.user)

    def get_entries(self, *, status=None, published_after=None, order=None,
                    direction=None, limit=100, offset=0, before_entry_id=None,
                    after_entry_id=None, changed_after=None, globally_visible=None):
        self.calls.append({'status': status, 'published_after': published_after,
                           'order': order, 'direction': direction, 'limit': limit,
                           'offset': offset, 'before_entry_id': before_entry_id,
                           'after_entry_id': after_entry_id, 'changed_after': changed_after,
                           'globally_visible': globally_visible})
        if self.error:
            raise self.error
        if isinstance(status, str):
            status = [status]
        selected = [e for e in self.entries
                    if (not status or e.get('status', 'unread') in status)
                    and (published_after is None or _timestamp(e) > published_after)
                    and (before_entry_id is None or e['id'] < before_entry_id)
                    and (after_entry_id is None or e['id'] > after_entry_id)
                    and (changed_after is None
                         or _timestamp(e, 'changed_at') > changed_after)
                    and not (globally_visible and _hidden_globally(e))]
        if order:
            # Like Miniflux: ties keep storage order, which is what breaks offsets
            selected.sort(key=lambda e: e['id'] if order == 'id' else e.get(order) or '',
                          reverse=direction == 'desc')
        page = selected[offset:offset + limit]
        result = {'total': len(selected), 'entries': [dict(e) for e in page]}
        if self.after_page:
            self.after_page(len(self.calls))
        return result

    def update_entries(self, entry_ids, status):
        if self.error:
            raise self.error
        self.marked.append((list(entry_ids), status))
        now = datetime.now(timezone.utc).isoformat()
        for e in self.entries:
            if e['id'] in entry_ids:
                e['status'] = status
                e['changed_at'] = now
        return True


def _hidden_globally(entry):
    feed = entry.get('feed') or {}
    return bool(feed.get('hide_globally') or (feed.get('category') or {}).get('hide_globally'))


def _timestamp(entry, key='published_at'):
    value = entry.get(key) or entry['published_at']  # new entries: changed = created
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def downgrade_to_v2(path):
    """
    Turn a database written by this version back into schema 2.

    Stands in for a database of the version before auto_marked: save_run
    only works on the latest schema.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            DROP TABLE auto_marked;
            DROP TABLE user_read;
            DROP INDEX idx_entries_published;
            ALTER TABLE entries DROP COLUMN section;
            ALTER TABLE stories DROP COLUMN topic_key;
            PRAGMA user_version = 2;
        """)
    finally:
        conn.close()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / 'test.db')
    cfg.deduplication.threshold = 0.9
    return cfg


@pytest.fixture
def store(config):
    return StoryStore(config.storage.db_path)

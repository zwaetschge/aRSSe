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
          'Kürzungen bei Bildung und Infrastruktur im Haushalt.')
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
    keyset paging with before_entry_id). after_page, if set, is called
    after every page, e.g. to simulate Miniflux storing new entries while
    the clusterer pages.
    """

    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.marked = []
        self.after_page = None

    def get_entries(self, *, status=None, published_after=None, order=None,
                    direction=None, limit=100, offset=0, before_entry_id=None,
                    after_entry_id=None):
        self.calls.append({'status': status, 'published_after': published_after,
                           'order': order, 'direction': direction, 'limit': limit,
                           'offset': offset, 'before_entry_id': before_entry_id})
        selected = [e for e in self.entries
                    if (not status or e.get('status', 'unread') in status)
                    and (published_after is None or _timestamp(e) > published_after)
                    and (before_entry_id is None or e['id'] < before_entry_id)
                    and (after_entry_id is None or e['id'] > after_entry_id)]
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
        self.marked.append((list(entry_ids), status))
        for e in self.entries:
            if e['id'] in entry_ids:
                e['status'] = status
        return True


def _timestamp(entry):
    published = datetime.fromisoformat(entry['published_at'].replace('Z', '+00:00'))
    return published.timestamp()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / 'test.db')
    cfg.deduplication.threshold = 0.9
    return cfg


@pytest.fixture
def store(config):
    return StoryStore(config.storage.db_path)

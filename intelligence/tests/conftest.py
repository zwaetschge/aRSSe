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
    """Minimal stand-in for miniflux.Client with the real method signatures."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.marked = []

    def get_entries(self, *, status=None, published_after=None, order=None,
                    direction=None, limit=100, offset=0):
        self.calls.append({'status': status, 'published_after': published_after,
                           'limit': limit, 'offset': offset})
        page = self.entries[offset:offset + limit]
        return {'total': len(self.entries), 'entries': [dict(e) for e in page]}

    def update_entries(self, entry_ids, status):
        self.marked.append((list(entry_ids), status))
        for e in self.entries:
            if e['id'] in entry_ids:
                e['status'] = status
        return True


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / 'test.db')
    cfg.deduplication.threshold = 0.9
    return cfg


@pytest.fixture
def store(config):
    return StoryStore(config.storage.db_path)

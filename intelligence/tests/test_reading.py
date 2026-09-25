"""
Read state: 'Story gelesen', hiding read stories, 'N neu' and the status
sync that takes over articles read in Miniflux.
"""

import base64
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

import news_clustering
from conftest import FakeClient, sample_entries
from news_clustering import NewsClusterer, StatusSync, run_status_sync
from store import ClusterResult, FetchResult
from web import create_app, safe_next

SAME_ORIGIN = {'Sec-Fetch-Site': 'same-origin'}


def entry(entry_id, feed_id, hours_ago=1.0, status='unread', title=None):
    now = datetime.now(timezone.utc)
    return {
        'id': entry_id,
        'feed_id': feed_id,
        'feed': {'id': feed_id, 'title': f'Feed {feed_id}'},
        'title': title or f'Artikel {entry_id}',
        'url': f'https://example.org/{entry_id}',
        'published_at': (now - timedelta(hours=hours_ago)).isoformat(),
        'status': status,
    }


def story_setup(store):
    """
    Story A: 1-4 in the window (2 read by the user, 3 a duplicate aRSSe
    marked read) and 5 unread but older than the window; story B: 6, 7.
    """
    entries = [entry(1, 1), entry(2, 2, status='read'), entry(3, 3, status='read'),
               entry(4, 4, hours_ago=2), entry(5, 5, hours_ago=30),
               entry(6, 1, hours_ago=3), entry(7, 2, hours_ago=3)]
    assigned = store.save_run(entries, [ClusterResult([1, 2, 3, 4, 5], 1, {3}),
                                        ClusterResult([6, 7], 6, set())])
    store.record_auto_marked([3])
    return entries, assigned[0], assigned[1]


def listed(html):
    return html.count('<article class="story">')


def user_read_rows(store):
    conn = sqlite3.connect(store.db_path)
    try:
        return sorted(row[0] for row in conn.execute("SELECT entry_id FROM user_read"))
    finally:
        conn.close()


# --- 'Story gelesen' ------------------------------------------------------------

def test_story_read_marks_the_unread_articles_in_the_window(config, store):
    _, story_a, story_b = story_setup(store)
    client = FakeClient([])
    http = create_app(config, store, client).test_client()
    html = http.get('/').get_data(as_text=True)
    assert listed(html) == 2
    assert f'action="/story/{story_a}/gelesen"><button>Story gelesen (2)</button>' in html

    response = http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN)
    assert response.status_code == 303
    assert response.headers['Location'] == '/'
    # Only 1 and 4: 2 and 3 are read, 5 is outside the window
    assert len(client.marked) == 1
    assert sorted(client.marked[0][0]) == [1, 4] and client.marked[0][1] == 'read'
    # Recorded as the user's marks, not as duplicates aRSSe marked
    assert user_read_rows(store) == [1, 4]
    assert store.auto_marked_ids() == {3}

    html = http.get('/').get_data(as_text=True)
    assert listed(html) == 1 and f'/story/{story_a}/gelesen' not in html
    assert '1 gelesene zeigen' in html and 'href="/?alle=1"' in html
    html = http.get('/?alle=1').get_data(as_text=True)
    assert listed(html) == 2 and '· gelesen' in html and 'Gelesene ausblenden' in html
    assert [s['id'] for s in http.get('/api/stories').get_json()] == [story_b]
    assert {s['id'] for s in http.get('/api/stories?alle=1').get_json()} == {story_a, story_b}
    # The story page stays reachable and has nothing left to mark
    page = http.get(f'/story/{story_a}').get_data(as_text=True)
    assert 'Story gelesen' not in page and '· gelesen' in page


def test_story_read_returns_to_the_page_it_came_from(config, store):
    _, story_a, _ = story_setup(store)
    http = create_app(config, store, FakeClient([])).test_client()
    page = http.get(f'/story/{story_a}?ansicht=chronologisch').get_data(as_text=True)
    assert (f'<input type="hidden" name="next" value="/story/{story_a}?ansicht=chronologisch">'
            in page)
    response = http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN,
                         data={'next': f'/story/{story_a}?auto=1'})
    assert response.headers['Location'] == f'/story/{story_a}?auto=1'


def test_story_read_on_the_last_page_returns_to_the_new_last_page(config, store):
    config.web.page_size = 1
    _, story_a, story_b = story_setup(store)
    http = create_app(config, store, FakeClient([])).test_client()
    page = http.get('/?seite=2&auto=1').get_data(as_text=True)
    assert f'/story/{story_b}/gelesen"><input type="hidden" name="next" ' \
        f'value="/?seite=2&amp;auto=1">' in page
    # With read stories listed, page 2 stays
    response = http.post(f'/story/{story_b}/gelesen', headers=SAME_ORIGIN,
                         data={'next': '/?alle=1&seite=2&auto=1'})
    assert response.headers['Location'] == '/?alle=1&seite=2&auto=1'
    # Without them it is gone: back to the last page there is, not a 404
    response = http.post(f'/story/{story_b}/gelesen', headers=SAME_ORIGIN,
                         data={'next': '/?seite=2&auto=1'})
    assert response.headers['Location'] == '/?auto=1'
    assert http.get('/?seite=2').status_code == 404
    assert http.get(response.headers['Location']).status_code == 200


@pytest.mark.parametrize('target', ['//evil.example', '//evil.example/x', '/\\evil.example',
                                    'http://evil.example/', 'https:evil.example', '',
                                    'evil', '/\r\nSet-Cookie: x=1', '/' + 'a' * 3000])
def test_story_read_never_redirects_elsewhere(config, store, target):
    _, story_a, _ = story_setup(store)
    http = create_app(config, store, FakeClient([])).test_client()
    response = http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN,
                         data={'next': target})
    assert response.status_code == 303
    assert response.headers['Location'] == '/'
    assert safe_next(target) == '/'


def test_cross_site_story_read_is_rejected(config, store):
    _, story_a, _ = story_setup(store)
    client = FakeClient([])
    http = create_app(config, store, client).test_client()
    for headers in ({'Origin': 'http://evil.example'},
                    {'Sec-Fetch-Site': 'cross-site'},
                    {'Sec-Fetch-Site': 'same-site'},
                    {}):
        response = http.post(f'/story/{story_a}/gelesen', headers=headers,
                             data={'next': '//evil.example'})
        assert response.status_code == 403, headers
    assert client.marked == []
    assert user_read_rows(store) == []
    # Same host in Origin (older browsers without Sec-Fetch-Site) is fine
    assert http.post(f'/story/{story_a}/gelesen', headers={'Origin': 'http://localhost'}
                     ).status_code == 303


def test_story_read_needs_login(config, store):
    config.web.auth.mode = 'basic'
    config.web.auth.username = 'leser'
    config.web.auth.password = 'geheim'
    _, story_a, _ = story_setup(store)
    client = FakeClient([])
    http = create_app(config, store, client).test_client()
    assert http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN).status_code == 401
    assert client.marked == []
    login = {'Authorization': 'Basic ' + base64.b64encode(b'leser:geheim').decode()}
    assert http.post(f'/story/{story_a}/gelesen',
                     headers={**SAME_ORIGIN, **login}).status_code == 303
    assert len(client.marked) == 1


def test_unreachable_miniflux_marks_nothing(config, store, caplog):
    _, story_a, _ = story_setup(store)
    client = FakeClient([])
    client.error = requests.ConnectionError('miniflux: connection refused')
    http = create_app(config, store, client).test_client()
    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        response = http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN)
    assert response.status_code == 502
    assert 'nicht als gelesen markiert' in response.get_data(as_text=True)
    assert user_read_rows(store) == []
    assert listed(http.get('/').get_data(as_text=True)) == 2


def test_without_api_key_there_is_no_read_button(config, store):
    _, story_a, _ = story_setup(store)
    http = create_app(config, store).test_client()
    assert 'Story gelesen' not in http.get('/').get_data(as_text=True)
    assert 'Story gelesen' not in http.get(f'/story/{story_a}').get_data(as_text=True)
    assert http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN).status_code == 503


def test_story_read_for_a_story_that_is_gone(config, store):
    story_setup(store)
    client = FakeClient([])
    http = create_app(config, store, client).test_client()
    response = http.post('/story/000000000000/gelesen', headers=SAME_ORIGIN,
                         data={'next': '/?alle=1'})
    assert response.status_code == 303 and response.headers['Location'] == '/?alle=1'
    assert client.marked == []


def test_read_story_reappears_with_new_articles(config, store):
    entries, story_a, _ = story_setup(store)
    http = create_app(config, store, FakeClient([])).test_client()
    http.post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN)
    for e in entries:
        if e['id'] in (1, 4):
            e['status'] = 'read'  # as Miniflux now returns them
    store.save_run(entries + [entry(8, 6, hours_ago=0.5)],
                   [ClusterResult([1, 2, 3, 4, 5, 8], 1, {3}), ClusterResult([6, 7], 6, set())])

    html = http.get('/').get_data(as_text=True)
    assert listed(html) == 2 and '<span class="new">1 neu</span>' in html
    assert f'action="/story/{story_a}/gelesen"><button>Story gelesen (1)</button>' in html
    # A story the user never marked has nothing 'neu'
    assert html.count(' neu</span>') == 1


def test_a_fetch_from_before_story_read_does_not_undo_it(config, store):
    # A clustering run fetched the story (all unread), the user marked it
    # read, then the run saved what it had fetched before
    entries, story_a, _ = story_setup(store)
    fetched_before = datetime.now(timezone.utc) - timedelta(minutes=2)
    create_app(config, store, FakeClient([])).test_client() \
        .post(f'/story/{story_a}/gelesen', headers=SAME_ORIGIN)
    store.save_run(entries, [ClusterResult([1, 2, 3, 4, 5], 1, {3}),
                             ClusterResult([6, 7], 6, set())],
                   FetchResult(entries, fetched_before - timedelta(hours=24),
                               fetched_at=fetched_before))
    assert store.get_story(story_a, 24)['unread_count'] == 0

    # A later fetch is Miniflux's word: the user set both back to unread there
    fetched_after = datetime.now(timezone.utc) + timedelta(seconds=2)
    store.save_run(entries, [ClusterResult([1, 2, 3, 4, 5], 1, {3}),
                             ClusterResult([6, 7], 6, set())],
                   FetchResult(entries, fetched_after - timedelta(hours=24),
                               fetched_at=fetched_after))
    assert store.get_story(story_a, 24)['unread_count'] == 2


def test_user_marks_are_no_duplicates_for_clustering(config, store):
    # Marked read by the user, the budget articles stay candidates for the
    # headline; only the duplicate aRSSe marked itself ranks last
    client = FakeClient(sample_entries())
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    budget = next(s for s in store.top_stories(24, 50) if 1 in {a['id'] for a in s['articles']})
    headline = budget['headline']['id']
    http = create_app(config, store, client).test_client()
    http.post(f"/story/{budget['id']}/gelesen", headers=SAME_ORIGIN)
    assert store.auto_marked_ids() == {2}

    stats = clusterer.run_clustering_cycle()
    assert stats['errors'] == 0 and stats['marked_read'] == 0
    again = store.get_story(budget['id'], 24)
    assert again['headline']['id'] == headline and again['unread_count'] == 0
    assert store.auto_marked_ids() == {2}


# --- Status sync ----------------------------------------------------------------

def synced_setup(config, store):
    client = FakeClient(sample_entries())
    NewsClusterer(config, store, client=client).run_clustering_cycle()
    client.calls.clear()
    return client, StatusSync(config, store, client)


def test_status_sync_takes_over_reads_from_miniflux(config, store):
    client, sync = synced_setup(config, store)
    chip = next(s for s in store.top_stories(24, 50) if 4 in {a['id'] for a in s['articles']})
    client.update_entries([4, 5], 'read')  # read in Miniflux

    assert sync.run() == 2
    call = client.calls[-1]
    assert call['status'] == 'read' and call['order'] == 'id'
    assert call['changed_after'] is not None and call['published_after'] is not None
    assert store.get_story(chip['id'], 24)['unread_count'] == 0
    # Not the user's marks from 'Story gelesen': nothing is 'neu' later on
    assert store.get_story(chip['id'], 24)['new_count'] == 0

    # Nothing changed since: asks from this sync on, updates nothing
    before = client.calls[-1]['changed_after']
    assert sync.run() == 0
    assert client.calls[-1]['changed_after'] >= before


def test_status_sync_waits_for_the_first_clustering_run(config, store):
    client = FakeClient(sample_entries())
    assert StatusSync(config, store, client).run() == 0
    assert client.calls == []


def test_status_sync_starts_over_after_a_clustering_run(config, store):
    # A run fetched before the last sync but saved after it may have stored
    # stale statuses: the next sync asks again from that run's fetch time
    client, sync = synced_setup(config, store)
    sync.run()
    synced = client.calls[-1]['changed_after']
    fetched = datetime.now(timezone.utc) - timedelta(minutes=20)
    store.set_meta('last_fetch_at', fetched.isoformat())
    sync.run()
    expected = int((fetched - news_clustering.STATUS_SYNC_OVERLAP).timestamp())
    assert client.calls[-1]['changed_after'] == expected < synced
    sync.run()
    assert client.calls[-1]['changed_after'] > expected


def test_status_sync_pages_and_stops(config, store):
    config.scheduling.batch_size = 2
    client, sync = synced_setup(config, store)
    client.update_entries([1, 3, 4, 5], 'read')
    assert sync.run() == 4
    # 2 is the duplicate the clustering run marked read
    assert [c['after_entry_id'] for c in client.calls] == [None, 2, 4]

    # A server that ignores after_entry_id repeats the first page: one more
    # request, not an endless loop
    client.calls.clear()
    real = client.get_entries
    client.get_entries = lambda **kw: real(**{**kw, 'after_entry_id': None})
    sync.run()
    assert len(client.calls) == 2


class StopAfter(threading.Event):
    """Event that records waits and stops the loop after n of them."""

    def __init__(self, n):
        super().__init__()
        self.n = n
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) >= self.n:
            self.set()
        return self.is_set()


def test_status_sync_loop_survives_errors_and_backs_off(config, store, caplog):
    client, _ = synced_setup(config, store)
    client.error = requests.ConnectionError('miniflux down')
    attempts = []

    def make_client():
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError('no client yet')
        return client

    stop = StopAfter(8)
    real_wait = stop.wait

    def wait(timeout=None):
        if len(stop.waits) == 6:
            client.error = None  # Miniflux is back
        return real_wait(timeout)
    stop.wait = wait

    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        run_status_sync(config, store, stop, make_client)
    interval = config.scheduling.status_sync_minutes * 60
    assert stop.waits == [interval, 2 * interval, 4 * interval, 8 * interval, 8 * interval,
                          8 * interval, 8 * interval, interval]
    assert len(attempts) == 2
    assert sum('Status sync with Miniflux failed' in r.getMessage()
               for r in caplog.records) == 6


def test_status_sync_setting(config):
    from config import ConfigError, _validate
    config.scheduling.status_sync_minutes = -1
    with pytest.raises(ConfigError, match='status_sync_minutes'):
        _validate(config)

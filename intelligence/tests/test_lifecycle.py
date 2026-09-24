"""
Story lifecycle: which cluster keeps a story ID, which articles count for
a story, and when articles and stories leave the database.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import store as store_module
from conftest import FakeClient, sample_entries
from news_clustering import NewsClusterer
from store import ClusterResult, FetchResult, StoryStore
from web import create_app


def entry(entry_id, feed_id, hours_ago=1.0, now=None):
    """A minimal Miniflux entry; feed_id doubles as the feed title."""
    now = now or datetime.now(timezone.utc)
    return {
        'id': entry_id,
        'feed_id': feed_id,
        'feed': {'id': feed_id, 'title': f'Feed {feed_id}'},
        'title': f'Artikel {entry_id}',
        'url': f'https://example.org/{entry_id}',
        'published_at': (now - timedelta(hours=hours_ago)).isoformat(),
        'status': 'unread',
    }


def cluster(ids, headline=None):
    return ClusterResult(list(ids), ids[0] if headline is None else headline, set())


def fetch_of(entries, lookback_hours=24, complete=True, now=None):
    now = now or datetime.now(timezone.utc)
    return FetchResult(entries, now - timedelta(hours=lookback_hours), complete=complete,
                       min_id=min((e['id'] for e in entries), default=None),
                       fetched_at=now)


def members(st, story_id, hours=24):
    story = st.get_story(story_id, hours)
    return sorted(a['id'] for a in story['articles']) if story else None


def stored_ids(st, table='entries', column='id'):
    conn = sqlite3.connect(st.db_path)
    try:
        return sorted(row[0] for row in conn.execute(f"SELECT {column} FROM {table}"))
    finally:
        conn.close()


# --- Stable IDs ---------------------------------------------------------------

@pytest.mark.parametrize('order', ['continuation_first', 'side_topic_first'])
def test_continuation_keeps_id_over_larger_side_topic(store, order):
    # r02: S={1..5}; next run 1-4 stay together, 5 joins a larger new topic
    run1 = [entry(i, i) for i in range(1, 6)]
    story_id = store.save_run(run1, [cluster([1, 2, 3, 4, 5])])[0]

    run2 = run1 + [entry(i, i % 13 + 1) for i in range(10, 19)]
    continuation = cluster([1, 2, 3, 4])
    side_topic = cluster([5] + list(range(10, 19)), headline=10)
    clusters = ([continuation, side_topic] if order == 'continuation_first'
                else [side_topic, continuation])
    assigned = store.save_run(run2, clusters)

    assert assigned[clusters.index(continuation)] == story_id
    assert assigned[clusters.index(side_topic)] != story_id
    assert members(store, story_id) == [1, 2, 3, 4]


@pytest.mark.parametrize('headline_first', [True, False])
def test_story_id_follows_the_headline_on_an_even_split(store, headline_first):
    run1 = [entry(i, i) for i in range(1, 7)]
    story_id = store.save_run(run1, [cluster([1, 2, 3, 4, 5, 6], headline=1)])[0]

    halves = [cluster([1, 2, 3]), cluster([4, 5, 6])]
    if not headline_first:
        halves.reverse()
    assigned = store.save_run(run1, halves)

    with_headline = 0 if headline_first else 1
    assert assigned[with_headline] == story_id
    assert assigned[1 - with_headline] != story_id
    assert store.get_story(story_id, 24)['headline']['id'] == 1


def test_more_shared_articles_beat_the_headline(store):
    run1 = [entry(i, i) for i in range(1, 7)]
    story_id = store.save_run(run1, [cluster([1, 2, 3, 4, 5, 6], headline=1)])[0]

    assigned = store.save_run(run1, [cluster([1, 2]), cluster([3, 4, 5, 6])])
    assert assigned[1] == story_id


def test_hidden_single_feed_series_never_takes_a_story_id(store):
    # The verifier's synthetic shape: a real 2-feed event merges for one
    # run with a larger series of a single feed, then they split again
    event = [entry(1, 1), entry(2, 2)]
    series = [entry(i, 3) for i in (10, 11, 12, 13)]
    shown = []

    def run(entries, clusters):
        assigned = store.save_run(entries, clusters, fetch_of(entries), min_sources=2)
        shown.append({s['id'] for s in store.top_stories(24, 50, min_sources=2)})
        return assigned

    first = run(event + series, [cluster([1, 2]), cluster([10, 11, 12, 13])])
    event_ids = [first[0]]
    merged = run(event + series, [cluster([1, 2, 10, 11, 12, 13], headline=1)])
    event_ids.append(merged[0])
    grown = event + series + [entry(3, 4)]
    last = run(grown, [cluster([10, 11, 12, 13]), cluster([1, 2, 3])])
    event_ids.append(last[1])

    assert len(set(event_ids)) == 1
    assert last[0] != event_ids[0]
    story = store.get_story(event_ids[0], 24)
    assert sorted(a['id'] for a in story['articles']) == [1, 2, 3]
    assert story['source_count'] == 3
    assert all(event_ids[0] in ids for ids in shown)


def test_merged_stories_keep_one_id(store):
    run1 = [entry(i, i % 3 + 1) for i in range(1, 5)]
    first = store.save_run(run1, [cluster([1, 2]), cluster([3, 4])])
    assert len(set(first.values())) == 2

    run2 = run1 + [entry(i, i % 3 + 1) for i in (7, 8)]
    second = store.save_run(run2, [cluster([7, 8]), cluster([1, 2, 3, 4])])
    # One cluster continues both stories: it takes one ID, the other ends
    assert second[1] in first.values()
    assert second[0] not in first.values()
    gone = (set(first.values()) - {second[1]}).pop()
    assert store.get_story(gone, 24) is None


# --- Articles deleted in Miniflux ---------------------------------------------

def test_flushed_entries_leave_their_story(config, store):
    # r13: the user reads the headline, aRSSe marked the duplicate read,
    # and 'Flush history' in Miniflux deletes both
    client = FakeClient(sample_entries())
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    budget = next(s for s in store.top_stories(24, 50)
                  if {a['id'] for a in s['articles']} == {1, 2, 3})
    assert {e['id'] for e in client.entries if e['status'] == 'read'} & {1, 2}

    client.entries = [e for e in client.entries if e['id'] not in (1, 2)]
    assert clusterer.run_clustering_cycle()['errors'] == 0

    assert store.get_story(budget['id'], 24) is None
    for story in store.top_stories(24, 50):
        assert not {1, 2} & {a['id'] for a in story['articles']}
    assert not {1, 2} & set(stored_ids(store))
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert '/entry/1"' not in html and '/entry/2"' not in html
    assert 'Prozessor' in html


def test_truncated_fetch_only_prunes_the_fetched_id_range(store):
    run1 = [entry(i, i) for i in range(1, 8)]
    first = store.save_run(run1, [cluster([1, 2, 3]), cluster([4, 5, 6, 7])],
                           fetch_of(run1))

    # max_entries hit: only the newest IDs 5 and 6 came back. 7 was deleted
    # in Miniflux; 1-4 lie below the fetched range and may still exist.
    run2 = [e for e in run1 if e['id'] in (5, 6)]
    store.save_run(run2, [cluster([5, 6])], fetch_of(run2, complete=False))

    assert stored_ids(store, 'story_entries', 'entry_id') == [1, 2, 3, 4, 5, 6]
    assert 7 not in stored_ids(store)
    assert members(store, first[0]) == [1, 2, 3]

    # A complete fetch proves 1-3 are gone
    store.save_run(run2, [cluster([5, 6])], fetch_of(run2))
    assert stored_ids(store, 'story_entries', 'entry_id') == [5, 6]
    assert store.get_story(first[0], 24) is None


def test_entries_near_the_cutoff_or_without_fetch_are_not_pruned(store):
    now = datetime.now(timezone.utc)
    # 23h59m30s old: inside the window but within the margin at its edge
    run1 = [entry(1, 1, hours_ago=24 - 1 / 120, now=now), entry(2, 2, now=now),
            entry(3, 3, now=now)]
    store.save_run(run1, [cluster([1, 2, 3])], fetch_of(run1, now=now))

    run2 = [e for e in run1 if e['id'] == 3]
    store.save_run(run2, [], fetch_of(run2, now=now))
    assert stored_ids(store) == [1, 3]

    # Without a FetchResult nothing is known about deletions
    store.save_run([], [])
    assert stored_ids(store) == [1, 3]


def test_empty_truncated_fetch_prunes_nothing(store):
    run1 = [entry(1, 1), entry(2, 2)]
    store.save_run(run1, [cluster([1, 2])], fetch_of(run1))
    store.save_run([], [], fetch_of([], complete=False))
    assert stored_ids(store) == [1, 2]


# --- Window-scoped ranking ----------------------------------------------------

def test_out_of_window_articles_do_not_count(config, store):
    entries = [
        # Story A: two feeds, but only one of them inside the 24h window
        entry(1, 1, hours_ago=30), entry(2, 2, hours_ago=31), entry(3, 3, hours_ago=1),
        entry(4, 4, hours_ago=2),
        # Story B: no article inside the window at all
        entry(5, 1, hours_ago=26), entry(6, 2, hours_ago=27),
        # Story C: two feeds inside the window
        entry(7, 1, hours_ago=5), entry(8, 2, hours_ago=6),
    ]
    assigned = store.save_run(entries, [cluster([1, 2, 3, 4], headline=1),
                                        cluster([5, 6]), cluster([7, 8])])
    story_a, story_b, story_c = assigned[0], assigned[1], assigned[2]

    shown = {s['id']: s for s in store.top_stories(24, 50, min_sources=1)}
    assert story_b not in shown
    assert store.get_story(story_b, 24) is None

    a = shown[story_a]
    assert [x['id'] for x in a['articles']] == [3, 4]
    assert (a['article_count'], a['source_count']) == (2, 2)
    # The stored headline left the window: the newest article stands in
    assert a['headline']['id'] == 3
    assert [x['id'] for x in a['earlier_articles']] == [1, 2]
    assert a['earlier_count'] == 2

    # Newer articles with the same in-window coverage rank higher
    ranked = [s['id'] for s in store.top_stories(24, 50, min_sources=2)]
    assert ranked == [story_a, story_c]
    entries.append(entry(9, 5, hours_ago=30))
    store.save_run(entries, [cluster([1, 2, 3, 4, 9], headline=1),
                             cluster([5, 6]), cluster([7, 8])])
    assert [s['id'] for s in store.top_stories(24, 50, min_sources=3)] == []

    # A story that reaches min_sources only through older articles is hidden
    entries = [entry(10, 1, hours_ago=30), entry(11, 2, hours_ago=1),
               entry(12, 2, hours_ago=2)]
    only_old = store.save_run(entries, [cluster([10, 11, 12], headline=11)])[0]
    assert only_old not in {s['id'] for s in store.top_stories(24, 50, min_sources=2)}
    assert only_old in {s['id'] for s in store.top_stories(24, 50, min_sources=1)}


def test_story_page_lists_earlier_articles_capped(config, store):
    entries = [entry(1, 1, hours_ago=1), entry(2, 2, hours_ago=2)]
    entries += [entry(i, i % 3 + 1, hours_ago=24 + i) for i in range(10, 16)]
    story_id = store.save_run(entries, [cluster([e['id'] for e in entries])])[0]

    story = store.get_story(story_id, 24, earlier_max=4)
    assert [a['id'] for a in story['earlier_articles']] == [10, 11, 12, 13]
    assert story['earlier_count'] == 6
    assert store.get_story(story_id, 24, earlier_max=0)['earlier_articles'] == []

    config.web.earlier_articles_max = 4
    html = create_app(config, store).test_client().get(f'/story/{story_id}') \
        .get_data(as_text=True)
    assert '2 Quellen, 2 Artikel' in html
    assert 'Frühere Berichte' in html
    assert '/entry/13"' in html and '/entry/14"' not in html
    assert 'und 2 ältere' in html


# --- Retention ----------------------------------------------------------------

class SimClock:
    now = None


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return SimClock.now if tz else SimClock.now.replace(tzinfo=None)


def test_long_running_story_keeps_its_id_but_not_its_old_articles(config, monkeypatch):
    # r10: 2 articles every 12 h for 20 days, 24 h window, retention 7 days
    monkeypatch.setattr(store_module, 'datetime', FakeDatetime)
    st = StoryStore(config.storage.db_path)
    start = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
    published = []
    story_ids = set()
    for k in range(40):
        SimClock.now = start + timedelta(hours=12 * k)
        feeds = (2 * k % 13 + 1, (2 * k + 1) % 13 + 1)
        published += [entry(2 * k + 1, feeds[0], hours_ago=0.1, now=SimClock.now),
                      entry(2 * k + 2, feeds[1], hours_ago=0.1, now=SimClock.now)]
        cutoff = SimClock.now - timedelta(hours=24)
        window = [e for e in published
                  if datetime.fromisoformat(e['published_at']) > cutoff]
        assigned = st.save_run(window, [cluster([e['id'] for e in window])],
                               fetch_of(window, now=SimClock.now), min_sources=2)
        st.cleanup(7)
        story_ids.add(assigned[0])

    assert len(story_ids) == 1
    story_id = story_ids.pop()
    story = st.get_story(story_id, 24, earlier_max=1000)
    window_feeds = {e['feed_id'] for e in window}
    assert story['source_count'] == len(window_feeds) == 4
    assert story['article_count'] == len(window) == 4
    assert st.top_stories(24, 50, min_sources=2)[0]['source_count'] == 4

    oldest = SimClock.now - timedelta(days=7)
    kept = story['articles'] + story['earlier_articles']
    assert kept
    assert all(datetime.fromisoformat(a['published_at']) >= oldest for a in kept)
    conn = sqlite3.connect(st.db_path)
    try:
        dates = [row[0] for row in conn.execute("SELECT published_at FROM entries")]
    finally:
        conn.close()
    assert len(dates) == len(kept) < len(published)
    assert all(datetime.fromisoformat(d) >= oldest for d in dates)

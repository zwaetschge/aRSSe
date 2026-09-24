import miniflux

from conftest import BUDGET, FakeClient, make_entry, sample_entries
from news_clustering import NewsClusterer


def run(config, store, entries):
    client = FakeClient(entries)
    clusterer = NewsClusterer(config, store, client=client)
    return clusterer, client, clusterer.run_clustering_cycle()


def story_members(store):
    return sorted(sorted(a['id'] for a in s['articles'])
                  for s in store.top_stories(24, 50))


def test_clusters_related_articles(config, store):
    _, _, stats = run(config, store, sample_entries())

    assert stats['errors'] == 0
    assert stats['clusters_found'] == 2
    assert story_members(store) == [[1, 2, 3], [4, 5]]


def test_marks_only_duplicates_read_via_bulk_api(config, store):
    _, client, stats = run(config, store, sample_entries())

    assert stats['duplicates_detected'] == 1
    assert stats['marked_read'] == 1
    assert len(client.marked) == 1
    ids, status = client.marked[0]
    assert status == 'read' and len(ids) == 1 and ids[0] in (1, 2)


def test_duplicate_action_none_leaves_miniflux_untouched(config, store):
    config.deduplication.duplicate_action = 'none'
    _, client, stats = run(config, store, sample_entries())

    assert stats['duplicates_detected'] == 1
    assert client.marked == []


def test_story_ids_are_stable_across_runs(config, store):
    entries = sample_entries()
    clusterer, client, _ = run(config, store, entries)
    before = {s['headline']['title']: s['id'] for s in store.top_stories(24, 50)}

    # A new article joins the budget story
    client.entries.append(make_entry(7, 4, 'Bundestag: Haushalt beschlossen',
                                     'Der Bundestag beschließt den Haushalt, '
                                     'die Opposition kritisiert die Schuldenbremse.'))
    clusterer.run_clustering_cycle()
    after = store.top_stories(24, 50)

    budget = next(s for s in after if 7 in {a['id'] for a in s['articles']})
    assert budget['id'] in before.values()
    assert len({s['id'] for s in after}) == len(after)


def test_fetch_paginates_and_filters_server_side(config, store):
    config.scheduling.batch_size = 2
    clusterer, client, _ = run(config, store, sample_entries())

    assert [c['offset'] for c in client.calls] == [0, 2, 4]
    assert all(c['published_after'] for c in client.calls)
    assert all(c['status'] == ['unread', 'read'] for c in client.calls)


def test_fetch_respects_max_entries(config, store):
    config.scheduling.batch_size = 2
    config.scheduling.max_entries = 3
    clusterer, client, stats = run(config, store, sample_entries())

    assert stats['articles_processed'] == 3


def test_api_failure_is_reported_not_raised(config, store):
    class BrokenClient(FakeClient):
        def get_entries(self, **kwargs):
            raise miniflux.ClientError(type('R', (), {'status_code': 500,
                                                     'json': lambda self: {}})())

    clusterer = NewsClusterer(config, store, client=BrokenClient([]))
    stats = clusterer.run_clustering_cycle()

    assert stats['errors'] == 1
    assert store.get_meta('last_success') is None


def test_stemming_merges_inflections(config, store):
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert clusterer._tokenize('bundeskanzlers') == clusterer._tokenize('bundeskanzler')
    assert clusterer._tokenize('und der die') == []


def test_canonical_strategies(config, store):
    entries = sample_entries()[:3]
    clusterer = NewsClusterer(config, store, client=FakeClient([]))

    config.deduplication.canonical_strategy = 'longest'
    assert clusterer._select_canonical(entries, [0, 2]) == 0

    config.deduplication.canonical_strategy = 'source_priority'
    config.deduplication.source_scores = {'zeit.de': 95, 'tagesschau.de': 85}
    assert clusterer._select_canonical(entries, [0, 1, 2]) == 2

    entries[1]['published_at'] = '2099-01-01T00:00:00Z'
    config.deduplication.canonical_strategy = 'newest'
    assert clusterer._select_canonical(entries, [0, 1, 2]) == 1


def test_untitled_entry_never_becomes_headline(config, store):
    entries = sample_entries()
    # Longest content, but no title: 'longest' strategy must still skip it
    entries.append(make_entry(9, 3, '', BUDGET * 3))
    run(config, store, entries)

    budget = next(s for s in store.top_stories(24, 50)
                  if 9 in {a['id'] for a in s['articles']})
    assert budget['headline']['title']


def test_headline_is_never_a_duplicate(config, store):
    config.deduplication.threshold = 0.85  # production default
    entries = sample_entries()
    # Untitled near-copy of the budget article with the longest content
    entries.append(make_entry(9, 3, '', BUDGET + ' ' + BUDGET[:40]))
    _, client, _ = run(config, store, entries)

    budget = next(s for s in store.top_stories(24, 50)
                  if 9 in {a['id'] for a in s['articles']})
    headline = budget['headline']
    marked = {i for ids, _ in client.marked for i in ids}
    assert headline['title']
    assert not headline['is_duplicate']
    assert headline['id'] not in marked

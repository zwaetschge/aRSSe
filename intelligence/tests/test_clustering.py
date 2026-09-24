import itertools
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import miniflux
import pytest

from conftest import BUDGET, FakeClient, make_entry, sample_entries
from news_clustering import NewsClusterer, _norm_url


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

    # Keyset paging: newest IDs first, then below the smallest ID seen
    assert [c['before_entry_id'] for c in client.calls] == [None, 5, 3]
    assert all(c['order'] == 'id' and c['direction'] == 'desc' for c in client.calls)
    assert all(c['offset'] == 0 for c in client.calls)
    assert all(c['published_after'] for c in client.calls)
    assert all(c['status'] == ['unread', 'read'] for c in client.calls)


def test_fetch_result_describes_the_fetch(config, store):
    config.scheduling.batch_size = 4
    clusterer = NewsClusterer(config, store, client=FakeClient(sample_entries()))
    before = datetime.now(timezone.utc)
    fetch = clusterer._fetch_recent_entries()

    assert sorted(e['id'] for e in fetch.entries) == [1, 2, 3, 4, 5, 6]
    assert fetch.complete and fetch.min_id == 1
    assert before <= fetch.fetched_at <= datetime.now(timezone.utc)
    assert fetch.fetched_at - fetch.cutoff == timedelta(hours=24)


def test_entry_stored_while_paging_is_fetched_once(config, store):
    # Offset paging fetched [1,2,3,3,4,5,6] when an entry arrived between
    # two pages, and save_run then failed with a UNIQUE violation
    config.scheduling.batch_size = 2
    client = FakeClient(sample_entries())

    def new_entry_arrives(page):
        if page == 1:
            client.entries.append(make_entry(7, 4, 'Bundestag: Haushalt beschlossen',
                                             'Der Bundestag beschließt den Haushalt.'))
    client.after_page = new_entry_arrives
    saved = []
    save_run = store.save_run
    store.save_run = lambda entries, *args: saved.append(
        [e['id'] for e in entries]) or save_run(entries, *args)
    stats = NewsClusterer(config, store, client=client).run_clustering_cycle()

    assert stats['errors'] == 0
    # Every ID exactly once; entry 7 follows in the next run
    assert sorted(saved[0]) == [1, 2, 3, 4, 5, 6]
    assert story_members(store) == [[1, 2, 3], [4, 5]]


def test_entry_repeated_on_two_pages_does_not_break_the_run(config, store):
    class RepeatingClient(FakeClient):
        """Returns the last entry of every page again on the next page."""

        def get_entries(self, **kwargs):
            page = super().get_entries(**kwargs)
            if kwargs.get('before_entry_id') and page['entries']:
                previous = next(e for e in self.entries
                                if e['id'] == kwargs['before_entry_id'])
                page['entries'].insert(0, dict(previous))
            return page

    config.scheduling.batch_size = 2
    client = RepeatingClient(sample_entries())
    clusterer = NewsClusterer(config, store, client=client)
    fetch = clusterer._fetch_recent_entries()
    assert sorted(e['id'] for e in fetch.entries) == [1, 2, 3, 4, 5, 6]

    stats = clusterer.run_clustering_cycle()
    assert stats['errors'] == 0
    assert story_members(store) == [[1, 2, 3], [4, 5]]


def test_tied_publication_dates_do_not_repeat_entries(config, store):
    # Many entries share one published_at (whole minutes, feeds without
    # dates). Postgres returns ties in any order, and the order changed with
    # LIMIT/OFFSET, so offset pages overlapped. Paging by ID is immune.
    class PostgresLikeClient(FakeClient):
        def get_entries(self, **kwargs):
            if len(self.calls) % 2:
                self.entries.reverse()  # another plan, another tie order
            return super().get_entries(**kwargs)

    config.scheduling.batch_size = 3
    entries = sample_entries()
    for e in entries:
        e['published_at'] = entries[0]['published_at']
    client = PostgresLikeClient(entries)
    stats = NewsClusterer(config, store, client=client).run_clustering_cycle()

    assert stats['errors'] == 0
    assert stats['articles_processed'] == 6
    assert story_members(store) == [[1, 2, 3], [4, 5]]


def test_server_ignoring_before_entry_id_does_not_loop(config, store):
    class OldServer(FakeClient):
        def get_entries(self, *, before_entry_id=None, **kwargs):
            return super().get_entries(**kwargs)

    config.scheduling.batch_size = 2
    client = OldServer(sample_entries())
    fetch = NewsClusterer(config, store, client=client)._fetch_recent_entries()
    assert len(fetch.entries) == 2
    assert len(client.calls) == 2
    # Older entries of the window were never fetched: pruning must not trust it
    assert not fetch.complete and fetch.min_id == 5


def test_fetch_respects_max_entries(config, store):
    config.scheduling.batch_size = 2
    config.scheduling.max_entries = 3
    clusterer, client, stats = run(config, store, sample_entries())

    assert stats['articles_processed'] == 3
    assert [c['limit'] for c in client.calls] == [2, 1]
    fetch = clusterer._fetch_recent_entries()
    # Keyset paging keeps the most recently stored entries
    assert sorted(e['id'] for e in fetch.entries) == [4, 5, 6]
    assert not fetch.complete and fetch.min_id == 4


def test_huge_feed_item_is_cut_before_parsing(config, store):
    # A 15 MB item (Miniflux's default body limit) took 5.9 s and ~600 MB RSS
    code = textwrap.dedent('''
        import resource, sys, time
        sys.path.insert(0, sys.argv[1])
        from config import Config
        from news_clustering import NewsClusterer
        clusterer = NewsClusterer(Config(), store=None, client=object())
        # Plain markup, and markup full of short data: runs the URI filter scans
        for content in ['<p>' + 'Wort und <b>Satz</b> ' * 750_000 + '</p>',
                        '<p>' + 'metadata:data: ' * 1_000_000 + '</p>']:
            entry = {'title': 'Titel ' * 1_000_000, 'content': content}
            baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            start = time.perf_counter()
            text = clusterer._preprocess_entry(entry)
            elapsed = time.perf_counter() - start
            growth_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - baseline) / 1024
            print(elapsed, growth_mb, len(text))
    ''')
    source = str(Path(__file__).resolve().parent.parent)
    result = subprocess.run([sys.executable, '-c', code, source],
                            capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        elapsed, growth_mb, length = line.split()
        assert float(elapsed) < 1.0
        assert float(growth_mb) < 100
        assert int(length) < 20_000


def test_inline_image_does_not_push_text_past_the_html_cut(config):
    # Miniflux keeps data:image URIs; a 240 KB one used up the whole HTML
    # budget and the article was clustered on its title alone
    image = '<p><img src="data:image/jpeg;base64,' + 'QUJD' * 60_000 + '"></p>'
    style = '<div style="background:url(DATA:image/png;base64,' + 'A' * 300_000 + ')"></div>'
    body = '<p>Der Bundestag hat den Bundeshaushalt beschlossen.</p>'
    clusterer = NewsClusterer(config, store=None, client=object())
    for content in [image + body, style + body]:
        entry = {'title': 'Haushalt', 'content': content}
        text = clusterer._preprocess_entry(entry)
        assert text == 'haushalt haushalt der bundestag hat den bundeshaushalt beschlossen'
        assert entry['_snippet'] == 'Der Bundestag hat den Bundeshaushalt beschlossen.'


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


# Two different broadcasts from the real Tagesschau feed (corpus ids 36/37):
# same title, a body of '[ mehr ]', different URLs
def broadcast(entry_id, path, hours_ago):
    url = f'https://www.tagesschau.de/{path}.html'
    entry = make_entry(entry_id, 1, 'tagesschau', '', hours_ago=hours_ago)
    entry['url'] = url
    entry['content'] = f'<p> <a href="{url}"></a> <br /> <br />[<a href="{url}">mehr</a>]</p>'
    return entry


def clusters_by_member(clusters):
    return {eid: c for c in clusters for eid in c.entry_ids}


def test_same_titled_broadcasts_are_not_duplicates(config, store):
    config.deduplication.threshold = 0.85
    config.deduplication.mark_read_scope = 'all'
    entries = sample_entries() + [broadcast(36, 'tagesschau_20_uhr/ts-81144', 1),
                                  broadcast(37, 'tagesschau/ts-81146', 2)]
    clusterer, client, _ = run(config, store, entries)

    cluster = clusters_by_member(clusterer._cluster([dict(e) for e in entries]))[36]
    assert 37 in cluster.entry_ids
    assert not cluster.duplicate_ids
    assert not {36, 37} & {i for ids, _ in client.marked for i in ids}


def listed_twice(first_id, second_id, url_variant):
    """An article a feed lists twice; Miniflux stores both copies."""
    title = 'Nach Richterspruch - Weißes Haus lässt CNN-Reporter wieder zu'
    text = ('Das Weiße Haus muss dem CNN-Reporter seinen Presseausweis zurückgeben. '
            'Ein Bundesrichter gab einem Eilantrag des Senders statt.')
    first = make_entry(first_id, 1, title, text)
    second = make_entry(second_id, 1, title, text)
    first['published_at'] = second['published_at']
    first['url'] = 'https://www.tagesschau.de/ausland/amerika/usa-medien-cnn-trump-102.html'
    second['url'] = url_variant
    return [first, second]


def test_item_listed_twice_keeps_the_first_stored_copy_in_either_order(config, store):
    copies = listed_twice(10, 42, 'HTTPS://WWW.Tagesschau.de/ausland/amerika/'
                                  'usa-medien-cnn-trump-102.html/?utm_source=rss#top')
    for entries in (sample_entries() + copies, (sample_entries() + copies)[::-1]):
        clusterer = NewsClusterer(config, store, client=FakeClient([]))
        cluster = clusters_by_member(clusterer._cluster([dict(e) for e in entries]))[10]
        assert cluster.duplicate_ids == cluster.copy_ids == {42}
        assert cluster.headline_entry_id == 10


def test_item_listed_twice_is_marked_read_in_a_single_feed_story(config, store):
    # A single-feed story is not on the front page, but an identical copy
    # never needs to be read twice
    _, client, stats = run(config, store, sample_entries() + listed_twice(
        10, 42, 'https://www.tagesschau.de/ausland/amerika/usa-medien-cnn-trump-102.html'))

    copies = next(s for s in store.top_stories(24, 50)
                  if 42 in {a['id'] for a in s['articles']})
    assert copies['source_count'] == 1
    # 2 is the budget duplicate
    assert client.marked == [([2, 42], 'read')]
    assert stats['marked_read'] == 2


def test_same_url_with_other_title_is_not_a_duplicate(config, store):
    # Items without a link get the site URL in Miniflux: same URL, other item
    entries = listed_twice(10, 42, 'https://www.tagesschau.de/ausland/amerika/'
                                   'usa-medien-cnn-trump-102.html')
    entries[1]['title'] = 'CNN-Reporter darf zurück ins Weiße Haus'
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert clusterer._detect_duplicates(entries) == (set(), set())


def test_user_unread_duplicate_is_not_marked_again(config, store):
    # r06: the user sets the duplicate back to unread after every cycle
    client = FakeClient(sample_entries())
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    assert len(client.marked) == 1
    marked = client.marked[0][0]

    for _ in range(3):
        for e in client.entries:
            if e['id'] in marked:
                e['status'] = 'unread'
        stats = clusterer.run_clustering_cycle()
        assert stats['errors'] == 0
        assert stats['duplicates_detected'] == 1
        assert stats['marked_read'] == 0
    assert len(client.marked) == 1


def agency_copies():
    """
    r05b: an agency text (A) that two outlets extend in different ways.

    Body similarity (title excluded): A-B .83, A-C .87, B-C .72.
    B is the longest and the canonical version.
    """
    title = 'Bundestag beschließt Haushalt'
    return [
        make_entry(1, 1, title, BUDGET),
        make_entry(2, 2, title, BUDGET + ' Zusätzlich: Die Länder fordern mehr Geld für '
                                          'Kitas, Schulen und Hochschulen im kommenden Jahr.'),
        make_entry(3, 3, title, BUDGET + ' Exklusiv: Verteidigungsministerium erhält '
                                          'Sondervermögen für Marine und Luftwaffe.'),
    ]


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_duplicates_are_compared_with_their_canonical(config, store, order):
    config.deduplication.threshold = 0.8
    triple = agency_copies()
    entries = [triple[i] for i in order] + sample_entries()[3:]
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    cluster = clusters_by_member(clusterer._cluster([dict(e) for e in entries]))[2]

    assert {1, 2, 3} <= set(cluster.entry_ids)
    assert cluster.headline_entry_id == 2
    # C is too far from B; its only near-duplicate A is not the one kept
    assert cluster.duplicate_ids == {1}


def test_title_alone_does_not_make_a_duplicate(config, store):
    # Short teasers: the equal title used to decide on its own
    teaser = 'Mehr dazu in Kürze auf unserer Seite.'
    entries = [make_entry(1, 1, 'Bundestag beschließt Haushalt', teaser),
               make_entry(2, 2, 'Bundestag beschließt Haushalt', teaser)]
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert clusterer._detect_duplicates(entries) == (set(), set())

    config.deduplication.min_body_tokens = 5
    assert clusterer._detect_duplicates(entries) == ({2}, set())


def test_mark_read_scope(config, store):
    # The budget story has three feeds; with min_sources 4 it is not shown
    config.web.min_sources = 4
    _, client, stats = run(config, store, sample_entries())
    assert stats['duplicates_detected'] == 1
    assert client.marked == []

    config.deduplication.mark_read_scope = 'all'
    _, client, _ = run(config, store, sample_entries())
    assert client.marked == [([2], 'read')]


def test_canonical_tie_goes_to_the_first_stored_copy(config, store):
    entries = sample_entries()[:2]
    entries[1]['feed'] = entries[0]['feed']
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    for strategy in ('longest', 'source_priority', 'newest'):
        config.deduplication.canonical_strategy = strategy
        entries[1]['published_at'] = entries[0]['published_at']
        assert clusterer._select_canonical(entries, [0, 1]) == 0
        assert clusterer._select_canonical(entries[::-1], [0, 1]) == 1


def test_longest_measures_text_not_markup(config, store):
    plain = make_entry(1, 1, 'Haushalt', BUDGET)
    markup = make_entry(2, 2, 'Haushalt', 'Kurz.')
    markup['content'] = '<div class="teaser-image-wrapper">' * 50 + 'Kurz.' + '</div>' * 50
    assert len(markup['content']) > len(plain['content'])
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert clusterer._select_canonical([plain, markup], [0, 1]) == 0


def test_url_normalization():
    base = 'https://www.tagesschau.de/inland/haushalt-100.html'
    for variant in ('HTTPS://WWW.TAGESSCHAU.DE/inland/haushalt-100.html',
                    base + '#kommentare', base + '/',
                    base + '?utm_source=rss&utm_medium=feed', base + '?wt_mc=rss.ard'):
        assert _norm_url(variant) == _norm_url(base)
    assert _norm_url(base + '?id=2') != _norm_url(base + '?id=3')
    assert _norm_url(base + '?id=2&utm_campaign=x') == _norm_url(base + '?id=2')
    # Path case matters on most servers
    assert _norm_url(base.replace('inland', 'Inland')) != _norm_url(base)
    assert _norm_url('') == _norm_url(None) == ''

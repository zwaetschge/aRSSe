"""
E-Ink front page: pages, absolute times, coverage slots, snippets, tap
targets and the home-screen files (manifest, icons).
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import web as web_module
from config import ConfigError, load_config, resolve_timezone
from conftest import FakeClient, sample_entries
from news_clustering import NewsClusterer, clean_snippet
from store import ClusterResult, select_coverage
from web import CONTENT_SECURITY_POLICY, clock, create_app, display_title

BERLIN = ZoneInfo('Europe/Berlin')


def entry(entry_id, feed_id, hours_ago=1.0, title=None, snippet=''):
    now = datetime.now(timezone.utc)
    return {
        'id': entry_id,
        'feed_id': feed_id,
        'feed': {'id': feed_id, 'title': f'Feed {feed_id}'},
        'title': f'Artikel {entry_id}' if title is None else title,
        'url': f'https://example.org/{entry_id}',
        'published_at': (now - timedelta(hours=hours_ago)).isoformat(),
        '_snippet': snippet,
        'status': 'unread',
    }


def save_stories(store, count, feeds=2, first_id=1):
    """count stories of `feeds` articles each; story k is k minutes older."""
    entries, clusters = [], []
    for k in range(count):
        ids = [first_id + k * feeds + i for i in range(feeds)]
        entries += [entry(i, n + 1, hours_ago=1 + k / 60) for n, i in enumerate(ids)]
        clusters.append(ClusterResult(ids, ids[0], set()))
    store.save_run(entries, clusters)


def stories_on(html):
    return re.findall(r'<article class="story">\s*<h2><a [^>]*>([^<]*)</a>', html)


# --- Pages --------------------------------------------------------------------

def test_front_page_is_split_into_pages(config, store):
    config.web.page_size = 3
    save_stories(store, 7)
    http = create_app(config, store).test_client()

    seen = []
    for page in (1, 2, 3):
        html = http.get(f'/?seite={page}').get_data(as_text=True)
        seen.append(stories_on(html))
        assert f'Seite {page} von 3 · 7 Stories' in html
    assert [len(titles) for titles in seen] == [3, 3, 1]
    assert len({t for titles in seen for t in titles}) == 7
    assert http.get('/').get_data() == http.get('/?seite=1').get_data()

    first = http.get('/').get_data(as_text=True)
    assert 'href="/?seite=2"' in first and 'rel="prev"' not in first
    middle = http.get('/?seite=2').get_data(as_text=True)
    assert 'href="/"' in middle and 'href="/?seite=3"' in middle
    last = http.get('/?seite=3').get_data(as_text=True)
    assert 'rel="next"' not in last and 'href="/?seite=2"' in last


@pytest.mark.parametrize('page', ['0', '4', '999', '-1', 'x', '1.5', '', '01'])
def test_pages_out_of_range_are_not_found(config, store, page):
    config.web.page_size = 3
    save_stories(store, 7)
    assert create_app(config, store).test_client().get(f'/?seite={page}').status_code == 404


def test_max_stories_caps_all_pages(config, store):
    config.web.page_size = 2
    config.web.max_stories = 5
    save_stories(store, 7)
    http = create_app(config, store).test_client()
    assert 'Seite 1 von 3 · 5 Stories' in http.get('/').get_data(as_text=True)
    assert len(stories_on(http.get('/?seite=3').get_data(as_text=True))) == 1
    assert http.get('/?seite=4').status_code == 404


def test_empty_front_page(config, store):
    http = create_app(config, store).test_client()
    html = http.get('/').get_data(as_text=True)
    assert 'Noch keine Stories' in html and '0 Stories' in html
    assert 'Seite' not in html and 'rel="next"' not in html
    assert http.get('/?seite=2').status_code == 404


def test_top_stories_page(store):
    save_stories(store, 5)
    ranked = [s['id'] for s in store.top_stories(24, 50)]
    stories, total = store.top_stories_page(24, 50, offset=2, page_size=2)
    assert total == 5 and [s['id'] for s in stories] == ranked[2:4]
    stories, total = store.top_stories_page(24, 4, offset=2, page_size=10)
    assert total == 4 and [s['id'] for s in stories] == ranked[2:4]


def test_auto_refresh_only_on_request(config, store):
    config.web.page_size = 1
    save_stories(store, 2)
    http = create_app(config, store).test_client()
    assert 'http-equiv="refresh"' not in http.get('/').get_data(as_text=True)
    html = http.get('/?auto=1').get_data(as_text=True)
    assert '<meta http-equiv="refresh" content="1800">' in html
    # Paging keeps the always-on display refreshing
    assert 'href="/?seite=2&amp;auto=1"' in html


def test_auto_refresh_survives_opening_a_story(config, store):
    config.web.articles_per_story = 1  # a link to every story page
    save_stories(store, 12, feeds=3)
    http = create_app(config, store).test_client()
    html = http.get('/?auto=1').get_data(as_text=True)
    # Every link to our own pages keeps the flag: header, 'Alle N Artikel'
    own = re.findall(r'<a [^>]*href="(/[^"]*)"', html)
    assert own and all(link.endswith('auto=1') for link in own), own
    story = re.search(r'href="(/story/[0-9a-f]+\?auto=1)"', html).group(1)
    html = http.get(story).get_data(as_text=True)
    # A passer-by opened a story: the display returns to the front page
    assert '<meta http-equiv="refresh" content="1800; url=/?auto=1">' in html
    own = re.findall(r'<a [^>]*href="(/[^"]*)"', html)
    assert own and all(link.endswith('auto=1') for link in own), own
    assert f'{story.split("?")[0]}?ansicht=chronologisch&amp;auto=1' in html
    back = re.search(r'<a class="more" href="([^"]*)"', html).group(1)
    assert back == '/?auto=1'
    assert 'http-equiv="refresh"' in http.get(back).get_data(as_text=True)
    # The story aged out while it was shown: back to the front page, not a
    # 404 that never reloads
    assert http.get('/story/000000000000?auto=1').headers['Location'] == '/?auto=1'
    assert http.get('/story/000000000000').status_code == 404
    # Without the flag nothing changes
    html = http.get(story.split('?')[0]).get_data(as_text=True)
    assert 'http-equiv="refresh"' not in html and 'auto=1' not in html


def test_auto_refresh_survives_fewer_pages(config, store):
    config.web.page_size = 3
    save_stories(store, 7)
    http = create_app(config, store).test_client()
    assert http.get('/?seite=3&auto=1').status_code == 200
    # Stories age out, two pages are left (the cap stands in for that): the
    # display reloading page 3 lands on the last page instead of a 404 that
    # never reloads
    config.web.max_stories = 5
    response = http.get('/?seite=3&auto=1')
    assert response.status_code == 302
    assert response.headers['Location'] == '/?seite=2&auto=1'
    html = http.get('/?seite=3&auto=1', follow_redirects=True).get_data(as_text=True)
    assert 'Seite 2 von 2' in html and 'http-equiv="refresh"' in html
    # Down to one page: back to the plain front page, still refreshing
    config.web.max_stories = 2
    assert http.get('/?seite=3&auto=1').headers['Location'] == '/?auto=1'
    # Without auto=1 a page past the end stays a 404
    assert http.get('/?seite=3').status_code == 404


FEED_NAMES = ['Tagesschau', 'Spiegel', 'Zeit', 'FAZ', 'SZ', 'taz', 'Deutschlandfunk', 'n-tv']


def test_first_page_of_a_busy_day_stays_small(config, store):
    # Busier than the reference corpus: every story on page 1 has 8 sources
    # (a full coverage list and a link to the rest), a full snippet and
    # titles of the corpus' average length (70 characters). Shortly after
    # midnight, the worst case: every time is yesterday's and carries a
    # weekday ('Do 23:41'). A whole-hour zone puts local time between 00:00
    # and 01:00 whatever the clock says; the articles are 1-1.5 hours old.
    hour = datetime.now(timezone.utc).hour
    config.web.timezone = f'Etc/GMT+{hour}' if hour <= 12 else f'Etc/GMT-{24 - hour}'
    entries, clusters = [], []
    for k in range(30):
        ids = list(range(k * 8 + 1, k * 8 + 9))
        for n, i in enumerate(ids):
            e = entry(i, 100 + n, hours_ago=1 + k / 60, snippet=('Wort ' * 56).strip(),
                      title=f'{k:02} Eine lange Überschrift über ein wichtiges Ereignis {i:04}'
                            .ljust(70, 'x'))
            e['feed']['title'] = FEED_NAMES[n]
            entries.append(e)
        clusters.append(ClusterResult(ids, ids[0], set()))
    store.save_run(entries, clusters)
    config.miniflux_public_url = 'https://miniflux.example.com'
    # With a Miniflux client every story gets its 'Story gelesen' button
    html = create_app(config, store, FakeClient([])).test_client().get('/').get_data()
    assert len(stories_on(html.decode())) == 10
    assert len(re.findall(r'<time [^>]*>[A-Z][a-z] \d\d:\d\d</time>', html.decode())) == 70
    assert html.decode().count('>Story gelesen (8)</button>') == 10
    # 25 KB before search, sections and the read buttons (about 1.7 KB)
    assert len(html) < 27_000, len(html)


# --- Times --------------------------------------------------------------------

NOW = datetime(2026, 9, 24, 18, 32, tzinfo=timezone.utc)  # a Thursday, 20:32 in Berlin


@pytest.mark.parametrize('value, text', [
    ('2026-09-24T12:53:00+00:00', '14:53'),
    ('2026-09-23T12:53:00+00:00', 'Mi 14:53'),
    ('2026-09-23T22:30:00+00:00', '00:30'),       # after midnight in Berlin: today
    ('2026-09-17T12:53:00+00:00', '17.09. 14:53'),
    ('2026-09-18T12:53:00+00:00', 'Fr 14:53'),
    ('2026-09-24T18:02:00Z', '20:02'),
])
def test_clock_shows_absolute_local_times(value, text):
    html = str(clock(value, BERLIN, now=NOW))
    assert html.startswith('<time datetime="') and html.endswith(f'>{text}</time>')
    assert re.search(r'datetime="2026-09-\d\dT\d\d:\d\d\+02:00"', html)


def test_clock_ignores_garbage():
    for value in (None, '', 'gestern', '2026-13-01'):
        assert str(clock(value, BERLIN, now=NOW)) == ''


def test_pages_show_absolute_times(config, store):
    NewsClusterer(config, store, client=FakeClient(sample_entries())).run_clustering_cycle()
    http = create_app(config, store).test_client()
    story_id = store.top_stories(24, 50)[0]['id']
    for path in ('/', f'/story/{story_id}'):
        html = http.get(path).get_data(as_text=True)
        assert '<time datetime=' in html
        for stale in ('gerade eben', 'vor ', 'zuerst gesehen'):
            assert stale not in html, (path, stale)
    index = http.get('/').get_data(as_text=True)
    assert re.search(r'Stand <time datetime="[^"]+">\d\d:\d\d</time>', index)


def test_story_page_names_first_and_latest_report(config, store):
    entries = [entry(1, 1, hours_ago=5), entry(2, 2, hours_ago=3), entry(3, 3, hours_ago=1)]
    entries[0]['feed']['title'] = 'taz'
    entries[2]['feed']['title'] = 'Deutschlandfunk'
    story_id = store.save_run(entries, [ClusterResult([1, 2, 3], 2, set())])[0]
    html = create_app(config, store).test_client().get(f'/story/{story_id}') \
        .get_data(as_text=True)
    assert re.search(r'Erste Meldung <time[^>]*>[^<]+</time> \(taz\) · '
                     r'zuletzt <time[^>]*>[^<]+</time> \(Deutschlandfunk\)', html)


def test_chronological_view_lists_oldest_first_by_day(config, store):
    now = datetime.now(timezone.utc)
    # Depending on the time of day, 18 h ago may be yesterday in Berlin
    entries = [entry(1, 1, hours_ago=1, snippet='Neuester Stand der Dinge, ausführlich.'),
               entry(2, 2, hours_ago=18), entry(3, 3, hours_ago=2)]
    story_id = store.save_run(entries, [ClusterResult([1, 2, 3], 1, set())])[0]
    config.web.timezone = 'Europe/Berlin'  # the expected day labels are Berlin days
    http = create_app(config, store).test_client()

    html = http.get(f'/story/{story_id}?ansicht=chronologisch').get_data(as_text=True)
    assert re.findall(r'</b> (Artikel \d)', html) == ['Artikel 2', 'Artikel 3', 'Artikel 1']
    today = now.astimezone(BERLIN).date()
    days = sorted({(now - timedelta(hours=h)).astimezone(BERLIN).date() for h in (18, 2, 1)})
    assert re.findall(r'<h3>([^<]+)</h3>', html) == [web_module.day_label(d, today)
                                                     for d in days]
    assert 'Neuester Stand der Dinge' in html  # one-line snippet
    assert 'aria-current="page">Chronologisch' in html

    default = http.get(f'/story/{story_id}').get_data(as_text=True)
    assert re.findall(r'</b> (Artikel \d)', default) == ['Artikel 1', 'Artikel 3', 'Artikel 2']
    assert f'href="/story/{story_id}?ansicht=chronologisch"' in default
    assert 'Neuester Stand der Dinge, ausführlich.</span>' not in default


def test_day_labels():
    today = datetime(2026, 9, 24).date()
    assert web_module.day_label(today, today) == 'Heute'
    assert web_module.day_label(today - timedelta(days=1), today) == 'Gestern'
    assert web_module.day_label(today - timedelta(days=2), today) == 'Dienstag, 22. September'


def test_time_zone_setting(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv('TZ', raising=False)
    assert resolve_timezone('') == ZoneInfo('Europe/Berlin')
    assert resolve_timezone('America/New_York') == ZoneInfo('America/New_York')
    monkeypatch.setenv('TZ', 'UTC')
    assert resolve_timezone('') == ZoneInfo('UTC')
    assert resolve_timezone('Asia/Tokyo') == ZoneInfo('Asia/Tokyo')
    monkeypatch.setenv('TZ', ':Europe/Vienna')
    assert resolve_timezone('') == ZoneInfo('Europe/Vienna')
    monkeypatch.setenv('TZ', 'CET-1CEST')
    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        assert resolve_timezone('') == ZoneInfo('Europe/Berlin')
    assert any('CET-1CEST' in r.getMessage() for r in caplog.records)

    path = tmp_path / 'config.yaml'
    for bad in ("'Mars/Olympus'", "'../etc/passwd'", '5'):
        path.write_text(f'web:\n  timezone: {bad}\n', encoding='utf-8')
        with pytest.raises(ConfigError, match='web.timezone'):
            load_config(str(path))
    path.write_text("web:\n  timezone: 'Europe/London'\n", encoding='utf-8')
    assert load_config(str(path)).web.timezone == 'Europe/London'


def test_page_size_setting(monkeypatch, tmp_path):
    for name in ('WEB_PAGE_SIZE', 'TZ'):
        monkeypatch.delenv(name, raising=False)
    config = load_config(None)
    assert (config.web.page_size, config.web.max_stories) == (10, 100)
    monkeypatch.setenv('WEB_PAGE_SIZE', '15')
    assert load_config(None).web.page_size == 15
    monkeypatch.setenv('WEB_PAGE_SIZE', '0')
    with pytest.raises(ConfigError, match='web.page_size'):
        load_config(None)
    monkeypatch.setenv('WEB_PAGE_SIZE', 'zehn')
    with pytest.raises(ConfigError, match='WEB_PAGE_SIZE'):
        load_config(None)


# --- Coverage -----------------------------------------------------------------

def article(entry_id, feed_id, duplicate=False):
    return {'id': entry_id, 'feed_id': feed_id, 'feed_title': f'Feed {feed_id}',
            'is_duplicate': int(duplicate)}


def test_coverage_gives_every_source_a_slot_first():
    # Newest first, as _load_story returns them; headline 10 is from feed 1
    articles = [article(1, 1), article(2, 2), article(3, 2), article(4, 1, duplicate=True),
                article(5, 3), article(6, 3), article(7, 4), article(10, 1), article(8, 1)]
    story = {'headline': articles[7], 'articles': articles}
    assert [a['id'] for a in select_coverage(story, 3)] == [2, 5, 7]
    # Then the rest, newest first, still without the duplicate
    assert [a['id'] for a in select_coverage(story, 10)] == [2, 5, 7, 1, 3, 6, 8]
    assert select_coverage(story, 0) == []


def test_front_page_skips_duplicates_and_repeated_sources(config, store):
    entries = [entry(1, 1, hours_ago=1), entry(2, 1, hours_ago=1.1), entry(3, 1, hours_ago=1.2),
               entry(4, 2, hours_ago=1.3), entry(5, 2, hours_ago=1.4), entry(6, 3, hours_ago=2),
               entry(7, 4, hours_ago=3)]
    story_id = store.save_run(entries, [ClusterResult([1, 2, 3, 4, 5, 6, 7], 1, {2, 5})])[0]
    config.web.articles_per_story = 3
    http = create_app(config, store).test_client()

    html = http.get('/').get_data(as_text=True)
    listed = [int(i) for i in re.findall(r'/entry/(\d+)"', html)]
    assert listed == [1, 4, 6, 7]  # headline, then one article per other feed
    assert '+2 gleichlautende Meldungen' in html
    assert f'href="/story/{story_id}"' in html

    page = http.get(f'/story/{story_id}').get_data(as_text=True)
    assert 'Duplikat' not in page
    before, _, after = page.partition('Gleichlautende Meldungen (2)')
    assert '/entry/2"' in after and '/entry/5"' in after
    assert '/entry/2"' not in before and '/entry/3"' in before


# --- Titles and snippets ------------------------------------------------------

@pytest.mark.parametrize('text, cleaned', [
    ('Die Koalition hat sich geeinigt.[ mehr ]', 'Die Koalition hat sich geeinigt.'),
    ('Die Koalition hat sich geeinigt. [mehr]', 'Die Koalition hat sich geeinigt.'),
    ('Wer wird der nächste Kandidat? mehr...', 'Wer wird der nächste Kandidat?'),
    ('Die Koalition hat sich geeinigt. Weiterlesen »', 'Die Koalition hat sich geeinigt.'),
    ('Den Laden an der Ecke gibt es nicht mehr', 'Den Laden an der Ecke gibt es nicht mehr'),
    ('None', ''),
    ('[ mehr ]', ''),
    ('Video ansehen', ''),
    ('Bundestag beschließt Haushalt', ''),  # the title again
])
def test_clean_snippet(text, cleaned):
    assert clean_snippet(text, 'Bundestag beschließt Haushalt') == cleaned


@pytest.mark.parametrize('tail', ['[ mehr ] ', 'Weiterlesen '])
def test_clean_snippet_is_linear_in_repeated_tails(tail):
    # A hostile item ending in thousands of teaser links (up to
    # MAX_HTML_CHARS of HTML) must not stall every clustering run
    text = 'Die Koalition hat sich geeinigt. ' + tail * 20000
    started = time.perf_counter()
    assert clean_snippet(text) == 'Die Koalition hat sich geeinigt.'
    assert time.perf_counter() - started < 1


def test_stored_snippets_are_clean(config, store):
    entries = sample_entries()
    entries[0]['content'] = '<p>Der Bundestag hat den Haushalt beschlossen.</p> [ mehr ]'
    entries[2]['content'] = 'None'
    NewsClusterer(config, store, client=FakeClient(entries)).run_clustering_cycle()
    snippets = {a['id']: a['snippet'] for s in store.top_stories(24, 50) for a in s['articles']}
    assert snippets[1] == 'Der Bundestag hat den Haushalt beschlossen.'
    assert snippets[3] == ''
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert '[ mehr ]' not in html and '>None<' not in html


def test_untitled_articles_are_labelled_with_their_text():
    ticker = {'title': '', 'feed_title': 'MDR',
              'snippet': '+++ Landtag wählt neuen Präsidenten +++ Bahn streicht Verbindungen '
                         'zwischen Leipzig und Halle +++'}
    # At most 80 characters of the text, cut at a word
    assert display_title(ticker) == ('MDR: +++ Landtag wählt neuen Präsidenten +++ Bahn '
                                     'streicht Verbindungen zwischen …')
    assert display_title(ticker, with_feed=False).startswith('+++ Landtag')
    assert display_title({'title': '', 'feed_title': 'MDR', 'snippet': ''}) == '(ohne Titel)'
    assert display_title({'title': ' Titel ', 'snippet': 'x'}) == 'Titel'
    assert display_title(None) == '(ohne Titel)'


# --- Tap targets --------------------------------------------------------------

def test_links_are_large_tap_targets(config, store):
    NewsClusterer(config, store, client=FakeClient(sample_entries())).run_clustering_cycle()
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert 'ul.coverage a { display: block; padding: .6rem 0; min-height: 44px; }' in html
    # :visited rules may only change colours, anything else is ignored
    visited = re.findall(r'a:visited \{([^}]*)\}', html)
    assert visited and all(re.fullmatch(r'\s*color: var\(--muted\);\s*', rule)
                           for rule in visited)
    # Source and time are part of the link, not separate small targets
    assert re.search(r'<li><a href="[^"]+/entry/\d+"><b>[^<]+:</b> [^<]+ <time ', html)
    story_id = store.top_stories(24, 50)[0]['id']
    page = create_app(config, store).test_client().get(f'/story/{story_id}') \
        .get_data(as_text=True)
    assert re.search(r'<a class="original meta" href="https://example\.org/\d+" '
                     r'rel="noopener noreferrer" aria-label="Original bei [^"]+">', page)
    assert '<span aria-hidden="true">←</span> Zurück' in page
    # The disclosure triangle is only drawn while summary is a list-item
    assert 'details summary { display: list-item; min-height: 44px;' in page
    assert not re.search(r'summary[^{]*\{[^}]*display: inline-block', page)


# --- Home-screen install ------------------------------------------------------

def test_manifest_and_icons(config, store):
    http = create_app(config, store).test_client()
    response = http.get('/static/manifest.webmanifest')
    assert response.status_code == 200
    assert response.mimetype == 'application/manifest+json'
    manifest = json.loads(response.get_data())
    assert manifest['start_url'] == '/' and manifest['display'] == 'standalone'
    assert (manifest['name'], manifest['short_name']) == ('aRSSe Top Stories', 'aRSSe')
    for icon in manifest['icons']:
        image = http.get(icon['src'])
        assert image.status_code == 200 and image.mimetype == icon['type'], icon
        if icon['type'] == 'image/png':
            width = int.from_bytes(image.get_data()[16:20], 'big')
            assert icon['sizes'] == f'{width}x{width}'
    favicon = http.get('/favicon.ico')
    assert favicon.status_code == 200 and favicon.get_data()[:4] == b'\x00\x00\x01\x00'

    html = http.get('/').get_data(as_text=True)
    # use-credentials: without it browsers fetch the manifest without the
    # login, and a reverse proxy asking for one answers 401
    for tag in ('<link rel="manifest" href="/static/manifest.webmanifest" '
                'crossorigin="use-credentials">',
                '<link rel="apple-touch-icon" href="/static/icon-192.png">',
                '<meta name="theme-color" content="#000000">'):
        assert tag in html


def test_manifest_is_public_but_pages_are_not(basic_config, store):
    http = create_app(basic_config, store).test_client()
    for path in ('/static/manifest.webmanifest', '/static/icon-192.png', '/static/icon.svg'):
        response = http.get(path)
        assert response.status_code == 200, path
        assert response.headers['Content-Security-Policy'] == CONTENT_SECURITY_POLICY
    for path in ('/', '/favicon.ico', '/?seite=2'):
        assert http.get(path).status_code == 401, path
    credentials = {'Authorization': 'Basic bGVzZXI6Z2VoZWlt'}  # leser:geheim
    assert http.get('/favicon.ico', headers=credentials).status_code == 200


def test_pages_need_nothing_the_csp_forbids(config, store):
    config.web.page_size = 1
    NewsClusterer(config, store, client=FakeClient(sample_entries())).run_clustering_cycle()
    http = create_app(config, store).test_client()
    story_id = store.top_stories(24, 50)[0]['id']
    for path in ('/', '/?seite=2&auto=1', f'/story/{story_id}',
                 f'/story/{story_id}?ansicht=chronologisch'):
        html = http.get(path).get_data(as_text=True)
        assert '<script' not in html and '@import' not in html
        assert not re.search(r'<[^>]+\son\w+=', html)  # no event handlers
        assert not re.search(r'<(img|iframe|object|embed|video|audio|source)\b', html)
        # Everything loaded by the page comes from the same origin (manifest-src,
        # img-src 'self'); links to Miniflux and publishers are navigation only
        for tag in re.findall(r'<link [^>]*>', html):
            assert re.search(r'href="/[^/]', tag), tag


@pytest.fixture
def basic_config(config):
    config.web.auth.mode = 'basic'
    config.web.auth.username = 'leser'
    config.web.auth.password = 'geheim'
    return config


def decoded_png(data: bytes) -> list:
    """The chunks of a PNG with the image data decompressed, CRCs left out."""
    import struct
    import zlib
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    chunks, idat, pos = [], b'', 8
    while pos < len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b'IDAT':
            idat += body
        else:
            chunks.append((kind, body))
        pos += 12 + length
    return chunks + [(b'pixels', zlib.decompress(idat))]


def decoded_ico(data: bytes) -> tuple:
    """Header and directory entry of a one-image .ico (without the image size) and its PNG."""
    import struct
    *entry, image_size, offset = struct.unpack('<BBBBHHII', data[6:22])
    assert offset == 22 and len(data) == offset + image_size
    return data[:6], entry, decoded_png(data[offset:])


def test_make_icons_is_reproducible(tmp_path, monkeypatch):
    import importlib.util
    import types
    import zlib
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'make-icons.py'
    spec = importlib.util.spec_from_file_location('make_icons', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    static = Path(web_module.STATIC_DIR)
    monkeypatch.setattr(module, 'STATIC', tmp_path)
    # Deflate output differs between implementations (zlib-ng on newer
    # distributions), so the pixels are compared, not the compressed bytes;
    # a different compression level stands in for another implementation
    monkeypatch.setattr(module, 'zlib', types.SimpleNamespace(
        crc32=zlib.crc32, compress=lambda data, level=-1: zlib.compress(data, 1)))
    module.main()
    for name in ('icon-192.png', 'icon-512.png'):
        assert decoded_png((tmp_path / name).read_bytes()) \
            == decoded_png((static / name).read_bytes()), name
    assert decoded_ico((tmp_path / 'favicon.ico').read_bytes()) \
        == decoded_ico((static / 'favicon.ico').read_bytes())
    assert (tmp_path / 'icon.svg').read_bytes() == (static / 'icon.svg').read_bytes()


def test_single_page_story_count_wording(config, store):
    save_stories(store, 1)
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert '· 1 Story' in html


# --- Miniflux themes ----------------------------------------------------------

@pytest.mark.parametrize('name', ['eink-theme.css', 'color-theme.css'])
def test_miniflux_themes_load_no_web_fonts(name):
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / 'css' / name).read_text(encoding='utf-8')
    active = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    assert '@import' not in active and 'googleapis' not in active and 'url(' not in active
    # The web fonts stay available as an opt-in
    assert "/* @import url('https://fonts.googleapis.com/" in css
    assert re.search(r'--font-body: [^;]*(Georgia|sans-serif)', active)

"""
Sections (Rubriken), topics ('Mehr zum Thema', 'Verwandte Stories'),
search and the schema version that stores them.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import news_clustering
import store as store_module
from config import DEFAULT_PATH_SECTIONS, ConfigError, load_config
from conftest import FakeClient, sample_entries
from news_clustering import NewsClusterer, section_of
from store import MIGRATIONS, ClusterResult, StoryStore
from web import create_app

PATH_SECTIONS = [(re.compile(p, re.IGNORECASE), name)
                 for p, name in DEFAULT_PATH_SECTIONS.items()]


def entry(entry_id, feed_id, hours_ago=1.0, title=None, snippet='', section=None,
          status='unread'):
    now = datetime.now(timezone.utc)
    return {
        'id': entry_id,
        'feed_id': feed_id,
        'feed': {'id': feed_id, 'title': f'Feed {feed_id}'},
        'title': title or f'Artikel {entry_id}',
        'url': f'https://example.org/{entry_id}',
        'published_at': (now - timedelta(hours=hours_ago)).isoformat(),
        '_snippet': snippet,
        '_section': section,
        'status': status,
    }


def with_category(e, title, **category):
    e['feed'] = dict(e['feed'], category={'id': 7, 'title': title, **category})
    return e


def headlines(html):
    return re.findall(r'<article class="story">\s*<h2><a [^>]*>([^<]*)</a>', html)


# --- Sections -----------------------------------------------------------------

@pytest.mark.parametrize('category, url, section', [
    ('Sport', 'https://www.tagesschau.de/inland/haushalt-100.html', 'Sport'),
    ('All', 'https://www.tagesschau.de/ausland/amerika/usa-trump-100.html', 'Politik'),
    ('Alle', 'https://www.faz.net/aktuell/sport/fussball/klopp-110.html', 'Sport'),
    (None, 'https://www.n-tv.de/sport/fussball_nationalmannschaft/Klopp-123.html', 'Sport'),
    ('All', 'https://www.mdr.de/nachrichten/sachsen/dresden/stadtrat-100.html', 'Regional'),
    # Regional wins over the broad Politik, whatever comes first in the path
    ('All', 'https://www.tagesschau.de/inland/regional/bayern/br-wahl-100.html', 'Regional'),
    ('All', 'https://www.heise.de/news/Neuer-Prozessor-123.html', None),
    # The last segment names the article: 'sport' there says nothing
    ('All', 'https://www.zeit.de/news/2026-09/sport', None),
    ('All', 'https://www.spiegel.de/wirtschaft/unternehmen/x-a-123', 'Wirtschaft'),
    ('  ', 'https://www.spiegel.de/kultur/kino/film-a-123', 'Kultur'),
    ('All', 'not a url', None),
    ('All', None, None),
])
def test_section_of(category, url, section):
    e = {'id': 1, 'url': url, 'feed': {'id': 1, 'title': 'Feed'}}
    if category is not None:
        with_category(e, category)
    assert section_of(e, PATH_SECTIONS) == section


def test_section_of_tolerates_missing_fields():
    assert section_of({'id': 1}, PATH_SECTIONS) is None
    assert section_of({'id': 1, 'feed': None, 'url': 'https://x.example/sport/a'},
                      PATH_SECTIONS) == 'Sport'
    assert section_of({'id': 1, 'feed': {'category': None}}, PATH_SECTIONS) is None
    assert section_of({'id': 1, 'feed': {'category': {'title': 42}}}, PATH_SECTIONS) is None
    long_name = with_category({'id': 1, 'feed': {}}, 'x' * 200)
    assert section_of(long_name, PATH_SECTIONS) == 'x' * 40
    # Without path sections only Miniflux categories count
    assert section_of({'id': 1, 'url': 'https://x.example/sport/a'}, []) is None


def sectioned_entries():
    entries = sample_entries()
    for e in entries[:3]:  # budget: default category, tagesschau.de/ausland/...
        with_category(e, 'All')
        e['url'] = f"https://www.tagesschau.de/ausland/europa/haushalt-{e['id']}.html"
    for e in entries[3:5]:  # chip: a Miniflux category
        with_category(e, 'Sport')
    return entries


def test_sections_come_from_categories_and_urls(config, store):
    client = FakeClient(sectioned_entries())
    NewsClusterer(config, store, client=client).run_clustering_cycle()
    sections = {s['headline']['id']: s['section'] for s in store.top_stories(24, 50)}
    assert sections == {1: 'Politik', 4: 'Sport'}

    http = create_app(config, store).test_client()
    html = http.get('/').get_data(as_text=True)
    assert '<nav class="sections" aria-label="Rubriken">' in html
    assert '<span aria-current="page">Alle</span>' in html
    assert '<a href="/?rubrik=Politik">Politik (1)</a>' in html
    assert '<a href="/?rubrik=Sport">Sport (1)</a>' in html

    sport = http.get('/?rubrik=Sport').get_data(as_text=True)
    assert headlines(sport) == ['Neuer Prozessor vorgestellt']
    assert '<span aria-current="page">Sport (1)</span>' in sport and 'href="/"' in sport
    assert '1 Story' in sport
    assert [s['section'] for s in http.get('/api/stories?rubrik=Sport').get_json()] == ['Sport']
    assert len(http.get('/api/stories').get_json()) == 2

    empty = http.get('/?rubrik=Kultur')
    assert empty.status_code == 200
    assert 'Keine Stories in der Rubrik „Kultur“' in empty.get_data(as_text=True)
    assert http.get('/?rubrik=' + 'x' * 41).status_code == 404


def test_section_links_keep_the_display_flags(config, store):
    NewsClusterer(config, store, client=FakeClient(sectioned_entries())).run_clustering_cycle()
    config.web.page_size = 1
    http = create_app(config, store).test_client()
    html = http.get('/?alle=1&auto=1').get_data(as_text=True)
    assert 'href="/?rubrik=Sport&amp;alle=1&amp;auto=1"' in html
    assert 'href="/?alle=1&amp;seite=2&amp;auto=1"' in html
    html = http.get('/?rubrik=Sport&alle=1').get_data(as_text=True)
    assert 'href="/?rubrik=Sport">Gelesene ausblenden' in html


def test_hidden_categories_are_respected(config, store):
    entries = sectioned_entries()
    for e in entries[3:5]:
        e['feed']['category']['hide_globally'] = True
    client = FakeClient(entries)
    NewsClusterer(config, store, client=client).run_clustering_cycle()
    assert all(call['globally_visible'] is True for call in client.calls)
    assert [s['headline']['id'] for s in store.top_stories(24, 50)] == [1]

    config.miniflux_respect_hide_globally = False
    client = FakeClient(entries)
    NewsClusterer(config, store, client=client).run_clustering_cycle()
    assert all(call['globally_visible'] is None for call in client.calls)
    assert len(store.top_stories(24, 50)) == 2


def test_story_section_is_the_majority_of_its_window(store):
    entries = [entry(1, 1, section='Sport'), entry(2, 2, section='Politik'),
               entry(3, 3, section='Sport'), entry(4, 4), entry(5, 5, hours_ago=30,
                                                                  section='Politik'),
               entry(6, 6, hours_ago=31, section='Politik')]
    story_id = store.save_run(entries, [ClusterResult([1, 2, 3, 4, 5, 6], 1, set())])[0]
    assert store.get_story(story_id, 24)['section'] == 'Sport'
    # A tie goes to the newest article
    tie = [entry(7, 1, hours_ago=2, section='Sport'), entry(8, 2, hours_ago=1,
                                                             section='Kultur')]
    story_id = store.save_run(tie, [ClusterResult([7, 8], 7, set())])[0]
    assert store.get_story(story_id, 24)['section'] == 'Kultur'
    # No section known: none
    story_id = store.save_run([entry(9, 1), entry(10, 2)],
                              [ClusterResult([9, 10], 9, set())])[0]
    assert store.get_story(story_id, 24)['section'] is None


# --- Topics -------------------------------------------------------------------

def designed_distances(between):
    """Budget 1-3 and chip 4-5 are stories; `between` apart; weather far away."""
    d = np.full((6, 6), 0.99)
    d[np.ix_([0, 1, 2], [0, 1, 2])] = 0.3
    d[np.ix_([3, 4], [3, 4])] = 0.2
    d[np.ix_([0, 1, 2], [3, 4])] = between
    d[np.ix_([3, 4], [0, 1, 2])] = between
    np.fill_diagonal(d, 0)
    return d


@pytest.mark.parametrize('between, topic_threshold, keys', [
    (0.85, 0.9, {(1, 2, 3): 1, (4, 5): 1}),
    (0.95, 0.9, {(1, 2, 3): 1, (4, 5): 4}),
    (0.85, 0.8, {(1, 2, 3): 1, (4, 5): 4}),
    (0.85, 0, {(1, 2, 3): None, (4, 5): None}),
])
def test_topic_pass_groups_stories_of_one_event(config, store, monkeypatch, between,
                                                topic_threshold, keys):
    config.clustering.topic_threshold = topic_threshold
    monkeypatch.setattr(news_clustering, 'cosine_distances',
                        lambda matrix: designed_distances(between))
    clusters = NewsClusterer(config, store, client=FakeClient([]))._cluster(sample_entries())
    # Story boundaries stay at clustering.threshold
    assert {tuple(sorted(c.entry_ids)): c.topic_key for c in clusters} == keys


def save_topics(store):
    """Stories A (5 feeds) and B (3 feeds) share a topic; C (4 feeds) stands alone."""
    entries, clusters = [], []
    for ids, feeds, key, title in (([1, 2, 3, 4, 5], 5, 1, 'Klopp gibt Debüt'),
                                   ([6, 7, 8], 3, 1, 'Klopp nominiert Neuling'),
                                   ([9, 10, 11, 12], 4, None, 'Haushalt beschlossen')):
        entries += [entry(i, n + 1, title=f'{title} {i}') for n, i in enumerate(ids[:feeds])]
        clusters.append(ClusterResult(ids, ids[0], set(), topic_key=key))
    return store.save_run(entries, clusters)


def test_front_page_gives_every_topic_one_slot(config, store):
    story_a, story_b, story_c = save_topics(store).values()
    http = create_app(config, store).test_client()
    html = http.get('/').get_data(as_text=True)
    assert headlines(html) == ['Klopp gibt Debüt 1', 'Haushalt beschlossen 9']
    assert '2 Stories' in html
    assert (f'<li><a href="/story/{story_b}"><b>Mehr zum Thema:</b> Klopp nominiert Neuling 6 '
            f'(3 Quellen)</a></li>') in html

    # Pages count topics, not stories
    config.web.page_size = 1
    assert 'Seite 1 von 2 · 2 Stories' in http.get('/').get_data(as_text=True)
    assert headlines(http.get('/?seite=2').get_data(as_text=True)) == ['Haushalt beschlossen 9']
    assert http.get('/?seite=3').status_code == 404
    # The API stays flat
    assert len(http.get('/api/stories').get_json()) == 3

    page = http.get(f'/story/{story_a}').get_data(as_text=True)
    assert '<h3>Verwandte Stories</h3>' in page and f'href="/story/{story_b}"' in page
    page = http.get(f'/story/{story_b}').get_data(as_text=True)
    assert f'href="/story/{story_a}"' in page
    assert 'Verwandte Stories' not in http.get(f'/story/{story_c}').get_data(as_text=True)


def test_reading_the_lead_story_hands_the_slot_to_the_next(config, store):
    story_a, story_b, _ = save_topics(store).values()
    http = create_app(config, store, FakeClient([])).test_client()
    http.post(f'/story/{story_a}/gelesen', headers={'Sec-Fetch-Site': 'same-origin'})
    html = http.get('/').get_data(as_text=True)
    assert headlines(html) == ['Haushalt beschlossen 9', 'Klopp nominiert Neuling 6']
    assert 'Mehr zum Thema' not in html
    html = http.get('/?alle=1').get_data(as_text=True)
    assert headlines(html)[0] == 'Klopp gibt Debüt 1' and 'Mehr zum Thema' in html


def test_many_related_stories_link_to_the_story_page(config, store):
    entries, clusters = [], []
    for k in range(6):
        ids = [10 * k + 1, 10 * k + 2]
        entries += [entry(ids[0], 1, hours_ago=1 + k), entry(ids[1], 2, hours_ago=1 + k)]
        clusters.append(ClusterResult(ids, ids[0], set(), topic_key=1))
    lead = store.save_run(entries, clusters)[0]
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert html.count('Mehr zum Thema:') == 3
    assert f'<a class="more" href="/story/{lead}">Alle 2 Artikel · 5 verwandte' in html


def test_group_counts_per_section(config, store):
    entries, clusters = [], []
    for k, (section, key) in enumerate((('Sport', 1), ('Sport', 1), ('Sport', None),
                                        ('Politik', 1))):
        ids = [10 * k + 1, 10 * k + 2]
        entries += [entry(ids[0], 1, hours_ago=1 + k, section=section),
                    entry(ids[1], 2, hours_ago=1 + k, section=section)]
        clusters.append(ClusterResult(ids, ids[0], set(), topic_key=key))
    store.save_run(entries, clusters)
    page = store.front_page(24, 100, 0, 10, min_sources=2)
    assert page.total == 2 and page.sections == [('Sport', 2), ('Politik', 1)]
    assert store.front_page(24, 100, 0, 10, min_sources=2, section='Politik').total == 1


# --- Search -------------------------------------------------------------------

def save_searchable(store):
    entries = [
        entry(1, 1, title='Klopp gibt Debüt als Bundestrainer', snippet='Premiere in Amsterdam'),
        entry(2, 2, title='Bundestrainer Klopp siegt'),
        entry(3, 1, title='Ärzte warnen vor Hitze', snippet='100% sicher: der Sommer kommt'),
        entry(4, 2, title='Hitzewelle erreicht Deutschland'),
        # A story from days ago: still stored (retention), not in the window
        entry(5, 1, hours_ago=50, title='Felssturz an der Zugspitze'),
        entry(6, 2, hours_ago=51, title='Zugspitze: Bergbahn gesperrt'),
        # One feed only: never shown, not found either
        entry(7, 3, title='Klopp-Tasse im Angebot'),
        entry(8, 3, title='Klopp-Poster im Angebot'),
    ]
    return store.save_run(entries, [ClusterResult([1, 2], 1, set()),
                                    ClusterResult([3, 4], 3, set()),
                                    ClusterResult([5, 6], 5, set()),
                                    ClusterResult([7, 8], 7, set())])


def test_search_finds_stories_by_title_and_snippet(config, store):
    config.miniflux_public_url = 'https://mf.example'
    save_searchable(store)
    http = create_app(config, store).test_client()
    html = http.get('/suche?q=Klopp').get_data(as_text=True)
    assert '1 Story zu „Klopp“' in html
    assert 'Klopp gibt Debüt als Bundestrainer' in html and 'Tasse' not in html
    assert 'value="Klopp"' in html
    assert 'href="https://mf.example/search?q=Klopp">In Miniflux suchen' in html
    assert re.search(r'<a class="more" href="/story/[0-9a-f]+">Zur Story', html)

    # Case-insensitive for umlauts, too; the snippet counts
    assert 'Ärzte warnen' in http.get('/suche?q=ärzte').get_data(as_text=True)
    assert 'Ärzte warnen' in http.get('/suche?q=SOMMER').get_data(as_text=True)
    assert 'Klopp gibt' in http.get('/suche?q=amsterdam').get_data(as_text=True)


def test_search_treats_wildcards_literally(config, store):
    save_searchable(store)
    http = create_app(config, store).test_client()
    assert '1 Story zu „100%“' in http.get('/suche?q=100%25').get_data(as_text=True)
    assert '0 Stories zu „%%“' in http.get('/suche?q=%25%25').get_data(as_text=True)
    assert '0 Stories zu „__“' in http.get('/suche?q=__').get_data(as_text=True)
    assert '0 Stories' in http.get('/suche?q=Kl_pp').get_data(as_text=True)
    assert store.search('\\', 24, 50) == []


def test_search_finds_older_stories_and_links_them_to_the_publisher(config, store):
    config.miniflux_public_url = 'https://mf.example'
    save_searchable(store)
    http = create_app(config, store).test_client()
    html = http.get('/suche?q=zugspitze').get_data(as_text=True)
    assert '1 Story zu „zugspitze“' in html
    assert 'href="https://example.org/5"' in html and 'mf.example/feed' not in html
    # Its story page is gone with the window: no link there
    assert 'Zur Story' not in html


@pytest.mark.parametrize('query', ['', 'K', ' K ', 'x' * 101])
def test_short_or_long_queries_show_the_empty_form(config, store, query):
    save_searchable(store)
    response = create_app(config, store).test_client().get('/suche', query_string={'q': query})
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Suchbegriff mit mindestens 2 Zeichen' in html
    assert 'value=""' in html and 'Stories zu' not in html and 'In Miniflux suchen' not in html
    assert ('Höchstens 100 Zeichen' in html) == (len(query) > 100)


def test_search_results_are_paged(config, store):
    config.web.page_size = 1
    save_searchable(store)
    http = create_app(config, store).test_client()
    first = http.get('/suche?q=e').get_data(as_text=True)  # too short: form only
    assert 'Stories zu' not in first
    first = http.get('/suche?q=er').get_data(as_text=True)
    assert 'Seite 1 von 3' in first and 'href="/suche?q=er&amp;seite=2"' in first
    assert http.get('/suche?q=er&seite=3').status_code == 200
    assert http.get('/suche?q=er&seite=4').status_code == 404
    assert http.get('/suche?q=er&seite=x').status_code == 404


def test_search_form_on_every_page(config, store):
    save_searchable(store)
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert '<form class="search" action="/suche" role="search"><input type="search" name="q"' \
        in html


# --- Schema -------------------------------------------------------------------

def test_v3_database_gets_sections_topics_and_read_marks(tmp_path, monkeypatch):
    path = str(tmp_path / 'arsse.db')
    monkeypatch.setattr(store_module, 'MIGRATIONS', MIGRATIONS[:3])
    StoryStore(path)
    monkeypatch.undo()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = sqlite3.connect(path)
    with conn:
        conn.executemany("INSERT INTO entries (id, feed_id, feed_title, title, url, "
                         "published_at, status) VALUES (?, ?, 'Feed', ?, '', ?, 'unread')",
                         [(1, 1, 'Alt 1', now), (2, 2, 'Alt 2', now)])
        conn.execute("INSERT INTO stories VALUES ('oldstory', 1, ?, ?)", (now, now))
        conn.executemany("INSERT INTO story_entries VALUES (?, 'oldstory', 0)", [(1,), (2,)])
    conn.close()

    st = StoryStore(path)
    story = st.get_story('oldstory', 24)
    assert (story['section'], story['topic_key'], story['unread_count']) == (None, None, 2)
    assert [s['id'] for s in st.front_page(24, 10, 0, 10, 2).stories] == ['oldstory']
    st.mark_read([1, 2, 99])
    assert st.get_story('oldstory', 24)['new_count'] == 0
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [r[0] for r in conn.execute("SELECT entry_id FROM user_read")] == [1, 2]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = "
                            "'idx_entries_published'").fetchone()
    finally:
        conn.close()


def test_read_marks_leave_with_their_articles(store):
    entries = [entry(1, 1, hours_ago=24 * 8), entry(2, 2)]
    store.save_run(entries, [ClusterResult([1, 2], 2, set())])
    store.mark_read([1, 2])
    store.cleanup(7)
    conn = sqlite3.connect(store.db_path)
    try:
        assert [r[0] for r in conn.execute("SELECT entry_id FROM user_read")] == [2]
    finally:
        conn.close()


# --- Settings -----------------------------------------------------------------

def write_yaml(tmp_path, text):
    path = tmp_path / 'config.yaml'
    path.write_text(text, encoding='utf-8')
    return str(path)


@pytest.fixture
def clean_env(monkeypatch):
    for var in ('CLUSTERING_THRESHOLD', 'CLUSTERING_TOPIC_THRESHOLD'):
        monkeypatch.delenv(var, raising=False)


def test_topic_threshold_setting(tmp_path, monkeypatch, clean_env):
    assert load_config(None).clustering.topic_threshold == 0.9
    assert load_config(write_yaml(tmp_path, 'clustering:\n  topic_threshold: 0\n')) \
        .clustering.topic_threshold == 0
    for text in ('clustering:\n  topic_threshold: 0.7\n',
                 'clustering:\n  topic_threshold: 0.75\n',
                 'clustering:\n  topic_threshold: 1.0\n',
                 'clustering:\n  threshold: 0.95\n  topic_threshold: 0.92\n',
                 "clustering:\n  topic_threshold: '0.9'\n"):
        with pytest.raises(ConfigError, match='topic_threshold'):
            load_config(write_yaml(tmp_path, text))
    monkeypatch.setenv('CLUSTERING_TOPIC_THRESHOLD', '0.85')
    assert load_config(None).clustering.topic_threshold == 0.85
    monkeypatch.setenv('CLUSTERING_TOPIC_THRESHOLD', 'viel')
    with pytest.raises(ConfigError, match='CLUSTERING_TOPIC_THRESHOLD'):
        load_config(None)


def test_a_high_threshold_turns_the_default_topics_off(tmp_path, monkeypatch, clean_env,
                                                      caplog):
    # A CLUSTERING_THRESHOLD that was valid before topics existed still loads,
    # with the shipped config.yaml as well
    shipped = str(Path(__file__).resolve().parent.parent / 'config.yaml')
    monkeypatch.setenv('CLUSTERING_THRESHOLD', '0.9')
    for path in (None, shipped):
        cfg = load_config(path)
        assert cfg.clustering.threshold == 0.9 and cfg.clustering.topic_threshold == 0
    assert 'topics are off' in caplog.text
    assert load_config(write_yaml(tmp_path, 'clustering:\n  threshold: 0.95\n')) \
        .clustering.topic_threshold == 0
    # Set on purpose, a topic threshold at or below the threshold stays an error
    monkeypatch.setenv('CLUSTERING_TOPIC_THRESHOLD', '0.9')
    with pytest.raises(ConfigError, match='topic_threshold'):
        load_config(None)
    monkeypatch.setenv('CLUSTERING_TOPIC_THRESHOLD', '0.95')
    assert load_config(shipped).clustering.topic_threshold == 0.95


def test_path_sections_setting(tmp_path, clean_env):
    cfg = load_config(write_yaml(tmp_path, "web:\n  path_sections:\n"
                                           "    'wetter|klima': Umwelt\n"))
    assert cfg.web.path_sections == {'wetter|klima': 'Umwelt'}
    assert load_config(write_yaml(tmp_path, "web:\n  path_sections: {}\n")) \
        .web.path_sections == {}
    assert load_config(None).web.path_sections == DEFAULT_PATH_SECTIONS
    shipped = Path(__file__).resolve().parent.parent / 'config.yaml'
    assert load_config(str(shipped)).web.path_sections == DEFAULT_PATH_SECTIONS
    for text in ("web:\n  path_sections: [sport]\n",
                 "web:\n  path_sections:\n    'sport(': Sport\n",
                 "web:\n  path_sections:\n    sport: ''\n",
                 "web:\n  path_sections:\n    sport: " + 'x' * 41 + "\n",
                 "web:\n  path_sections:\n    sport: 3\n"):
        with pytest.raises(ConfigError, match='path_sections'):
            load_config(write_yaml(tmp_path, text))


def test_respect_hide_globally_setting(tmp_path, clean_env):
    assert load_config(None).miniflux_respect_hide_globally is True
    cfg = load_config(write_yaml(tmp_path, 'miniflux:\n  respect_hide_globally: false\n'))
    assert cfg.miniflux_respect_hide_globally is False
    with pytest.raises(ConfigError, match='respect_hide_globally'):
        load_config(write_yaml(tmp_path, "miniflux:\n  respect_hide_globally: 'nein'\n"))

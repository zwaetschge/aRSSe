from conftest import FakeClient, make_entry, sample_entries
from news_clustering import NewsClusterer
from web import create_app


def client_for(config, store, entries=None):
    NewsClusterer(config, store, client=FakeClient(entries or sample_entries())) \
        .run_clustering_cycle()
    return create_app(config, store).test_client()


def test_index_lists_stories(config, store):
    config.miniflux_public_url = 'https://news.example.com/'
    http = client_for(config, store)
    html = http.get('/').get_data(as_text=True)

    assert 'Bundestag' in html and 'Prozessor' in html
    assert 'https://news.example.com/feed/' in html
    assert 'Wetter' not in html  # noise, no story


def test_story_page_and_404(config, store):
    http = client_for(config, store)
    story_id = store.top_stories(24, 50)[0]['id']

    assert http.get(f'/story/{story_id}').status_code == 200
    assert http.get('/story/doesnotexist').status_code == 404


def test_untrusted_content_is_escaped(config, store):
    entries = sample_entries()
    entries[0]['title'] = '<script>alert(1)</script> Bundestag beschließt Haushalt'
    entries[0]['url'] = 'javascript:alert(1)'
    http = client_for(config, store, entries)

    story_id = next(s['id'] for s in store.top_stories(24, 50)
                    if 1 in {a['id'] for a in s['articles']})
    for path in ('/', f'/story/{story_id}'):
        html = http.get(path).get_data(as_text=True)
        assert '<script>alert(1)' not in html
        assert 'javascript:' not in html


def test_healthz(config, store):
    http = create_app(config, store).test_client()
    assert http.get('/healthz').status_code == 503

    http = client_for(config, store)
    response = http.get('/healthz')
    assert response.status_code == 200
    assert response.get_json()['last_stats']['clusters_found'] == 2


def test_api_stories(config, store):
    http = client_for(config, store)
    data = http.get('/api/stories').get_json()
    assert {len(s['articles']) for s in data} == {2, 3}


def test_single_source_stories_are_hidden(config, store):
    ads = [make_entry(20 + i, 4, f'Anzeige: Notebook {i} zum Tiefstpreis bei Amazon',
                      'Jetzt zum Tiefstpreis bei Amazon sichern, Angebot nur heute gültig.')
           for i in range(3)]
    http = client_for(config, store, sample_entries() + ads)

    assert any(s['source_count'] == 1 for s in store.top_stories(24, 50))
    html = http.get('/').get_data(as_text=True)
    assert 'Tiefstpreis' not in html
    assert 'Bundestag' in html

    config.web.min_sources = 1
    html = create_app(config, store).test_client().get('/').get_data(as_text=True)
    assert 'Tiefstpreis' in html

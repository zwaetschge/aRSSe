"""Clustering quality and headline choice (the gold harness guards the numbers)."""

import json
from pathlib import Path

import pytest
from sklearn.metrics.pairwise import cosine_similarity

from conftest import BUDGET, FakeClient, make_entry, sample_entries
from news_clustering import NewsClusterer

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def fixture_entries():
    with open(FIXTURES / 'eval_corpus.json', encoding='utf-8') as f:
        return json.load(f)


def stories(clusters):
    return sorted(sorted(c.entry_ids) for c in clusters)


def similarity(clusterer, entries, a, b):
    """Cosine similarity of two entries in the clustering vector space."""
    texts = [clusterer._preprocess_entry(dict(e)) for e in entries]
    matrix = cosine_similarity(clusterer._build_vectorizer().fit_transform(texts))
    index = {e['id']: i for i, e in enumerate(entries)}
    return matrix[index[a], index[b]]


def headline_of(store, entry_id):
    story = next(s for s in store.top_stories(24, 50)
                 if entry_id in {a['id'] for a in s['articles']})
    return story['headline']['id']


def test_unigrams_by_default(config, store):
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert clusterer._build_vectorizer().ngram_range == (1, 1)
    config.clustering.ngram_max = 2
    assert clusterer._build_vectorizer().ngram_range == (1, 2)


def test_one_shared_rare_word_does_not_make_a_story(config, store):
    # Articles 9 and 10 (two feeds) only share 'Razzia'; 11 and 12 are a
    # real event reported in different words
    entries = fixture_entries()
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert 0.25 < similarity(clusterer, entries, 9, 10) < 0.30
    assert similarity(clusterer, entries, 11, 12) >= 0.35

    found = stories(clusterer._cluster([dict(e) for e in entries]))
    assert [9, 10] not in found
    assert [11, 12] in found
    assert found == [[1, 2], [3, 4], [5, 6], [7, 8], [11, 12]]

    # Average linkage alone accepts the pair (1 - threshold = 0.25)
    config.clustering.min_pair_similarity = 0
    assert [9, 10] in stories(clusterer._cluster([dict(e) for e in entries]))


def test_pair_floor_applies_only_to_two_article_stories(config, store):
    # The chip pair is very similar, but a floor above it removes it; the
    # three budget articles stay together whatever their pair similarities
    config.clustering.min_pair_similarity = 0.99
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert stories(clusterer._cluster(sample_entries())) == [[1, 2, 3]]


def microsoft_365_pair():
    ad = make_entry(21, 2, 'Anzeige: Microsoft 365 Security im Unternehmen absichern',
                    'Der Workshop zeigt, wie Administratoren Daten in Microsoft 365 '
                    'absichern, wo Microsoft die Daten speichert und welche Einstellungen '
                    'für Compliance und Sicherheit nötig sind. Jetzt Platz im Workshop '
                    'sichern, Frühbucherrabatt bis Ende des Monats.')
    ad['feed'] = {'id': 2, 'title': 'Golem', 'site_url': 'https://www.golem.de'}
    heise = make_entry(22, 4, 'Speicherort von Microsoft-365-Daten wird wählbar',
                       'Microsoft lässt Unternehmen wählen, wo die Daten von Microsoft 365 '
                       'gespeichert werden. Das soll bei Compliance helfen.')
    # Two documents alone leave no vocabulary (min_df, max_df)
    return [ad, heise] + sample_entries()[3:5]


def test_ad_never_heads_a_story(config, store):
    entries = microsoft_365_pair()
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    assert clusterer._select_canonical(entries, [0, 1]) == 1

    cluster = next(c for c in clusterer._cluster([dict(e) for e in entries])
                   if 21 in c.entry_ids)
    assert sorted(cluster.entry_ids) == [21, 22]
    assert cluster.headline_entry_id == 22
    assert cluster.noise_ids == {21}


def test_noise_patterns_are_configurable(config, store):
    config.clustering.noise_title_patterns = []
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    # Without patterns the longer ad wins again
    assert clusterer._select_canonical(microsoft_365_pair(), [0, 1]) == 0


def test_untitled_entry_counts_as_noise(config, store):
    entries = sample_entries()
    entries.append(make_entry(9, 3, '', BUDGET * 3))
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    cluster = next(c for c in clusterer._cluster(entries) if 9 in c.entry_ids)
    assert cluster.noise_ids == {9}


def test_headline_stays_when_a_longer_article_joins(config, store):
    entries = sample_entries()
    client = FakeClient(entries)
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    first = headline_of(store, 3)

    # The longest article of the story, not a duplicate of any other
    client.entries.append(make_entry(
        7, 4, 'Haushalt 2027: Was der Beschluss des Bundestags bedeutet',
        'Der Bundestag hat den Haushalt beschlossen. Die Schuldenbremse bleibt, die '
        'Opposition kritisiert Kürzungen. ' + 'Was ändert sich für Familien, Pendler, '
        'Studierende und Rentner? Ein Überblick über Bildung, Infrastruktur, Verkehr, '
        'Gesundheit, Verteidigung und Klimaschutz im neuen Bundeshaushalt. ' * 3))
    [cluster] = [c for c in clusterer._cluster([dict(e) for e in client.entries])
                 if 7 in c.entry_ids]
    assert cluster.headline_entry_id == 7  # what the cluster alone would pick
    assert first in cluster.entry_ids and first != 7

    clusterer.run_clustering_cycle()
    assert headline_of(store, 7) == first


@pytest.mark.parametrize('strategy', ['newest', 'source_priority'])
def test_headline_follows_newest_and_source_priority(config, store, strategy):
    # Only 'longest' keeps the headline: with these strategies the user
    # asked for the newest report or the best source on top
    config.deduplication.canonical_strategy = strategy
    config.deduplication.duplicate_action = 'none'
    config.deduplication.source_scores = {'zeit.de': 95, 'spiegel.de': 90, 'heise.de': 80}
    entries = [make_entry(1, 4, 'Bundestag beschließt Haushalt', BUDGET, hours_ago=5),
               make_entry(2, 2, 'Haushalt: Bundestag stimmt zu',
                          'Nach langer Debatte stimmt der Bundestag dem Haushalt zu. Die '
                          'Schuldenbremse bleibt, die Opposition kritisiert die Kürzungen '
                          'bei der Bildung.', hours_ago=4)] + sample_entries()[3:5]
    client = FakeClient(entries)
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    assert headline_of(store, 1) == 2

    # Newer and from the best source
    client.entries.append(make_entry(
        7, 3, 'Haushalt 2027 beschlossen: Opposition kritisiert Kürzungen',
        'Der Bundestag hat den Haushalt beschlossen. Die Opposition kritisiert Kürzungen '
        'bei Bildung und Infrastruktur, die Schuldenbremse bleibt.', hours_ago=0.2))
    clusterer.run_clustering_cycle()
    assert headline_of(store, 1) == 7


def test_noise_headline_is_replaced_when_a_real_article_joins(config, store):
    podcast = make_entry(21, 1, 'Podcast: Der Bundestag und der Haushalt',
                         'Der Bundestag beschließt den Haushalt. Die Opposition kritisiert '
                         'Kürzungen bei Bildung und Infrastruktur, die Schuldenbremse bleibt.')
    liveblog = make_entry(22, 2, 'Liveblog: Haushaltsdebatte im Bundestag',
                          'Der Bundestag debattiert den Haushalt. Kürzungen bei Bildung und '
                          'Infrastruktur, die Opposition kritisiert die Schuldenbremse.')
    client = FakeClient([podcast, liveblog] + sample_entries()[3:5])
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    assert headline_of(store, 21) in (21, 22)

    client.entries.append(make_entry(23, 3, 'Bundestag beschließt Haushalt',
                                     'Der Bundestag hat den Haushalt beschlossen. Die '
                                     'Opposition kritisiert Kürzungen bei Bildung.'))
    clusterer.run_clustering_cycle()
    assert headline_of(store, 21) == 23


def test_headline_that_became_a_duplicate_is_replaced(config, store):
    config.deduplication.threshold = 0.85
    entries = sample_entries()
    entries[1]['feed_id'] = entries[0]['feed_id'] = 1  # no duplicate yet
    entries[1]['feed'] = entries[0]['feed']
    client = FakeClient(entries)
    clusterer = NewsClusterer(config, store, client=client)
    clusterer.run_clustering_cycle()
    first = headline_of(store, 3)
    assert first == 1

    # An agency copy of the headline's text with one sentence more
    client.entries.append(make_entry(7, 4, 'Haushalt beschlossen',
                                     BUDGET + ' Der Bundesrat berät am Freitag.'))
    clusterer.run_clustering_cycle()
    marked = {i for ids, _ in client.marked for i in ids}
    assert first in marked
    assert headline_of(store, 7) == 7


def test_untitled_ticker_is_left_out(config, store):
    entries = sample_entries()
    ticker = make_entry(9, 3, '', '+++ Bundestag beschließt Haushalt, Opposition kritisiert '
                                  'Kürzungen +++ Neuer Prozessor für Notebooks vorgestellt '
                                  '+++ Sonne am Wochenende +++')
    clusterer = NewsClusterer(config, store, client=FakeClient([]))
    found = clusterer._cluster([dict(e) for e in entries + [ticker]])
    assert stories(found) == [[1, 2, 3], [4, 5]]

    # With a title it is an article like any other
    ticker['title'] = 'Nachrichten am Morgen'
    found = clusterer._cluster([dict(e) for e in entries + [ticker]])
    assert 9 in {i for c in found for i in c.entry_ids}


def weather_story():
    return [make_entry(11, 1, 'Wetter: Sonne am Wochenende',
                       'Meteorologen erwarten sommerliche Temperaturen und viel Sonnenschein '
                       'am Wochenende, im Süden bis 30 Grad.'),
            make_entry(12, 2, 'Wetter - Sonnig und warm am Wochenende',
                       'Am Wochenende viel Sonnenschein und sommerliche Temperaturen, '
                       'im Süden bis 30 Grad.')]


def test_excluded_stories_are_not_listed(config, store):
    config.deduplication.duplicate_action = 'none'
    NewsClusterer(config, store, client=FakeClient(sample_entries()[:5] + weather_story())) \
        .run_clustering_cycle()
    listed = [sorted(a['id'] for a in s['articles']) for s in store.top_stories(24, 50)]
    assert [11, 12] in listed

    listed = [sorted(a['id'] for a in s['articles'])
              for s in store.top_stories(24, 50, exclude_patterns=[r'^wetter\b'])]
    assert [11, 12] not in listed
    assert [1, 2, 3] in listed


def test_story_with_one_excluded_title_stays(config, store):
    entries = sample_entries()[:5] + weather_story()
    entries[-1]['title'] = 'Sonnig und warm am Wochenende'
    NewsClusterer(config, store, client=FakeClient(entries)).run_clustering_cycle()
    listed = [sorted(a['id'] for a in s['articles'])
              for s in store.top_stories(24, 50, exclude_patterns=[r'^Wetter\b'])]
    assert [11, 12] in listed


def test_front_page_hides_weather_by_default(config, store):
    from web import create_app
    NewsClusterer(config, store, client=FakeClient(sample_entries()[:5] + weather_story())) \
        .run_clustering_cycle()
    http = create_app(config, store).test_client()
    titles = {s['headline']['title'] for s in http.get('/api/stories').get_json()}
    assert titles and not any(t.startswith('Wetter') for t in titles)
    assert 'Sonne am Wochenende' not in http.get('/').get_data(as_text=True)

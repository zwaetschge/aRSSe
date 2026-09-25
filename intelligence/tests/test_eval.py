"""The gold regression harness (eval/, evaluate.py --gold) on a synthetic fixture."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeClient, sample_entries
from eval.export_corpus import export
from eval.metrics import CANNOT_LINK, MUST_LINK, NEUTRAL, Gold, Label, load_gold, \
    pairwise_scores, story_quality
from eval.replay import ReplayResult, _compare, replay
from evaluate import score
from news_clustering import NewsClusterer

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / 'fixtures'


@pytest.fixture
def corpus():
    with open(FIXTURES / 'eval_corpus.json', encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture
def gold():
    return load_gold(str(FIXTURES / 'eval_gold.json'))


def test_gold_relations(gold):
    assert gold.relation('https://example.org/tagesschau/1',
                         'https://example.org/zeit/2') == MUST_LINK
    # Same neutral topic, different events
    assert gold.relation('https://example.org/tagesschau/1',
                         'https://example.org/spiegel/11') == NEUTRAL
    assert gold.relation('https://example.org/tagesschau/9',
                         'https://example.org/heise/10') == CANNOT_LINK
    # One item listed twice is the same article
    assert gold.relation('https://example.org/tagesschau/9',
                         'https://example.org/tagesschau/9') == MUST_LINK
    assert gold.relation('https://example.org/tagesschau/9', 'https://example.org/x') is None
    assert gold.window_hours == 24 and gold.window_end.isoformat() == '2026-09-24T12:00:00+00:00'


def test_pairwise_scores_and_story_quality():
    gold = Gold(labels={'a': Label('E'), 'b': Label('E'), 'c': Label('E'),
                        'd': Label(None, frozenset({'T'})), 'e': Label(None, frozenset({'T'})),
                        'f': Label()})
    url_of = {1: 'a', 2: 'b', 3: 'c', 4: 'd', 5: 'e', 6: 'f', 7: 'unlabelled'}
    feed_of = {1: 1, 2: 2, 3: 3, 4: 1, 5: 2, 6: 3, 7: 1}
    groups = [[1, 2, 6], [4, 5], [3, 7]]

    row = pairwise_scores(gold, groups, url_of)
    # Pairs: 1-2 must, 1-6 and 2-6 cannot, 4-5 neutral, 3-7 not labelled
    assert (row['tp'], row['fp'], row['must_link']) == (1, 2, 3)
    assert row['precision'] == pytest.approx(1 / 3)
    assert row['recall'] == pytest.approx(1 / 3)
    assert row['labelled'] == 6

    quality = story_quality(gold, groups, url_of, feed_of)
    # 3-7 has no labelled pair: it says nothing about purity
    assert {k: quality[k] for k in ('shown', 'pure', 'mixed', 'junk', 'unlabelled')} == \
        {'shown': 3, 'pure': 1, 'mixed': 1, 'junk': 0, 'unlabelled': 1}
    quality = story_quality(gold, [[4, 6], [1, 2]], url_of, feed_of)
    assert quality['junk_groups'] == [[4, 6]]


def test_fixture_scores_perfectly_with_the_defaults(config, corpus, gold):
    clusterer = NewsClusterer(config, None, client=object())
    row = score(clusterer, corpus, gold)
    assert (row['precision'], row['recall']) == (1.0, 1.0)
    assert (row['shown'], row['junk'], row['noise_headlines']) == (5, 0, 0)

    # Without the floor for two-article stories the 'Razzia' pair is junk
    config.clustering.min_pair_similarity = 0
    row = score(clusterer, corpus, gold)
    assert (row['tp'], row['fp'], row['junk']) == (5, 1, 1)
    assert row['precision'] == pytest.approx(5 / 6)


def test_replay_measures_stability(config, corpus, gold, tmp_path):
    clusterer = NewsClusterer(config, None, client=object())
    # The last hour: four stories stay, the strike becomes the fifth (4 + 5 shown)
    result = replay(clusterer, corpus, gold.window_end, runs=3, step_minutes=30,
                    db_dir=str(tmp_path))
    assert (result.runs, result.transitions, result.previous_shown) == (3, 2, 9)
    assert result.rate('id_kept') == 1.0
    assert result.headline_changed == 0 and result.vanished == 0
    assert result.top10_overlap == 9
    assert len(result.final_page) == 5
    assert all(sources == 2 for _, _, sources in result.final_page)


def test_replay_counts_changes_between_front_pages():
    result = ReplayResult()
    before = [('a', 1, {1, 2}), ('b', 3, {3, 4}), ('c', 5, {5, 6}), ('d', 7, {7, 8})]
    after = [('a', 2, {1, 2}), ('b', 3, {3, 4, 9}), ('e', 6, {6, 10})]
    _compare(result, before, after)
    assert (result.previous_shown, result.id_kept, result.headline_changed) == (4, 2, 1)
    # 'c' lives on under another ID, 'd' is gone
    assert result.vanished == 1
    assert result.top10_overlap == 2
    assert result.rate('headline_changed') == 0.5 and result.rate('id_kept') == 0.5


def test_export_keeps_what_evaluation_needs(config):
    entries = sample_entries()
    for e in entries:
        e['created_at'] = e['published_at']
        e['user_id'] = 7  # not needed, not exported
    clusterer = NewsClusterer(config, None, client=FakeClient(entries))
    exported = export(clusterer)
    assert [e['id'] for e in exported] == [1, 2, 3, 4, 5, 6]
    assert set(exported[0]) == {'id', 'feed_id', 'status', 'title', 'content', 'url',
                                'published_at', 'created_at', 'feed'}
    assert exported[0]['feed'] == {'id': 1, 'title': 'Tagesschau',
                                   'site_url': 'https://www.tagesschau.de'}


def test_evaluate_command_prints_the_table(tmp_path):
    result = subprocess.run(
        [sys.executable, 'evaluate.py', '--corpus', str(FIXTURES / 'eval_corpus.json'),
         '--gold', str(FIXTURES / 'eval_gold.json'), '--replay', '3',
         '--config', str(tmp_path / 'missing.yaml')],
        cwd=HERE.parent, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert '12 articles from 4 feeds' in result.stdout
    assert '1.000 1.000 1.000 |     5     5     0     0' in result.stdout
    assert 'id_kept' in result.stdout


def test_evaluate_without_gold_prints_the_stories(tmp_path):
    # No labels, no scores: an all-zero P/R table would read as a failure
    result = subprocess.run(
        [sys.executable, 'evaluate.py', '--corpus', str(FIXTURES / 'eval_corpus.json'),
         '--replay', '2', '--config', str(tmp_path / 'missing.yaml')],
        cwd=HERE.parent, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert '12 articles from 4 feeds' in result.stdout and 'labelled' not in result.stdout
    assert '5 stories (>= 2 sources), 10/12 articles in stories' in result.stdout
    assert 'Bundestag beschließt Haushalt für 2027' in result.stdout
    assert ' P ' not in result.stdout and 'pure' not in result.stdout
    assert 'id_kept' in result.stdout


def test_evaluate_reports_stories_without_labels(tmp_path):
    with open(FIXTURES / 'eval_gold.json', encoding='utf-8') as f:
        data = json.load(f)
    # The strike story (Zeit and Heise) loses its labels
    data['articles'] = {url: label for url, label in data['articles'].items()
                        if (label or {}).get('event') != 'STREIK'}
    partial = tmp_path / 'gold.json'
    partial.write_text(json.dumps(data))
    result = subprocess.run(
        [sys.executable, 'evaluate.py', '--corpus', str(FIXTURES / 'eval_corpus.json'),
         '--gold', str(partial), '--config', str(tmp_path / 'missing.yaml')],
        cwd=HERE.parent, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert '|     5     4     0     0 |' in result.stdout
    assert 'Up to 1 shown stories have no labelled pair' in result.stdout


def test_evaluate_refuses_a_gold_file_for_another_corpus(tmp_path):
    other = tmp_path / 'gold.json'
    other.write_text(json.dumps({'articles': {'https://example.org/else': {}}}))
    result = subprocess.run(
        [sys.executable, 'evaluate.py', '--corpus', str(FIXTURES / 'eval_corpus.json'),
         '--gold', str(other)],
        cwd=HERE.parent, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert 'No article of the corpus is labelled' in result.stderr


def test_export_corpus_reads_the_user_config(tmp_path, monkeypatch):
    # Same window as the service, which reads DATA_PATH/intelligence/config.yaml
    from eval import export_corpus
    user = tmp_path / 'user.yaml'
    user.write_text('scheduling:\n  lookback_hours: 36\n', encoding='utf-8')
    seen = {}

    def fake_export(clusterer):
        seen['hours'] = clusterer.config.scheduling.lookback_hours
        return []

    monkeypatch.setattr(export_corpus, 'export', fake_export)
    monkeypatch.delenv('LOOKBACK_HOURS', raising=False)
    monkeypatch.setenv('MINIFLUX_API_KEY', 'test-key')
    monkeypatch.setattr(sys, 'argv', ['export_corpus.py', '--out', str(tmp_path / 'c.json'),
                                      '--config', str(HERE.parent / 'config.yaml'),
                                      '--user-config', str(user)])
    export_corpus.main()
    assert seen['hours'] == 36

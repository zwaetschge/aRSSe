"""Operations: config layering, visible errors, health states, logs, build pins."""

import hashlib
import io
import logging.handlers
import os
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import miniflux
import pytest
import requests
import yaml

import config as config_module
import fetch_nltk_data
import news_clustering
from config import ConfigError, changed_settings, ensure_user_config, load_config
from conftest import FakeClient, sample_entries
from news_clustering import (MissingApiKeyError, NewsClusterer, describe_error, record_error,
                             run_scheduler)
from test_scheduler import FakeClusterer, StopAfter
from web import create_app

HERE = Path(__file__).resolve().parent.parent
SHIPPED = str(HERE / 'config.yaml')
LAYER_ENV = ('WEB_MIN_SOURCES', 'WEB_MAX_STORIES', 'LOOKBACK_HOURS', 'RETENTION_DAYS',
             'CLUSTERING_THRESHOLD', 'DEDUP_THRESHOLD', 'WEB_AUTH_MODE', 'WEB_USERNAME',
             'WEB_PASSWORD')


@pytest.fixture
def clean_env(monkeypatch):
    for name in LAYER_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def write(path, text):
    path.write_text(text, encoding='utf-8')
    return str(path)


# --- Config layering ---------------------------------------------------------

def test_user_config_overrides_shipped_file_and_env_overrides_both(tmp_path, clean_env):
    user = write(tmp_path / 'config.yaml', 'web:\n  min_sources: 3\n')
    assert load_config(SHIPPED, user).web.min_sources == 3

    clean_env.setenv('WEB_MIN_SOURCES', '4')
    assert load_config(SHIPPED, user).web.min_sources == 4


def test_missing_user_config_is_fine(tmp_path, clean_env):
    cfg = load_config(SHIPPED, str(tmp_path / 'missing' / 'config.yaml'))
    assert cfg.web.min_sources == 2


def test_user_config_merges_per_setting(tmp_path, clean_env):
    base = write(tmp_path / 'base.yaml',
                 'web:\n  max_stories: 50\n  min_sources: 2\n'
                 '  auth:\n    mode: basic\n    username: leser\n    password: alt\n'
                 'miniflux:\n  url: "http://mf:8080"\n')
    user = write(tmp_path / 'user.yaml',
                 'web:\n  min_sources: 3\n  auth:\n    password: neu\n'
                 'miniflux:\n  public_url: "https://news.example.com"\n')
    cfg = load_config(base, user)
    assert (cfg.web.max_stories, cfg.web.min_sources) == (50, 3)
    assert (cfg.web.auth.mode, cfg.web.auth.username, cfg.web.auth.password) == \
        ('basic', 'leser', 'neu')
    assert cfg.miniflux_url == 'http://mf:8080'
    assert cfg.miniflux_public_url == 'https://news.example.com'


def test_user_config_is_validated_with_its_path(tmp_path, clean_env):
    user = write(tmp_path / 'user.yaml', '- web\n- min_sources\n')
    with pytest.raises(ConfigError, match='user.yaml must contain sections'):
        load_config(SHIPPED, user)
    user = write(tmp_path / 'user.yaml', 'web:\n  min_sources: [3\n')
    with pytest.raises(ConfigError, match='user.yaml is not valid YAML'):
        load_config(SHIPPED, user)
    user = write(tmp_path / 'user.yaml', 'web:\n  min_sources: 0\n')
    with pytest.raises(ConfigError, match='web.min_sources'):
        load_config(SHIPPED, user)


def test_env_overrides_for_template_users(clean_env):
    clean_env.setenv('LOOKBACK_HOURS', '36')
    clean_env.setenv('RETENTION_DAYS', '3')
    clean_env.setenv('WEB_MAX_STORIES', '40')
    cfg = load_config(SHIPPED)
    assert cfg.scheduling.lookback_hours == 36
    assert cfg.storage.retention_days == 3
    assert cfg.web.max_stories == 40

    clean_env.setenv('WEB_MIN_SOURCES', 'drei')
    with pytest.raises(ConfigError, match='WEB_MIN_SOURCES must be a number'):
        load_config(SHIPPED)


def test_shipped_config_equals_the_defaults(clean_env):
    # The baked file is a reference: an edited copy is detected by comparing
    # it with the defaults (see test_edited_reference_file_is_reported)
    cfg = config_module.Config()
    config_module._apply_yaml_config(cfg, config_module._read_yaml(SHIPPED))
    assert changed_settings(cfg) == []


def test_edited_reference_file_is_reported(tmp_path, clean_env, caplog):
    edited = (HERE / 'config.yaml').read_text(encoding='utf-8').replace(
        'min_sources: 2', 'min_sources: 3').replace('interval_minutes: 30',
                                                    'interval_minutes: 15')
    base = write(tmp_path / 'config.yaml', edited)
    user = str(tmp_path / 'data' / 'config.yaml')

    with caplog.at_level('WARNING', logger='arsse-intelligence'):
        cfg = load_config(base, user)
    assert cfg.web.min_sources == 3  # still applies
    assert 'scheduling.interval_minutes, web.min_sources' in caplog.text
    assert user in caplog.text

    caplog.clear()
    with caplog.at_level('WARNING', logger='arsse-intelligence'):
        load_config(SHIPPED, user)
        load_config(base)  # evaluate.py and tests: no layering, no hint
    assert 'differs from the shipped defaults' not in caplog.text


def edited_checkout(tmp_path, *replacements):
    """./intelligence/config.yaml after 'git stash pop' kept an old edit."""
    text = (HERE / 'config.yaml').read_text(encoding='utf-8')
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new)
    (tmp_path / 'legacy').mkdir(exist_ok=True)
    return write(tmp_path / 'legacy' / 'config.yaml', text)


def test_old_checkout_edits_still_apply_with_a_migration_hint(tmp_path, clean_env, caplog):
    # The published image carries the pristine reference; the user's edits
    # live only in the checkout, which compose mounts at /app/legacy
    legacy = edited_checkout(tmp_path, ('duplicate_action: "mark_read"',
                                        'duplicate_action: "none"'),
                             ('threshold: 0.75', 'threshold: 0.7'))
    user = str(tmp_path / 'data' / 'config.yaml')

    with caplog.at_level('WARNING', logger='arsse-intelligence'):
        cfg = load_config(SHIPPED, user, legacy)
    assert cfg.deduplication.duplicate_action == 'none'
    assert cfg.clustering.threshold == 0.7
    warning = caplog.text
    assert 'clustering.threshold, deduplication.duplicate_action' in warning
    assert user in warning and 'git checkout -- intelligence/config.yaml' in warning

    # The user file wins; only what it leaves out is still taken from the checkout
    (tmp_path / 'data').mkdir()
    write(tmp_path / 'data' / 'config.yaml', 'clustering:\n  threshold: 0.8\n')
    caplog.clear()
    with caplog.at_level('WARNING', logger='arsse-intelligence'):
        cfg = load_config(SHIPPED, user, legacy)
    assert (cfg.clustering.threshold, cfg.deduplication.duplicate_action) == (0.8, 'none')
    assert 'differs from the shipped reference: deduplication.duplicate_action.' in caplog.text

    # Moved completely: no warning any more, and the environment still wins
    write(tmp_path / 'data' / 'config.yaml',
          'clustering:\n  threshold: 0.7\ndeduplication:\n  duplicate_action: "none"\n')
    clean_env.setenv('CLUSTERING_THRESHOLD', '0.65')
    caplog.clear()
    with caplog.at_level('WARNING', logger='arsse-intelligence'):
        cfg = load_config(SHIPPED, user, legacy)
    assert cfg.clustering.threshold == 0.65
    assert caplog.text == ''


def test_unchanged_missing_or_unreadable_checkout_file_is_ignored(tmp_path, clean_env, caplog):
    user = str(tmp_path / 'data' / 'config.yaml')
    pristine = edited_checkout(tmp_path)
    with caplog.at_level('INFO', logger='arsse-intelligence'):
        assert changed_settings(load_config(SHIPPED, user, pristine)) == []
        load_config(SHIPPED, user, str(tmp_path / 'missing' / 'config.yaml'))
        # Compose Manager without a checkout: Docker creates an empty directory
        load_config(SHIPPED, user, str(tmp_path / 'data'))
    assert 'legacy' not in caplog.text

    broken = write(tmp_path / 'legacy' / 'config.yaml', 'web:\n  min_sources: [3\n')
    with caplog.at_level('WARNING', logger='arsse-intelligence'):
        cfg = load_config(SHIPPED, user, broken)
    assert cfg.web.min_sources == 2
    assert 'Ignoring the old settings file' in caplog.text


def test_compose_mounts_the_checkout_for_the_migration_hint():
    compose = yaml.safe_load((HERE.parent / 'docker-compose.yml').read_text(encoding='utf-8'))
    volumes = compose['services']['intelligence']['volumes']
    legacy = os.path.dirname(config_module.LEGACY_CONFIG_PATH)
    assert f'./intelligence:{legacy}:ro' in volumes
    assert news_clustering.LEGACY_CONFIG_PATH == config_module.LEGACY_CONFIG_PATH


def test_user_config_stub_is_created_once(tmp_path, clean_env):
    path = tmp_path / 'config.yaml'
    assert ensure_user_config(str(path))
    assert path.read_text(encoding='utf-8') == \
        (HERE / 'config.stub.yaml').read_text(encoding='utf-8')
    assert oct(path.stat().st_mode & 0o777) == '0o600'
    assert load_config(SHIPPED, str(path)).web.min_sources == 2  # only comments

    path.write_text('web:\n  min_sources: 5\n', encoding='utf-8')
    assert not ensure_user_config(str(path))
    assert load_config(SHIPPED, str(path)).web.min_sources == 5
    # No data volume: nothing is created
    assert not ensure_user_config(str(tmp_path / 'missing' / 'config.yaml'))


def test_stub_examples_are_valid(tmp_path, clean_env):
    lines = (HERE / 'config.stub.yaml').read_text(encoding='utf-8').splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith('# Beispiele'))
    examples = '\n'.join(line[2:] for line in lines[start + 1:] if line.startswith('# '))
    cfg = load_config(SHIPPED, write(tmp_path / 'config.yaml', examples))
    assert cfg.web.min_sources == 3
    assert cfg.deduplication.canonical_strategy == 'source_priority'


# --- Visible errors ----------------------------------------------------------

def http_error(cls, status):
    response = requests.Response()
    response.status_code = status
    return cls(response)


@pytest.mark.parametrize('error, kind, message', [
    (MissingApiKeyError('x'), 'config', 'MINIFLUX_API_KEY fehlt'),
    (http_error(miniflux.AccessUnauthorized, 401), 'auth', 'Miniflux lehnt den API-Key ab'),
    (requests.ConnectionError('refused'), 'connection',
     'Miniflux unter http://miniflux:8080 nicht erreichbar'),
    (http_error(miniflux.ServerError, 500), 'connection',
     'Miniflux unter http://miniflux:8080 nicht erreichbar (HTTP 500)'),
    (sqlite3.OperationalError('attempt to write a readonly database'), 'database',
     'Datenbank nicht beschreibbar'),
    (KeyError('secret detail'), 'internal',
     'Clustering fehlgeschlagen (KeyError, Details im Log)'),
])
def test_errors_are_described_in_german(config, error, kind, message):
    assert describe_error(error, config) == (kind, message)


def test_error_message_hides_url_credentials(config):
    config.miniflux_url = 'http://user:geheim@miniflux:8080'
    _, message = describe_error(requests.ConnectionError(), config)
    assert 'geheim' not in message and 'miniflux:8080' in message


def test_rejected_api_key_is_shown_on_healthz_and_front_page(config, store):
    client = FakeClient(sample_entries())
    client.error = http_error(miniflux.AccessUnauthorized, 401)
    started = datetime.now(timezone.utc)
    NewsClusterer(config, store, client=client).run_clustering_cycle()
    http = create_app(config, store, started=started).test_client()

    response = http.get('/healthz')
    body = response.get_json()
    assert response.status_code == 503
    assert body['status'] == 'stale'
    assert body['last_error']['message'] == 'Miniflux lehnt den API-Key ab'
    assert body['last_error']['kind'] == 'auth'
    html = http.get('/').get_data(as_text=True)
    assert 'Fehler seit <time' in html
    assert 'Miniflux lehnt den API-Key ab' in html

    # The next successful run clears it
    client.error = None
    NewsClusterer(config, store, client=client).run_clustering_cycle()
    assert store.get_meta('last_error') is None
    assert http.get('/healthz').get_json()['status'] == 'ok'
    assert 'Fehler seit' not in http.get('/').get_data(as_text=True)


def test_error_keeps_its_start_time_while_it_repeats(config, store):
    record_error(store, config, requests.ConnectionError())
    first = store.get_meta('last_error')
    record_error(store, config, requests.ConnectionError())
    assert store.get_meta('last_error')['at'] == first['at']
    record_error(store, config, MissingApiKeyError())
    assert store.get_meta('last_error')['message'] == 'MINIFLUX_API_KEY fehlt'


def test_missing_api_key_is_stored_by_the_scheduler(config, store):
    config.miniflux_api_key = ''
    # main() takes the start time before the scheduler: a first run that
    # fails before the web server is up still ends 'starting'
    started = datetime.now(timezone.utc)
    run_scheduler(config, store, StopAfter(1),
                  lambda: NewsClusterer(config, store))
    assert store.get_meta('last_error')['message'] == 'MINIFLUX_API_KEY fehlt'
    http = create_app(config, store, started=started).test_client()
    assert http.get('/healthz').status_code == 503


def test_healthz_starting_ok_and_stale(config, store):
    http = create_app(config, store).test_client()
    body = http.get('/healthz').get_json()
    assert body['status'] == 'starting'
    assert body['last_error'] is None

    # A success from before this start that is too old: still starting
    store.set_meta('last_success', '2020-01-01T00:00:00+00:00')
    assert http.get('/healthz').get_json()['status'] == 'starting'

    # A failed run ends 'starting'
    record_error(store, config, requests.ConnectionError())
    response = http.get('/healthz')
    assert (response.status_code, response.get_json()['status']) == (503, 'stale')


def test_healthz_ignores_an_error_from_before_the_restart(config, store):
    # E.g. a wrong API key, fixed by the user, then the container restarted
    record_error(store, config, MissingApiKeyError())
    error = store.get_meta('last_error')
    error.update(at='2020-01-01T00:00:00+00:00', last='2020-01-01T00:05:00+00:00')
    store.set_meta('last_error', error)
    http = create_app(config, store).test_client()
    response = http.get('/healthz')
    assert (response.status_code, response.get_json()['status']) == (200, 'starting')
    # The error is still reported until the next run
    assert response.get_json()['last_error']['message'] == 'MINIFLUX_API_KEY fehlt'

    # The same error again after the start: 'at' stays, 'last' moves on
    record_error(store, config, MissingApiKeyError())
    assert store.get_meta('last_error')['at'] == '2020-01-01T00:00:00+00:00'
    response = http.get('/healthz')
    assert (response.status_code, response.get_json()['status']) == (503, 'stale')


def test_retry_backoff_is_capped_at_five_minutes(config, store):
    config.scheduling.interval_minutes = 60
    stop = StopAfter(8)
    run_scheduler(config, store, stop, lambda: FakeClusterer([1] * 7 + [0]))
    assert stop.waits == [10, 20, 40, 80, 160, 300, 300, 3600]


def test_retry_backoff_never_exceeds_a_short_interval(config, store):
    config.scheduling.interval_minutes = 1
    stop = StopAfter(4)
    run_scheduler(config, store, stop, lambda: FakeClusterer([1, 1, 1, 1]))
    assert stop.waits == [10, 20, 40, 60]


# --- Limits ------------------------------------------------------------------

def parse_bytes(value):
    number, unit = re.fullmatch(r'(\d+)([kmg]?)b?', str(value).lower()).groups()
    return int(number) * 1024 ** ' kmg'.index(unit or ' ')


def test_memory_limit_fits_the_largest_allowed_run():
    # Measured peak RSS of one clustering cycle (OPENBLAS_NUM_THREADS=1):
    # ~176 MiB after the imports, plus ~3.2 dense n x n float64 matrices
    # (273 MiB at 2000, 568 at 4000, 788 at 5000 articles). Rounded up for
    # the web server threads; a limit below this OOM-kills every cycle.
    n = config_module.MAX_ENTRIES_LIMIT
    peak = 200 * 1024 ** 2 + 3.5 * n * n * 8
    compose = yaml.safe_load((HERE.parent / 'docker-compose.yml').read_text(encoding='utf-8'))
    assert parse_bytes(compose['services']['intelligence']['mem_limit']) >= peak
    template = (HERE.parent / 'unraid' / 'arsse-intelligence.xml').read_text(encoding='utf-8')
    assert parse_bytes(re.search(r'--memory=(\S+)', template).group(1)) >= peak


# --- Logs --------------------------------------------------------------------

def test_log_file_is_rotated(config, tmp_path):
    config.logging.file = str(tmp_path / 'intelligence.log')
    news_clustering.setup_logging(config)
    try:
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(handlers) == 1
        assert handlers[0].maxBytes == 5 * 1024 * 1024
        assert handlers[0].backupCount == 3
    finally:
        for handler in logging.getLogger().handlers[:]:
            handler.close()
            logging.getLogger().removeHandler(handler)


# --- Build pins --------------------------------------------------------------

def stopwords_zip(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('stopwords/german', 'der\ndie\ndas\n')
    path = tmp_path / 'stopwords.zip'
    path.write_bytes(buffer.getvalue())
    return path


def test_stopwords_download_is_verified(tmp_path):
    archive = stopwords_zip(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    dest = tmp_path / 'nltk_data'

    fetch_nltk_data.fetch(archive.as_uri(), digest, str(dest))
    assert (dest / 'corpora' / 'stopwords' / 'german').read_text() == 'der\ndie\ndas\n'


def test_tampered_stopwords_fail_the_build(tmp_path, monkeypatch, capsys):
    archive = stopwords_zip(tmp_path)
    dest = tmp_path / 'nltk_data'
    with pytest.raises(fetch_nltk_data.ChecksumError):
        fetch_nltk_data.fetch(archive.as_uri(), '0' * 64, str(dest))
    assert not dest.exists()

    # The Dockerfile runs main(): a wrong checksum must end with an error code
    monkeypatch.setattr(fetch_nltk_data, 'STOPWORDS_URL', archive.as_uri())
    assert fetch_nltk_data.main(['--dest', str(dest)]) == 1
    assert 'does not match the pinned' in capsys.readouterr().err


def test_lock_file_pins_every_requirement_with_hashes():
    requirements = (HERE / 'requirements.in').read_text(encoding='utf-8')
    lock = (HERE / 'requirements.txt').read_text(encoding='utf-8')
    names = {line.split('>')[0].split('=')[0].split('<')[0].strip().lower()
             for line in requirements.splitlines() if line and not line.startswith('#')}
    pinned = {line.split('==')[0].lower() for line in lock.splitlines()
              if '==' in line and not line.startswith(' ')}
    assert names <= pinned
    blocks = lock.split('==')[1:]
    assert all('--hash=sha256:' in block for block in blocks)
    assert os.path.basename(fetch_nltk_data.STOPWORDS_URL) == 'stopwords.zip'

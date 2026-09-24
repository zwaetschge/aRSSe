import pytest

from config import ConfigError, load_config


def write_yaml(tmp_path, text):
    path = tmp_path / 'config.yaml'
    path.write_text(text, encoding='utf-8')
    return str(path)


def test_yaml_values_apply(tmp_path, monkeypatch):
    monkeypatch.delenv('CLUSTERING_THRESHOLD', raising=False)
    cfg = load_config(write_yaml(tmp_path, 'clustering:\n  threshold: 0.55\n'))
    assert cfg.clustering.threshold == 0.55


def test_empty_env_does_not_override_yaml(tmp_path, monkeypatch):
    # docker-compose passes unset variables through as empty strings
    monkeypatch.setenv('CLUSTERING_THRESHOLD', '')
    cfg = load_config(write_yaml(tmp_path, 'clustering:\n  threshold: 0.55\n'))
    assert cfg.clustering.threshold == 0.55


def test_env_overrides_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('CLUSTERING_THRESHOLD', '0.3')
    cfg = load_config(write_yaml(tmp_path, 'clustering:\n  threshold: 0.55\n'))
    assert cfg.clustering.threshold == 0.3


def test_legacy_duplicate_action_is_mapped(tmp_path):
    cfg = load_config(write_yaml(tmp_path, 'deduplication:\n  duplicate_action: hide\n'))
    assert cfg.deduplication.duplicate_action == 'mark_read'


def test_invalid_values_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_config(write_yaml(tmp_path, 'deduplication:\n  canonical_strategy: random\n'))
    with pytest.raises(ValueError):
        load_config(write_yaml(tmp_path, 'clustering:\n  threshold: 1.5\n'))


def test_shipped_config_is_valid(monkeypatch):
    from pathlib import Path
    for var in ('CLUSTERING_THRESHOLD', 'DEDUP_THRESHOLD'):
        monkeypatch.delenv(var, raising=False)
    load_config(str(Path(__file__).resolve().parent.parent / 'config.yaml'))


def test_web_port_in_yaml_is_ignored(tmp_path, monkeypatch):
    # compose publishes and health-checks container port 8081 only
    monkeypatch.delenv('WEB_PORT', raising=False)
    cfg = load_config(write_yaml(tmp_path, 'web:\n  port: 9000\n  max_stories: 10\n'))
    assert cfg.web.port == 8081
    assert cfg.web.max_stories == 10


def test_legacy_dbscan_settings_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv('CLUSTERING_EPS', '0.4')
    monkeypatch.delenv('CLUSTERING_THRESHOLD', raising=False)
    cfg = load_config(write_yaml(tmp_path, 'clustering:\n  eps: 0.4\n  min_samples: 3\n'))
    assert cfg.clustering.threshold == 0.75
    assert not hasattr(cfg.clustering, 'eps')


def test_miniflux_public_port(tmp_path, monkeypatch):
    monkeypatch.delenv('MINIFLUX_PORT', raising=False)
    assert load_config(None).miniflux_public_port == 8080
    path = write_yaml(tmp_path, 'miniflux:\n  public_port: 8090\n')
    assert load_config(path).miniflux_public_port == 8090

    monkeypatch.setenv('MINIFLUX_PORT', '18080')
    assert load_config(path).miniflux_public_port == 18080

    # Unusable values only affect link fallbacks: warn, keep config.yaml
    monkeypatch.setenv('MINIFLUX_PORT', '0')
    assert load_config(path).miniflux_public_port == 8090


def test_miniflux_port_with_host_ip_binding(tmp_path, monkeypatch, caplog):
    # Docker port syntax "127.0.0.1:8080" (Miniflux only on localhost behind
    # a reverse proxy) must not stop the service; the port is only a fallback
    monkeypatch.setenv('MINIFLUX_PUBLIC_URL', 'https://news.example.com')
    monkeypatch.setenv('MINIFLUX_PORT', '127.0.0.1:8090')
    assert load_config(None).miniflux_public_port == 8090

    monkeypatch.setenv('MINIFLUX_PORT', '[::1]:8091')
    assert load_config(None).miniflux_public_port == 8091

    monkeypatch.setenv('MINIFLUX_PORT', 'kaputt')
    with caplog.at_level('WARNING'):
        assert load_config(None).miniflux_public_port == 8080
    assert 'MINIFLUX_PORT' in caplog.text

    monkeypatch.delenv('MINIFLUX_PORT')
    path = write_yaml(tmp_path, 'miniflux:\n  public_port: "127.0.0.1:8092"\n')
    assert load_config(path).miniflux_public_port == 8092


@pytest.mark.parametrize('yaml_text, key', [
    ('scheduling:\n  batch_size: 1500\n', 'scheduling.batch_size'),
    ('scheduling:\n  batch_size: 0\n', 'scheduling.batch_size'),
    ('scheduling:\n  max_entries: 6000\n', 'scheduling.max_entries'),
    ('scheduling:\n  lookback_hours: 0\n', 'scheduling.lookback_hours'),
    ('scheduling:\n  batch_size: yes\n', 'scheduling.batch_size'),
    ('storage:\n  retention_days: 0\n', 'storage.retention_days'),
    ('storage:\n  retention_days: -1\n', 'storage.retention_days'),
    ('storage:\n  retention_days: 1\nscheduling:\n  lookback_hours: 48\n',
     'storage.retention_days'),
    ('clustering:\n  max_features: 0\n', 'clustering.max_features'),
    ('clustering:\n  threshold: "0.8"\n', 'clustering.threshold'),
    ('deduplication:\n  threshold: "hoch"\n', 'deduplication.threshold'),
    ('web:\n  articles_per_story: -1\n', 'web.articles_per_story'),
    ('web:\n  max_stories: 0\n', 'web.max_stories'),
    ('web:\n  min_sources: 1.5\n', 'web.min_sources'),
    ('miniflux:\n  url: null\n', 'miniflux.url'),
    ('miniflux:\n  url: ""\n', 'miniflux.url'),
    ('miniflux:\n  public_url: null\n', 'miniflux.public_url'),
    ('storage:\n  db_path: ""\n', 'storage.db_path'),
])
def test_values_that_would_break_silently_are_rejected(tmp_path, monkeypatch, yaml_text, key):
    for var in ('CLUSTERING_THRESHOLD', 'DEDUP_THRESHOLD', 'MINIFLUX_URL',
                'MINIFLUX_PUBLIC_URL'):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError) as error:
        load_config(write_yaml(tmp_path, yaml_text))
    assert key in str(error.value)


def test_limits_themselves_are_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv('CLUSTERING_THRESHOLD', raising=False)
    cfg = load_config(write_yaml(tmp_path, (
        'scheduling:\n  batch_size: 1000\n  max_entries: 5000\n  lookback_hours: 24\n'
        'storage:\n  retention_days: 1\n'
        'clustering:\n  threshold: 0.1\n  max_features: 1\n'
        'web:\n  articles_per_story: 0\n  max_stories: 1\n')))
    assert cfg.scheduling.batch_size == 1000
    assert cfg.clustering.threshold == 0.1


def test_malformed_numbers_in_env_name_the_variable(monkeypatch):
    monkeypatch.setenv('CLUSTERING_THRESHOLD', 'null,8')
    with pytest.raises(ConfigError, match='CLUSTERING_THRESHOLD'):
        load_config(None)
    monkeypatch.delenv('CLUSTERING_THRESHOLD')
    monkeypatch.setenv('CLUSTERING_INTERVAL', '30m')
    with pytest.raises(ConfigError, match='CLUSTERING_INTERVAL'):
        load_config(None)


def test_config_error_is_a_value_error():
    assert issubclass(ConfigError, ValueError)

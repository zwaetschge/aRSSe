import pytest

from config import load_config


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

    monkeypatch.setenv('MINIFLUX_PORT', '0')
    with pytest.raises(ValueError):
        load_config(path)

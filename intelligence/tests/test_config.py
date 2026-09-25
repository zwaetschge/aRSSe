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
    ('web:\n  earlier_articles_max: -1\n', 'web.earlier_articles_max'),
    ('web:\n  earlier_articles_max: "20"\n', 'web.earlier_articles_max'),
    ('miniflux:\n  url: null\n', 'miniflux.url'),
    ('miniflux:\n  url: ""\n', 'miniflux.url'),
    ('miniflux:\n  public_url: null\n', 'miniflux.public_url'),
    ('storage:\n  db_path: ""\n', 'storage.db_path'),
    ('clustering:\n  ngram_max: 0\n', 'clustering.ngram_max'),
    ('clustering:\n  ngram_max: 4\n', 'clustering.ngram_max'),
    ('clustering:\n  ngram_max: 1.5\n', 'clustering.ngram_max'),
    ('clustering:\n  min_pair_similarity: 1.0\n', 'clustering.min_pair_similarity'),
    ('clustering:\n  min_pair_similarity: -0.1\n', 'clustering.min_pair_similarity'),
    ('clustering:\n  min_pair_similarity: "0.3"\n', 'clustering.min_pair_similarity'),
    ("clustering:\n  noise_title_patterns: ['^(Anzeige:']\n", 'clustering.noise_title_patterns'),
    ('clustering:\n  noise_title_patterns: 5\n', 'clustering.noise_title_patterns'),
    ("web:\n  exclude_patterns: ['[Wetter']\n", 'web.exclude_patterns'),
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
        'web:\n  articles_per_story: 0\n  max_stories: 1\n  earlier_articles_max: 0\n')))
    assert cfg.scheduling.batch_size == 1000
    assert cfg.web.earlier_articles_max == 0
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


def test_dedup_body_tokens_and_scope(tmp_path, monkeypatch):
    monkeypatch.delenv('DEDUP_MIN_BODY_TOKENS', raising=False)
    cfg = load_config(None)
    assert cfg.deduplication.min_body_tokens == 25
    assert cfg.deduplication.mark_read_scope == 'visible'

    path = write_yaml(tmp_path, 'deduplication:\n  min_body_tokens: 40\n'
                                '  mark_read_scope: all\n')
    cfg = load_config(path)
    assert cfg.deduplication.min_body_tokens == 40
    assert cfg.deduplication.mark_read_scope == 'all'

    monkeypatch.setenv('DEDUP_MIN_BODY_TOKENS', '10')
    assert load_config(path).deduplication.min_body_tokens == 10
    monkeypatch.setenv('DEDUP_MIN_BODY_TOKENS', 'viele')
    with pytest.raises(ConfigError, match='DEDUP_MIN_BODY_TOKENS'):
        load_config(path)
    monkeypatch.delenv('DEDUP_MIN_BODY_TOKENS')

    for bad in ('min_body_tokens: 0', 'min_body_tokens: "25"', 'mark_read_scope: shown'):
        with pytest.raises(ConfigError, match=bad.split(':')[0]):
            load_config(write_yaml(tmp_path, f'deduplication:\n  {bad}\n'))


WEB_ENV = ('WEB_AUTH_MODE', 'WEB_USERNAME', 'WEB_PASSWORD', 'WEB_PASSWORD_FILE',
           'WEB_AUTH_PROXY_HEADER', 'WEB_TRUSTED_PROXIES', 'WEB_ALLOWED_HOSTS',
           'MINIFLUX_API_KEY', 'MINIFLUX_API_KEY_FILE')


@pytest.fixture
def web_env(monkeypatch):
    for var in WEB_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_web_auth_defaults_to_none(web_env):
    cfg = load_config(None)
    assert cfg.web.auth.mode == 'none'
    assert cfg.web.auth.proxy_header == 'Remote-User'
    assert cfg.web.allowed_hosts == []


def test_web_auth_from_yaml(tmp_path, web_env):
    cfg = load_config(write_yaml(tmp_path, (
        'web:\n  allowed_hosts: [Tower.local, "stories.example.com:443"]\n'
        '  auth:\n    mode: proxy\n    proxy_header: X-Forwarded-User\n'
        '    trusted_proxies: ["172.30.0.10", "fd00::/64"]\n')))
    assert cfg.web.auth.mode == 'proxy'
    assert cfg.web.auth.proxy_header == 'X-Forwarded-User'
    assert cfg.web.auth.trusted_proxies == ['172.30.0.10', 'fd00::/64']
    assert cfg.web.allowed_hosts == ['tower.local', 'stories.example.com']


def test_web_auth_from_env(tmp_path, web_env):
    web_env.setenv('WEB_AUTH_MODE', 'Basic')
    web_env.setenv('WEB_USERNAME', 'leser')
    web_env.setenv('WEB_PASSWORD', 'geheim')
    web_env.setenv('WEB_ALLOWED_HOSTS', 'tower.local, 192.168.1.10')
    web_env.setenv('WEB_TRUSTED_PROXIES', '172.30.0.10/32,172.30.0.11')
    cfg = load_config(None)
    assert (cfg.web.auth.mode, cfg.web.auth.username, cfg.web.auth.password) == \
        ('basic', 'leser', 'geheim')
    assert cfg.web.allowed_hosts == ['tower.local', '192.168.1.10']
    assert cfg.web.auth.trusted_proxies == ['172.30.0.10/32', '172.30.0.11']

    # WEB_PASSWORD beats a password_file from config.yaml
    path = write_yaml(tmp_path, 'web:\n  auth:\n    password_file: /nicht/da\n')
    assert load_config(path).web.auth.password == 'geheim'


def test_web_password_file(tmp_path, web_env):
    secret = tmp_path / 'web_password'
    secret.write_text('aus-datei\n', encoding='utf-8')
    web_env.setenv('WEB_AUTH_MODE', 'basic')
    web_env.setenv('WEB_USERNAME', 'leser')
    web_env.setenv('WEB_PASSWORD', 'aus-env')
    web_env.setenv('WEB_PASSWORD_FILE', str(secret))
    assert load_config(None).web.auth.password == 'aus-datei'

    web_env.setenv('WEB_PASSWORD_FILE', str(tmp_path / 'fehlt'))
    with pytest.raises(ConfigError, match='password_file'):
        load_config(None)
    secret.write_text('  \n', encoding='utf-8')
    web_env.setenv('WEB_PASSWORD_FILE', str(secret))
    with pytest.raises(ConfigError, match='empty'):
        load_config(None)

    # Only read when basic auth needs it
    web_env.setenv('WEB_AUTH_MODE', 'none')
    web_env.setenv('WEB_PASSWORD_FILE', str(tmp_path / 'fehlt'))
    assert load_config(None).web.auth.mode == 'none'


@pytest.mark.parametrize('env, key', [
    ({'WEB_AUTH_MODE': 'basic', 'WEB_USERNAME': 'leser'}, 'WEB_PASSWORD'),
    ({'WEB_AUTH_MODE': 'basic', 'WEB_PASSWORD': 'geheim'}, 'WEB_USERNAME'),
    ({'WEB_AUTH_MODE': 'basic', 'WEB_USERNAME': 'a:b', 'WEB_PASSWORD': 'x'}, 'username'),
    ({'WEB_AUTH_MODE': 'proxy'}, 'WEB_TRUSTED_PROXIES'),
    ({'WEB_AUTH_MODE': 'proxy', 'WEB_TRUSTED_PROXIES': '172.30.0.300'}, 'trusted_proxies'),
    ({'WEB_AUTH_MODE': 'miniflux'}, 'web.auth.mode'),
    ({'WEB_ALLOWED_HOSTS': 'http://tower.local'}, 'web.allowed_hosts'),
    ({'WEB_ALLOWED_HOSTS': 'tower.local:x'}, 'web.allowed_hosts'),
])
def test_web_auth_that_cannot_work_is_rejected(web_env, env, key):
    for name, value in env.items():
        web_env.setenv(name, value)
    with pytest.raises(ConfigError, match=key):
        load_config(None)


def test_web_auth_yaml_types(tmp_path, web_env):
    with pytest.raises(ConfigError, match='web.auth'):
        load_config(write_yaml(tmp_path, 'web:\n  auth: basic\n'))
    with pytest.raises(ConfigError, match='web.allowed_hosts'):
        load_config(write_yaml(tmp_path, 'web:\n  allowed_hosts: 5\n'))
    with pytest.raises(ConfigError, match='web.auth.username'):
        load_config(write_yaml(tmp_path, 'web:\n  auth:\n    mode: basic\n'
                                         '    username: 1234\n    password: x\n'))


def test_miniflux_api_key_file(tmp_path, web_env):
    secret = tmp_path / 'api_key'
    secret.write_text('schluessel-aus-datei\n', encoding='utf-8')
    web_env.setenv('MINIFLUX_API_KEY', 'aus-env')
    web_env.setenv('MINIFLUX_API_KEY_FILE', str(secret))
    assert load_config(None).miniflux_api_key == 'schluessel-aus-datei'

    web_env.setenv('MINIFLUX_API_KEY_FILE', str(tmp_path / 'fehlt'))
    with pytest.raises(ConfigError, match='MINIFLUX_API_KEY_FILE'):
        load_config(None)


def test_allowed_hosts_ipv6_and_idn(web_env):
    # Browsers send IDN names as punycode and IPv6 hosts in brackets
    web_env.setenv('WEB_ALLOWED_HOSTS', 'Büro.local, fd00::10, [FD00:0::11]:8081')
    assert load_config(None).web.allowed_hosts == ['xn--bro-hoa.local', 'fd00::10',
                                                   'fd00::11']


@pytest.mark.parametrize('hosts', ['*', '*.example.com', '.example.com', 'tower..local'])
def test_allowed_hosts_wildcards_are_rejected(web_env, hosts):
    # Accepted before, but then no real Host header ever matched
    web_env.setenv('WEB_ALLOWED_HOSTS', hosts)
    with pytest.raises(ConfigError, match='web.allowed_hosts'):
        load_config(None)


@pytest.mark.parametrize('header', ['X_Remote_User', 'Remote User', 'Remote-User:'])
def test_proxy_header_waitress_would_drop_is_rejected(web_env, header):
    # waitress discards request headers whose name contains '_'
    web_env.setenv('WEB_AUTH_MODE', 'proxy')
    web_env.setenv('WEB_TRUSTED_PROXIES', '172.30.0.10')
    web_env.setenv('WEB_AUTH_PROXY_HEADER', header)
    with pytest.raises(ConfigError, match='proxy_header'):
        load_config(None)


def test_trusted_proxies_must_not_cover_everyone(web_env, caplog):
    web_env.setenv('WEB_AUTH_MODE', 'proxy')
    for everyone in ('0.0.0.0/0', '::/0'):
        web_env.setenv('WEB_TRUSTED_PROXIES', f'172.30.0.10,{everyone}')
        with pytest.raises(ConfigError, match='trusted_proxies'):
            load_config(None)
    # Public networks are allowed, but unusual enough for a warning
    web_env.setenv('WEB_TRUSTED_PROXIES', '8.8.8.0/24')
    with caplog.at_level('WARNING'):
        assert load_config(None).web.auth.trusted_proxies == ['8.8.8.0/24']
    assert '8.8.8.0/24' in caplog.text
    caplog.clear()
    web_env.setenv('WEB_TRUSTED_PROXIES', '172.18.0.1/32')
    with caplog.at_level('WARNING'):
        load_config(None)
    assert 'trusted_proxies' not in caplog.text


def test_clustering_quality_defaults(monkeypatch):
    for var in ('CLUSTERING_NGRAM_MAX', 'CLUSTERING_MIN_PAIR_SIMILARITY'):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(None)
    assert cfg.clustering.ngram_max == 1
    assert cfg.clustering.min_pair_similarity == 0.30
    assert r'^(Anzeige|heise-Angebot):' in cfg.clustering.noise_title_patterns
    assert cfg.web.exclude_patterns == [r'^Wetter\b']


def test_shipped_config_lists_the_default_patterns(monkeypatch):
    from pathlib import Path
    from config import DEFAULT_NOISE_TITLE_PATTERNS
    for var in ('CLUSTERING_THRESHOLD', 'DEDUP_THRESHOLD', 'CLUSTERING_NGRAM_MAX',
                'CLUSTERING_MIN_PAIR_SIMILARITY'):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(str(Path(__file__).resolve().parent.parent / 'config.yaml'))
    assert cfg.clustering.noise_title_patterns == DEFAULT_NOISE_TITLE_PATTERNS
    assert cfg.web.exclude_patterns == [r'^Wetter\b']
    assert (cfg.clustering.ngram_max, cfg.clustering.min_pair_similarity) == (1, 0.30)


def test_clustering_quality_env(monkeypatch):
    monkeypatch.setenv('CLUSTERING_NGRAM_MAX', '2')
    monkeypatch.setenv('CLUSTERING_MIN_PAIR_SIMILARITY', '0.35')
    cfg = load_config(None)
    assert cfg.clustering.ngram_max == 2
    assert cfg.clustering.min_pair_similarity == 0.35
    monkeypatch.setenv('CLUSTERING_MIN_PAIR_SIMILARITY', 'hoch')
    with pytest.raises(ConfigError, match='CLUSTERING_MIN_PAIR_SIMILARITY'):
        load_config(None)


def test_pattern_lists(tmp_path):
    # A single string is one pattern, even with commas; an empty list disables
    cfg = load_config(write_yaml(tmp_path, (
        'clustering:\n  noise_title_patterns: "^(Anzeige|Werbung){1,2}:"\n'
        'web:\n  exclude_patterns: []\n')))
    assert cfg.clustering.noise_title_patterns == ['^(Anzeige|Werbung){1,2}:']
    assert cfg.web.exclude_patterns == []

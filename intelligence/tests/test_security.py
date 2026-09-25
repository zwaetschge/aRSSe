"""Access control, DNS rebinding, CSRF and security headers of the web interface."""

import base64
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from config import load_config
from conftest import FakeClient, sample_entries
from news_clustering import NewsClusterer, check_api_key_user
from store import ClusterResult
from web import CONTENT_SECURITY_POLICY, create_app


def basic(user, password):
    token = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def app_for(config, store, run=True):
    if run:
        NewsClusterer(config, store, client=FakeClient(sample_entries())).run_clustering_cycle()
    return create_app(config, store)


@pytest.fixture
def basic_config(config):
    config.web.auth.mode = 'basic'
    config.web.auth.username = 'leser'
    config.web.auth.password = 'geheim'
    return config


@pytest.fixture
def proxy_config(config):
    config.web.auth.mode = 'proxy'
    config.web.auth.trusted_proxies = ['172.30.0.10/32']
    return config


def test_basic_auth(basic_config, store):
    http = app_for(basic_config, store).test_client()
    story_id = store.top_stories(24, 50)[0]['id']

    for path in ('/', f'/story/{story_id}', '/api/stories', '/nicht-da'):
        response = http.get(path)
        assert response.status_code == 401, path
        assert response.headers['WWW-Authenticate'].startswith('Basic realm="aRSSe"')
        assert 'Bundestag' not in response.get_data(as_text=True)

    assert http.get('/', headers=basic('leser', 'geheim')).status_code == 200
    assert http.get('/api/stories', headers=basic('leser', 'geheim')).get_json()
    assert http.get('/', headers=basic('leser', 'falsch')).status_code == 401
    assert http.get('/', headers=basic('admin', 'geheim')).status_code == 401
    assert http.get('/', headers=basic('leser', 'geheim2')).status_code == 401
    assert http.get('/', headers={'Authorization': 'Bearer geheim'}).status_code == 401
    # Docker health check has no credentials
    assert http.get('/healthz').status_code == 200


def test_basic_auth_with_non_ascii_password(basic_config, store):
    basic_config.web.auth.password = 'Grüße'
    http = app_for(basic_config, store, run=False).test_client()
    assert http.get('/', headers=basic('leser', 'Grüße')).status_code == 200
    assert http.get('/', headers=basic('leser', 'Grusse')).status_code == 401


def test_proxy_auth_trusts_header_only_from_proxy(proxy_config, store):
    http = app_for(proxy_config, store).test_client()

    def get(remote_addr, headers=None):
        return http.get('/', headers=headers or {},
                        environ_base={'REMOTE_ADDR': remote_addr}).status_code

    user = {'Remote-User': 'leser'}
    assert get('192.168.1.50', user) == 403
    assert get('172.30.0.10', user) == 200
    assert get('::ffff:172.30.0.10', user) == 200
    assert get('172.30.0.10') == 403
    assert get('172.30.0.10', {'Remote-User': ''}) == 403
    assert http.get('/healthz', environ_base={'REMOTE_ADDR': '127.0.0.1'}).status_code == 200


def test_proxy_auth_custom_header(proxy_config, store):
    proxy_config.web.auth.proxy_header = 'X-Authentik-Username'
    proxy_config.web.auth.trusted_proxies = ['172.30.0.0/24', 'fd00::/64']
    http = app_for(proxy_config, store, run=False).test_client()
    for addr in ('172.30.0.99', 'fd00::5'):
        assert http.get('/', headers={'X-Authentik-Username': 'leser'},
                        environ_base={'REMOTE_ADDR': addr}).status_code == 200
    assert http.get('/', headers={'Remote-User': 'leser'},
                    environ_base={'REMOTE_ADDR': '172.30.0.99'}).status_code == 403


def test_allowed_hosts_block_dns_rebinding(config, store):
    config.web.allowed_hosts = ['tower.local']
    http = app_for(config, store).test_client()

    for host in ('rebind.attacker.example', 'rebind.attacker.example:8081', 'tower.local.evil'):
        assert http.get('/', headers={'Host': host}).status_code == 400, host
        assert http.get('/healthz', headers={'Host': host}).status_code == 400, host
    for host in ('tower.local', 'TOWER.local:8081', 'localhost', 'localhost:8081',
                 '127.0.0.1', '127.0.0.1:8081', '[::1]:8081'):
        assert http.get('/', headers={'Host': host}).status_code == 200, host


def test_allowed_hosts_match_what_browsers_send(monkeypatch, store):
    # IDN names arrive as punycode, IPv6 addresses in brackets (503: no cycle yet)
    for var in ('WEB_AUTH_MODE', 'WEB_TRUSTED_PROXIES', 'WEB_AUTH_PROXY_HEADER'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('WEB_ALLOWED_HOSTS', 'büro.local,fd00::10')
    http = create_app(load_config(None), store).test_client()
    for host in ('xn--bro-hoa.local', 'XN--BRO-HOA.local:8081', '[fd00::10]:8081'):
        assert http.get('/healthz', headers={'Host': host}).status_code != 400, host
    assert http.get('/healthz', headers={'Host': '[fd00::11]'}).status_code == 400


def test_any_host_is_accepted_without_allowed_hosts(config, store):
    http = app_for(config, store, run=False).test_client()
    assert http.get('/', headers={'Host': 'rebind.attacker.example'}).status_code == 200


def test_security_headers_on_every_response(basic_config, store):
    http = app_for(basic_config, store).test_client()
    story_id = store.top_stories(24, 50)[0]['id']
    auth = basic('leser', 'geheim')
    responses = [http.get(path, headers=auth) for path in
                 ('/', f'/story/{story_id}', '/api/stories', '/story/gibtsnicht')]
    responses += [http.get('/healthz'), http.get('/')]  # health check, 401
    for response in responses:
        assert response.headers['Content-Security-Policy'] == CONTENT_SECURITY_POLICY
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.headers['X-Frame-Options'] == 'DENY'
        assert response.headers['Referrer-Policy'] == 'same-origin'
        assert response.headers['Cross-Origin-Resource-Policy'] == 'same-origin'
    assert "script-src" not in CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in CONTENT_SECURITY_POLICY


HOSTILE_URLS = [
    'javascript:alert(1)', 'JaVaScRiPt:alert(1)', ' javascript:alert(1)',
    '\x01javascript:alert(1)', 'java\tscript:alert(1)', 'java\nscript:alert(1)',
    'data:text/html,<script>alert(1)</script>', 'vbscript:msgbox(1)',
    'http://ok.example/"><script>alert(1)</script>', "https://ok.example/' onmouseover='alert(1)",
    ' javascript:alert(1)', 'https:javascript:alert(1)', '//evil.example/',
    '\\\\evil.example', 'http:\\\\evil.example',
]


def test_hostile_feed_content_renders_inert(config, store):
    now = datetime.now(timezone.utc).isoformat()
    entries = [{
        'id': i,
        # Without feed_id the link goes to the article URL itself
        'feed_id': None if i % 2 else 7,
        'feed': {'id': 7 + i % 2, 'title': '<img src=x onerror=alert("ft")>'},
        'title': f'</title><script>alert("t{i}")</script>',
        'url': url,
        'published_at': now,
        'status': 'unread',
        '_snippet': '<svg onload=alert("s")>',
    } for i, url in enumerate(HOSTILE_URLS, start=1)]
    store.save_run(entries, [ClusterResult(entry_ids=[e['id'] for e in entries],
                                           headline_entry_id=1, duplicate_ids=set())])
    config.miniflux_public_url = 'http://mf.example'
    config.web.articles_per_story = len(entries)
    http = create_app(config, store).test_client()
    story_id = http.get('/api/stories').get_json()[0]['id']

    for path in ('/', f'/story/{story_id}'):
        page = http.get(path).get_data(as_text=True)
        for tag in ('<script', '<img', '<svg'):
            assert tag not in page, (path, tag)
        links = [html.unescape(h) for h in re.findall(r'href="([^"]*)"', page)]
        assert len(links) > len(HOSTILE_URLS) // 2
        for link in links:
            # http(s) or a path on this server, never protocol-relative
            if link.startswith('/'):
                assert not link.startswith(('//', '/\\')), link
            else:
                assert urlsplit(link).scheme in ('http', 'https'), link
            # What a browser would execute after dropping controls and whitespace
            squashed = re.sub(r'[\x00-\x20\x7f ]', '', link).lower()
            assert not squashed.startswith(('javascript:', 'data:', 'vbscript:')), link


def csrf_app(config, store):
    app = create_app(config, store)

    @app.post('/test/aktion')
    def action():
        return 'ok'
    return app.test_client()


def test_post_requires_same_origin(config, store):
    http = csrf_app(config, store)
    host = {'Host': 'tower.local:8081'}

    def post(headers):
        return http.post('/test/aktion', headers={**host, **headers}).status_code

    assert post({'Sec-Fetch-Site': 'same-origin'}) == 200
    assert post({'Sec-Fetch-Site': 'none'}) == 200
    assert post({'Sec-Fetch-Site': 'cross-site'}) == 403
    assert post({'Sec-Fetch-Site': 'same-site'}) == 403
    # Cross-site Origin wins over a matching Referer
    assert post({'Sec-Fetch-Site': 'cross-site', 'Origin': 'http://tower.local:8081'}) == 403
    # Older browsers without Sec-Fetch-*: Origin, else Referer must match Host
    assert post({'Origin': 'http://evil.example'}) == 403
    assert post({'Origin': 'http://tower.local:8081'}) == 200
    assert post({'Origin': 'null'}) == 403
    assert post({'Origin': 'http://tower.local:9999'}) == 403
    assert post({'Referer': 'http://tower.local:8081/story/abc'}) == 200
    assert post({'Referer': 'http://evil.example/tower.local:8081'}) == 403
    assert post({}) == 403
    # Behind a TLS proxy the Host has no port and the Origin a default one
    assert http.post('/test/aktion', headers={'Host': 'stories.example.com',
                                              'Origin': 'https://stories.example.com:443'}
                     ).status_code == 200


def test_post_needs_auth_before_origin(basic_config, store):
    http = csrf_app(basic_config, store)
    same = {'Sec-Fetch-Site': 'same-origin'}
    assert http.post('/test/aktion', headers=same).status_code == 401
    assert http.post('/test/aktion', headers={**same, **basic('leser', 'geheim')}
                     ).status_code == 200
    assert http.post('/test/aktion', headers={'Sec-Fetch-Site': 'cross-site',
                                              **basic('leser', 'geheim')}).status_code == 403


def test_startup_warns_without_auth(config, store, caplog):
    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        create_app(config, store)
    assert any('web.auth.mode=none' in r.getMessage() for r in caplog.records)


def test_no_warning_with_auth(basic_config, store, caplog):
    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        create_app(basic_config, store)
    assert not any('web.auth.mode' in r.getMessage() for r in caplog.records)


def test_admin_api_key_is_reported(caplog):
    client = FakeClient([])
    client.user = {'id': 1, 'username': 'admin', 'is_admin': True}
    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        assert check_api_key_user(client) is True
    assert any("admin user 'admin'" in r.getMessage() for r in caplog.records)

    caplog.clear()
    client.user = {'id': 2, 'username': 'leser', 'is_admin': False}
    with caplog.at_level(logging.WARNING, logger='arsse-intelligence'):
        assert check_api_key_user(client) is True
    assert not caplog.records


def test_unreachable_miniflux_does_not_break_the_cycle(config, store):
    import requests
    client = FakeClient(sample_entries())
    client.user = requests.ConnectionError('miniflux: Name or service not known')
    clusterer = NewsClusterer(config, store, client=client)

    assert clusterer.run_clustering_cycle()['errors'] == 0
    assert clusterer.api_key_checked is False  # asked again next cycle

    client.user = {'id': 1, 'username': 'admin', 'is_admin': True}
    clusterer.run_clustering_cycle()
    assert clusterer.api_key_checked is True
    client.user = RuntimeError('must not be asked again')
    assert clusterer.run_clustering_cycle()['errors'] == 0

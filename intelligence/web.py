"""
Top Stories web interface for the aRSSe Intelligence Layer.

Server-rendered, JavaScript-free and without external resources, so it
works on E-Ink readers and behind restrictive networks alike.
"""

import hmac
import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, render_template, request

from config import Config, WebAuthConfig
from store import StoryStore, parse_date

logger = logging.getLogger('arsse-intelligence')

# Hostnames and IP literals as they may appear in a Host header
_HOSTNAME = re.compile(r'^[A-Za-z0-9._-]+$|^[0-9A-Fa-f:.]+$')

# The pages have no JavaScript and load nothing from elsewhere, so the
# policy can forbid everything except the inline <style> block
CONTENT_SECURITY_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                           "manifest-src 'self'; base-uri 'none'; form-action 'self'; "
                           "frame-ancestors 'none'")
SECURITY_HEADERS = {
    'Content-Security-Policy': CONTENT_SECURITY_POLICY,
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'same-origin',
    'Cross-Origin-Resource-Policy': 'same-origin',
}
# Reachable without login: the Docker health check and files a browser
# fetches without credentials (web app manifest)
PUBLIC_PATHS = ('/healthz',)
PUBLIC_PREFIXES = ('/static/',)
# Always accepted Host names, so the health check and local calls work
LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def is_loopback_url(url: str) -> bool:
    """True if url has no host or one that only works on the server itself."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return True
    if not host or host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def request_hostname() -> Optional[str]:
    """Hostname the browser used for this request, bracketed if IPv6."""
    try:
        host = urlsplit(f'//{request.host}').hostname
    except ValueError:
        return None
    if not host or not _HOSTNAME.match(host):
        return None
    return f'[{host}]' if ':' in host else host


def _unauthorized() -> Response:
    return Response('Anmeldung erforderlich\n', 401, {
        'WWW-Authenticate': 'Basic realm="aRSSe", charset="UTF-8"',
        'Content-Type': 'text/plain; charset=utf-8',
    })


def _peer_address(remote_addr: Optional[str]):
    """TCP peer of the request as an IP address, or None."""
    try:
        address = ipaddress.ip_address(remote_addr or '')
    except ValueError:
        return None
    # IPv4 clients on a dual-stack socket appear as ::ffff:a.b.c.d
    if address.version == 6 and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _authenticate(auth: WebAuthConfig, networks: list) -> Optional[Response]:
    """Check the request against web.auth; returns an error response or None."""
    if auth.mode == 'basic':
        credentials = request.authorization
        if credentials is None or credentials.type != 'basic':
            return _unauthorized()
        # Compare both parts in constant time, without short-circuiting
        user_ok = hmac.compare_digest((credentials.username or '').encode(),
                                      auth.username.encode())
        password_ok = hmac.compare_digest((credentials.password or '').encode(),
                                          auth.password.encode())
        if not (user_ok & password_ok):
            return _unauthorized()
        g.user = auth.username
    elif auth.mode == 'proxy':
        # Only the proxy may set the header: anyone else could just send it
        peer = _peer_address(request.remote_addr)
        user = request.headers.get(auth.proxy_header, '').strip()
        if peer is None or not any(peer in network for network in networks):
            logger.info("Rejected request from %s: not in web.auth.trusted_proxies",
                        request.remote_addr)
            abort(403)
        if not user:
            logger.info("Rejected request from %s: no %s header", request.remote_addr,
                        auth.proxy_header)
            abort(403)
        g.user = user
    return None


def _origin_host(value: str) -> Optional[str]:
    """host[:port] of an Origin or Referer, without default ports."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return None
    return _strip_default_port(parts.netloc)


def _strip_default_port(host: str) -> str:
    host = host.lower().rsplit('@', 1)[-1]
    for port in (':80', ':443'):
        if host.endswith(port):
            return host[:-len(port)]
    return host


def require_same_origin() -> None:
    """
    Reject cross-site requests (CSRF) to state-changing routes with 403.

    Browsers send Sec-Fetch-Site; older ones at least Origin or Referer,
    which must name the host the request went to. Basic Auth alone does
    not help: browsers attach cached credentials to cross-site forms too.
    """
    site = request.headers.get('Sec-Fetch-Site')
    if site is not None:
        if site in ('same-origin', 'none'):
            return
        abort(403)
    source = request.headers.get('Origin') or request.headers.get('Referer')
    if not source or _origin_host(source) != _strip_default_port(request.host):
        abort(403)


def create_app(config: Config, store: StoryStore) -> Flask:
    """Create the Flask application."""
    app = Flask(__name__)
    public_url = config.miniflux_public_url.rstrip('/')
    max_age_hours = config.scheduling.lookback_hours

    # A loopback BASE_URL only works on the server itself; the E-Ink reader is
    # always another device. Build the link from the host it used instead.
    loopback = is_loopback_url(public_url)
    if loopback:
        logger.warning("BASE_URL '%s' points at localhost, which only works on the "
                       "server itself; Top Stories links use the requesting host with "
                       "port %d instead. Set BASE_URL in .env to the address your "
                       "devices use.", public_url, config.miniflux_public_port)
    base_path = urlsplit(public_url).path.rstrip('/') if loopback else ''

    auth = config.web.auth
    trusted_networks = [ipaddress.ip_network(n, strict=False) for n in auth.trusted_proxies]
    allowed_hosts = ({h.lower() for h in config.web.allowed_hosts} | set(LOOPBACK_HOSTS)
                     if config.web.allowed_hosts else None)
    if auth.mode == 'none':
        logger.warning("Top Stories are not protected (web.auth.mode=none): everyone who "
                       "reaches port 8081 can read your subscriptions and read status. Set "
                       "WEB_AUTH_MODE=basic with WEB_USERNAME/WEB_PASSWORD, or protect the "
                       "port at your reverse proxy (README, 'Absicherung').")
    else:
        logger.info("Top Stories require authentication (web.auth.mode=%s)", auth.mode)

    @app.before_request
    def guard():
        """Host allowlist, authentication and CSRF check for every request."""
        if allowed_hosts is not None:
            try:
                host = urlsplit(f'//{request.host}').hostname
            except ValueError:
                host = None
            if host not in allowed_hosts:
                # DNS rebinding: a foreign page resolving its name to this server
                logger.info("Rejected request for host '%s' (web.allowed_hosts)",
                            request.host)
                abort(400)
        if request.path in PUBLIC_PATHS or request.path.startswith(PUBLIC_PREFIXES):
            return None
        denied = _authenticate(auth, trusted_networks)
        if denied is not None:
            return denied
        if request.method not in SAFE_METHODS:
            require_same_origin()
        return None

    @app.after_request
    def security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def miniflux_base() -> str:
        """Base URL of Miniflux as seen by the current browser."""
        if not loopback:
            return public_url
        host = request_hostname()
        if not host:
            return public_url
        # Miniflux itself speaks plain HTTP on MINIFLUX_PORT
        return f"http://{host}:{config.miniflux_public_port}{base_path}"

    def entry_link(article: Optional[dict]) -> str:
        """Link to the article inside Miniflux (works for read and unread)."""
        if not article:
            return miniflux_base()
        if article.get('feed_id'):
            return f"{miniflux_base()}/feed/{article['feed_id']}/entry/{article['id']}"
        return safe_url(article.get('url')) or miniflux_base()

    def safe_url(url: Optional[str]) -> str:
        """Only pass through http(s) URLs; feeds are untrusted input."""
        if url and urlsplit(url).scheme in ('http', 'https'):
            return url
        return ''

    @app.template_filter('ago')
    def ago(value: Optional[str]) -> str:
        parsed = parse_date(value)
        if not parsed:
            return ''
        minutes = int((datetime.now(timezone.utc) - parsed).total_seconds() // 60)
        if minutes < 0:
            return ''  # dated in the future: no claim is better than a wrong one
        if minutes < 1:
            return 'gerade eben'
        if minutes < 60:
            return f'vor {minutes} Min.'
        if minutes < 60 * 24:
            return f'vor {minutes // 60} Std.'
        return f'vor {minutes // (60 * 24)} T.'

    @app.context_processor
    def helpers():
        return {'entry_link': entry_link, 'safe_url': safe_url, 'miniflux_url': miniflux_base()}

    def top_stories() -> list:
        return store.top_stories(max_age_hours, config.web.max_stories,
                                 config.web.min_sources, config.web.earlier_articles_max,
                                 config.web.exclude_patterns)

    @app.get('/')
    def index():
        return render_template(
            'index.html',
            stories=top_stories(),
            per_story=config.web.articles_per_story,
            last_success=store.get_meta('last_success'),
        )

    @app.get('/story/<story_id>')
    def story(story_id: str):
        found = store.get_story(story_id, max_age_hours, config.web.earlier_articles_max)
        if not found:
            abort(404)
        return render_template('story.html', story=found)

    @app.get('/api/stories')
    def api_stories():
        return jsonify(top_stories())

    @app.get('/healthz')
    def healthz():
        """Healthy while clustering runs succeed within 3 intervals."""
        last_success = parse_date(store.get_meta('last_success'))
        limit = timedelta(minutes=config.scheduling.interval_minutes * 3)
        healthy = last_success is not None and datetime.now(timezone.utc) - last_success < limit
        body = {
            'status': 'ok' if healthy else 'stale',
            'last_success': last_success.isoformat() if last_success else None,
            'last_stats': store.get_meta('last_stats'),
        }
        return jsonify(body), 200 if healthy else 503

    return app

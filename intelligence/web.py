"""
Top Stories web interface for the aRSSe Intelligence Layer.

Server-rendered, JavaScript-free and without external resources, so it
works on E-Ink readers and behind restrictive networks alike.
"""

import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request

from config import Config
from store import StoryStore, parse_date

logger = logging.getLogger('arsse-intelligence')

# Hostnames and IP literals as they may appear in a Host header
_HOSTNAME = re.compile(r'^[A-Za-z0-9._-]+$|^[0-9A-Fa-f:.]+$')


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

    @app.get('/')
    def index():
        stories = store.top_stories(max_age_hours, config.web.max_stories,
                                    config.web.min_sources, config.web.earlier_articles_max)
        return render_template(
            'index.html',
            stories=stories,
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
        return jsonify(store.top_stories(max_age_hours, config.web.max_stories,
                                         config.web.min_sources,
                                         config.web.earlier_articles_max))

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

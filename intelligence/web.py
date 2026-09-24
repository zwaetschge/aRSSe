"""
Top Stories web interface for the aRSSe Intelligence Layer.

Server-rendered, JavaScript-free and without external resources, so it
works on E-Ink readers and behind restrictive networks alike.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template

from config import Config
from store import StoryStore, parse_date


def create_app(config: Config, store: StoryStore) -> Flask:
    """Create the Flask application."""
    app = Flask(__name__)
    public_url = config.miniflux_public_url.rstrip('/')
    max_age_hours = config.scheduling.lookback_hours

    def entry_link(article: dict) -> str:
        """Link to the article inside Miniflux (works for read and unread)."""
        if article.get('feed_id'):
            return f"{public_url}/feed/{article['feed_id']}/entry/{article['id']}"
        return safe_url(article.get('url')) or public_url

    def safe_url(url: Optional[str]) -> str:
        """Only pass through http(s) URLs; feeds are untrusted input."""
        if url and urlparse(url).scheme in ('http', 'https'):
            return url
        return ''

    @app.template_filter('ago')
    def ago(value: Optional[str]) -> str:
        parsed = parse_date(value)
        if not parsed:
            return ''
        minutes = int((datetime.now(timezone.utc) - parsed).total_seconds() // 60)
        if minutes < 1:
            return 'gerade eben'
        if minutes < 60:
            return f'vor {minutes} Min.'
        if minutes < 60 * 24:
            return f'vor {minutes // 60} Std.'
        return f'vor {minutes // (60 * 24)} T.'

    @app.context_processor
    def helpers():
        return {'entry_link': entry_link, 'safe_url': safe_url, 'miniflux_url': public_url}

    @app.get('/')
    def index():
        stories = store.top_stories(max_age_hours, config.web.max_stories, config.web.min_sources)
        return render_template(
            'index.html',
            stories=stories,
            per_story=config.web.articles_per_story,
            last_success=store.get_meta('last_success'),
        )

    @app.get('/story/<story_id>')
    def story(story_id: str):
        found = store.get_story(story_id)
        if not found:
            abort(404)
        return render_template('story.html', story=found)

    @app.get('/api/stories')
    def api_stories():
        return jsonify(store.top_stories(max_age_hours, config.web.max_stories, config.web.min_sources))

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

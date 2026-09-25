"""
Top Stories web interface for the aRSSe Intelligence Layer.

Server-rendered, JavaScript-free and without external resources, so it
works on E-Ink readers and behind restrictive networks alike. The only
state-changing route, 'Story gelesen', is a plain form (POST) that marks
the story's unread articles read in Miniflux.
"""

import hmac
import ipaddress
import logging
import mimetypes
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)
from jinja2 import BaseLoader
from markupsafe import Markup

from config import MAX_SECTION_CHARS, Config, WebAuthConfig, resolve_timezone
from store import MAX_QUERY_CHARS, StoryStore, parse_date, select_coverage

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
# Reachable without login: the Docker health check and the home-screen files
# (manifest and icons; Android fetches the icons without credentials)
PUBLIC_PATHS = ('/healthz',)
PUBLIC_PREFIXES = ('/static/',)
# Always accepted Host names, so the health check and local calls work
LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
# Not in every mime.types; browsers ignore a manifest served as text/plain
mimetypes.add_type('application/manifest+json', '.webmanifest')

# Always-on displays reload the front page this often with ?auto=1 (seconds)
AUTO_REFRESH_SECONDS = 1800
# An untitled article is labelled with this many characters of its text
UNTITLED_LABEL_CHARS = 80
# Shorter search queries match nearly everything
MIN_QUERY_CHARS = 2
# 'Mehr zum Thema' links under a story on the front page; its story page
# lists all related stories
RELATED_ON_FRONT_PAGE = 3
# Longest redirect target accepted after 'Story gelesen'
MAX_NEXT_CHARS = 2000

WEEKDAYS = ('Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So')
WEEKDAY_NAMES = ('Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag',
                 'Samstag', 'Sonntag')
MONTHS = ('Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August',
          'September', 'Oktober', 'November', 'Dezember')


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


def clock(value: Optional[str], zone, now: Optional[datetime] = None) -> Markup:
    """
    Absolute time for pages that stay on screen for hours (E-Ink).

    '14:53' today, 'Mi 14:53' within the last week and '17.09. 14:53'
    before that, in the time zone zone and wrapped in <time datetime=...>.
    Relative times ('vor 5 Min.') would silently go stale.
    """
    parsed = parse_date(value)
    if not parsed:
        return Markup('')
    local = parsed.astimezone(zone)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    text = local.strftime('%H:%M')
    days = abs((today - local.date()).days)
    if days >= 7:
        text = local.strftime('%d.%m. ') + text
    elif days:
        text = f'{WEEKDAYS[local.weekday()]} {text}'
    return Markup('<time datetime="{}">{}</time>').format(
        local.isoformat(timespec='minutes'), text)


def day_label(day: date, today: date) -> str:
    """'Heute', 'Gestern' or 'Mittwoch, 23. September' as a timeline heading."""
    if day == today:
        return 'Heute'
    if day == today - timedelta(days=1):
        return 'Gestern'
    return f'{WEEKDAY_NAMES[day.weekday()]}, {day.day}. {MONTHS[day.month - 1]}'


def display_title(article: Optional[dict], with_feed: bool = True) -> str:
    """
    Title of an article; an untitled one (news ticker) is labelled with the
    start of its text, prefixed with its feed unless the source is shown
    next to it anyway.
    """
    if not article:
        return '(ohne Titel)'
    title = (article.get('title') or '').strip()
    if title:
        return title
    snippet = (article.get('snippet') or '').strip()
    if not snippet:
        return '(ohne Titel)'
    if len(snippet) > UNTITLED_LABEL_CHARS:
        snippet = snippet[:UNTITLED_LABEL_CHARS].rsplit(' ', 1)[0].rstrip(' .,;:') + ' …'
    feed = (article.get('feed_title') or '').strip()
    return f'{feed}: {snippet}' if with_feed and feed else snippet


def safe_next(value: Optional[str]) -> str:
    """
    Where to return after a form: a path on this server, else '/'.

    Only paths starting with a single '/' are accepted; '//host' and
    '/\\host' would lead browsers to another site (open redirect), and
    control characters have no place in a Location header.
    """
    if (not value or len(value) > MAX_NEXT_CHARS or not value.startswith('/')
            or value.startswith('//') or '\\' in value
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in value)):
        return '/'
    return value


def _text_response(text: str, status: int) -> Response:
    return Response(text + '\n', status, {'Content-Type': 'text/plain; charset=utf-8'})


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


class DedentLoader(BaseLoader):
    """
    Strip the leading indentation of every template line.

    lstrip_blocks only removes it before block tags; the indentation of
    plain HTML lines would otherwise cost about 2 KB on a busy front page.
    None of the templates contains whitespace-sensitive markup (<pre>).
    """

    def __init__(self, loader):
        self.loader = loader

    def get_source(self, environment, template):
        source, filename, uptodate = self.loader.get_source(environment, template)
        return re.sub(r'(?m)^[ \t]+', '', source), filename, uptodate

    def list_templates(self):
        return self.loader.list_templates()


def create_app(config: Config, store: StoryStore, client=None) -> Flask:
    """
    Create the Flask application.

    client is a Miniflux client for 'Story gelesen'; without one (no API
    key) the button is not shown.
    """
    app = Flask(__name__, static_folder=STATIC_DIR)
    # Template indentation is not sent: every KB counts on E-Ink readers
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.jinja_env.loader = DedentLoader(app.jinja_env.loader)
    zone = resolve_timezone(config.web.timezone)
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

    client_lock = threading.Lock()
    started = datetime.now(timezone.utc)
    auth = config.web.auth
    trusted_networks = [ipaddress.ip_network(n, strict=False) for n in auth.trusted_proxies]
    allowed_hosts = ({h.lower() for h in config.web.allowed_hosts} | set(LOOPBACK_HOSTS)
                     if config.web.allowed_hosts else None)
    if auth.mode == 'none':
        logger.warning("Top Stories are not protected (web.auth.mode=none): everyone who "
                       "reaches port 8081 can read your subscriptions and read status, and "
                       "mark stories read in your Miniflux ('Story gelesen'). Set "
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

    @app.template_filter('clock')
    def clock_filter(value: Optional[str]) -> Markup:
        return clock(value, zone)

    def timeline(articles: list) -> list:
        """Articles oldest first, grouped by day: [(heading, articles)]."""
        today = datetime.now(timezone.utc).astimezone(zone).date()
        dated = [(parse_date(a['published_at']), a) for a in articles]
        dated.sort(key=lambda pair: (pair[0] is not None, pair[0] or 0, pair[1]['id']))
        return [(day_label(day, today) if day else 'Ohne Datum',
                 [a for _, a in group])
                for day, group in groupby(dated, key=lambda pair: pair[0].astimezone(zone)
                                          .date() if pair[0] else None)]

    def always_on() -> bool:
        """The page was opened as an always-on display (?auto=1)."""
        return request.args.get('auto') == '1'

    def nav(path: str, **args) -> str:
        """Link to one of our pages, keeping ?auto=1 on an always-on display."""
        if always_on():
            args['auto'] = 1
        return f"{path}?{urlencode(args)}" if args else path

    def here() -> str:
        """This page's path and query, to return to after a form."""
        query = request.query_string.decode('latin-1')
        return request.path + ('?' + query if query else '')

    @app.context_processor
    def helpers():
        return {'entry_link': entry_link, 'safe_url': safe_url, 'miniflux_url': miniflux_base(),
                'display_title': display_title, 'select_coverage': select_coverage,
                'timeline': timeline, 'nav': nav, 'here': here,
                'can_mark_read': client is not None,
                'auto_refresh': AUTO_REFRESH_SECONDS if always_on() else None}

    def listing_args(section: Optional[str], show_read: bool) -> dict:
        """Query arguments that select a front page: section and read stories."""
        args = {}
        if section is not None:
            args['rubrik'] = section
        if show_read:
            args['alle'] = 1
        return args

    def page_number() -> int:
        """?seite=N as a number; anything else is not a page (404)."""
        page = request.args.get('seite', '1')
        if not re.fullmatch(r'[1-9][0-9]{0,5}', page):
            abort(404)
        return int(page)

    def show_read() -> bool:
        """?alle=1: list stories whose articles are all read, too."""
        return request.args.get('alle') == '1'

    def chosen_section() -> Optional[str]:
        """?rubrik=Name, or None for all sections."""
        section = request.args.get('rubrik') or None
        if section is not None and len(section) > MAX_SECTION_CHARS:
            abort(404)
        return section

    @app.get('/')
    def index():
        page = page_number()
        section = chosen_section()
        everything = show_read()
        args = listing_args(section, everything)

        def page_url(number: int) -> str:
            """Link to another page of this listing, keeping ?auto=1."""
            return nav(url_for('index'), **args, **({'seite': number} if number > 1 else {}))

        page_size = config.web.page_size
        front = store.front_page(
            max_age_hours, config.web.max_stories, (page - 1) * page_size, page_size,
            config.web.min_sources, config.web.earlier_articles_max,
            config.web.exclude_patterns, section=section, hide_read=not everything)
        pages = max(1, -(-front.total // page_size))
        if page > pages:
            # An always-on display stays on the page it paged to; when stories
            # age out it must land on the last page, not a 404 that never reloads
            if always_on():
                return redirect(page_url(pages))
            abort(404)
        return render_template(
            'index.html',
            stories=front.stories,
            total=front.total,
            sections=front.sections,
            section=section,
            show_read=everything,
            hidden_read=front.hidden_read,
            section_url=lambda name: nav(url_for('index'), **listing_args(name, everything)),
            read_toggle_url=nav(url_for('index'), **listing_args(section, not everything)),
            page=page,
            pages=pages,
            prev_url=page_url(page - 1) if page > 1 else None,
            next_url=page_url(page + 1) if page < pages else None,
            per_story=config.web.articles_per_story,
            related_max=RELATED_ON_FRONT_PAGE,
            last_success=store.get_meta('last_success'),
            last_error=store.get_meta('last_error'),
        )

    @app.get('/story/<story_id>')
    def story(story_id: str):
        found = store.get_story(story_id, max_age_hours, config.web.earlier_articles_max)
        if not found:
            # The story aged out while an always-on display showed it
            if always_on():
                return redirect(nav(url_for('index')))
            abort(404)
        chronological = request.args.get('ansicht') == 'chronologisch'
        return render_template(
            'story.html',
            story=found,
            chronological=chronological,
            related=store.related_stories(found, max_age_hours, config.web.min_sources,
                                          0, config.web.exclude_patterns),
            # An always-on display returns to the front page instead of
            # showing the story someone tapped into forever
            refresh_url=nav(url_for('index')),
            # Oldest and newest article in the window (articles are newest first)
            first=found['articles'][-1],
            last=found['articles'][0],
            originals=[a for a in found['articles'] if not a['is_duplicate']],
            duplicates=[a for a in found['articles'] if a['is_duplicate']],
        )

    def last_page_if_gone(target: str) -> str:
        """
        target, or the last page of its front page if that page is gone.

        Marking the only story of the last page read removes the page;
        returning there would be a 404.
        """
        parts = urlsplit(target)
        args = dict(parse_qsl(parts.query))
        page = args.get('seite', '')
        if parts.path != url_for('index') or not re.fullmatch(r'[1-9][0-9]{0,5}', page) \
                or page == '1':
            return target
        section = args.get('rubrik') or None
        if section is not None and len(section) > MAX_SECTION_CHARS:
            return target
        total = store.front_page(
            max_age_hours, config.web.max_stories, 0, 0, config.web.min_sources, 0,
            config.web.exclude_patterns, section=section,
            hide_read=args.get('alle') != '1').total
        pages = max(1, -(-total // config.web.page_size))
        if int(page) <= pages:
            return target
        if pages > 1:
            args['seite'] = str(pages)
        else:
            del args['seite']
        return parts.path + ('?' + urlencode(args) if args else '')

    @app.post('/story/<story_id>/gelesen')
    def mark_story_read(story_id: str):
        """
        'Story gelesen': mark the story's unread articles read in Miniflux.

        Only articles inside the window count, as everywhere on the front
        page. They are recorded as read by the user (user_read), not as
        duplicates aRSSe marked (auto_marked). Answers 303 to the page the
        form came from (next).
        """
        # The before_request guard checked login and origin already; a
        # route that writes to the user's Miniflux checks the origin again
        require_same_origin()
        target = safe_next(request.form.get('next'))
        if client is None:
            return _text_response('Kein MINIFLUX_API_KEY gesetzt – Stories lassen sich nicht '
                                  'als gelesen markieren.', 503)
        found = store.get_story(story_id, max_age_hours, 0)
        unread = [a['id'] for a in found['articles'] if a['status'] == 'unread'] \
            if found else []
        if unread:
            try:
                # One requests session for all web threads: calls take turns
                with client_lock:
                    client.update_entries(unread, status='read')
            except Exception as e:
                logger.warning("Could not mark story %s read in Miniflux: %s", story_id, e)
                return _text_response('Miniflux ist nicht erreichbar – die Story wurde nicht '
                                      'als gelesen markiert. Bitte später erneut versuchen.',
                                      502)
            store.mark_read(unread)
            logger.info("Marked %d articles of story %s read", len(unread), story_id)
        return redirect(last_page_if_gone(target), code=303)

    @app.get('/suche')
    def search():
        query = (request.args.get('q') or '').strip()
        page = page_number()
        valid = MIN_QUERY_CHARS <= len(query) <= MAX_QUERY_CHARS
        results = store.search(query, max_age_hours, config.web.max_stories,
                               config.web.min_sources, config.web.exclude_patterns) \
            if valid else []
        page_size = config.web.page_size
        pages = max(1, -(-len(results) // page_size))
        if page > pages:
            abort(404)

        def page_url(number: int) -> str:
            return nav(url_for('search'), q=query, **({'seite': number} if number > 1 else {}))

        return render_template(
            'search.html',
            query=query if valid else '',
            too_long=len(query) > MAX_QUERY_CHARS,
            stories=results[(page - 1) * page_size:page * page_size],
            total=len(results),
            page=page,
            pages=pages,
            prev_url=page_url(page - 1) if page > 1 else None,
            next_url=page_url(page + 1) if page < pages else None,
            per_story=config.web.articles_per_story,
            miniflux_search=f"{miniflux_base()}/search?{urlencode({'q': query})}"
            if valid else None,
        )

    @app.get('/favicon.ico')
    def favicon():
        # Browsers ask for it without being told; the pages link /static icons
        return send_from_directory(STATIC_DIR, 'favicon.ico')

    @app.get('/api/stories')
    def api_stories():
        """All listed stories, flat (no pages, no topic groups); ?rubrik= and ?alle=1."""
        return jsonify(store.top_stories(
            max_age_hours, config.web.max_stories, config.web.min_sources,
            config.web.earlier_articles_max, config.web.exclude_patterns,
            section=chosen_section(), hide_read=not show_read()))

    @app.get('/healthz')
    def healthz():
        """
        Healthy while clustering runs succeed within 3 intervals.

        'ok' (200): a run succeeded within 3 intervals. 'starting' (200):
        no run succeeded since the start, none failed, and the service runs
        for less than 3 intervals. 'stale' (503): anything else; last_error
        says why the last run failed.
        """
        now = datetime.now(timezone.utc)
        last_success = parse_date(store.get_meta('last_success'))
        last_error = store.get_meta('last_error')
        limit = timedelta(minutes=config.scheduling.interval_minutes * 3)
        if last_success is not None and now - last_success < limit:
            status = 'ok'
        elif (not last_error and (last_success is None or last_success < started)
              and now - started < limit):
            status = 'starting'
        else:
            status = 'stale'
        body = {
            'status': status,
            'last_success': last_success.isoformat() if last_success else None,
            'last_error': last_error,
            'last_stats': store.get_meta('last_stats'),
        }
        return jsonify(body), 503 if status == 'stale' else 200

    return app

"""
Configuration module for aRSSe Intelligence Layer.

Handles loading and validation of configuration from environment
variables and YAML configuration files.

Precedence: defaults < /app/config.yaml (baked into the image, reference
only) < /app/data/config.yaml (optional user settings next to the story
database) < environment variables. Each file only changes the settings it
names. Empty environment variables are ignored, so docker-compose can pass
variables through without clobbering values from the files.
"""

import dataclasses
import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger('arsse-intelligence')

# Settings baked into the image: the documented reference, never edited
DEFAULT_CONFIG_PATH = '/app/config.yaml'
# Optional user settings next to the story database (in appdata backups,
# untouched by image updates and 'git pull')
USER_CONFIG_PATH = '/app/data/config.yaml'
# Commented template copied to USER_CONFIG_PATH when it is missing
USER_CONFIG_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'config.stub.yaml')

DUPLICATE_ACTIONS = ('none', 'mark_read')
MARK_READ_SCOPES = ('visible', 'all')
CANONICAL_STRATEGIES = ('longest', 'source_priority', 'newest')
WEB_AUTH_MODES = ('none', 'basic', 'proxy')
# Time zone of the web interface when neither web.timezone nor TZ is set
DEFAULT_TIMEZONE = 'Europe/Berlin'

# Values accepted by older versions of config.yaml
_LEGACY_DUPLICATE_ACTIONS = {'tag': 'none', 'hide': 'mark_read'}
# DBSCAN settings from before the switch to average-linkage clustering
_LEGACY_CLUSTERING_KEYS = ('eps', 'min_samples', 'metric')

# Miniflux rejects larger pages (model.MaxEntryLimit)
MAX_BATCH_SIZE = 1000
# Average linkage needs time and memory quadratic in the number of articles
MAX_ENTRIES_LIMIT = 5000
# Longer word n-grams only add rare features (and cost time); measured on
# the reference corpus, even bigrams lower precision and recall
MAX_NGRAM = 3

# Titles of items that should not head a story: ads, paywall teasers,
# podcasts, live blogs, videos, weather and daily roundups. Matched
# case-insensitively anywhere in the title (use ^ to anchor).
DEFAULT_NOISE_TITLE_PATTERNS = [
    r'^(Anzeige|heise-Angebot):',
    r'^\((g|S)\+\)',
    r'heise\+',
    r'SPIEGEL\+',
    r'\bF\+',
    r'SZ Plus',
    r'Podcasts?\b',
    r'Live-?blog|Live-?ticker|Newsblog',
    r'^Video:',
    r'^Wetter\b',
    r'News des Tages',
    r'^Was jetzt\?',
    r'Briefing',
]

# Example priorities for canonical_strategy 'source_priority' (others: 50)
DEFAULT_SOURCE_SCORES = {
    'sueddeutsche.de': 100,
    'zeit.de': 95,
    'spiegel.de': 90,
    'faz.net': 90,
    'tagesschau.de': 85,
    'heise.de': 80,
    'golem.de': 75,
}

# Sections (Rubriken) from URL path segments, for feeds in Miniflux's default
# category: regular expression for one whole segment -> section name. The
# first pattern (in this order) that matches any segment of the path wins,
# so the specific ones come before the broad 'Politik'.
DEFAULT_PATH_SECTIONS = {
    r'regional(es)?|baden-wuerttemberg|bayern|berlin(-brandenburg)?|brandenburg|bremen'
    r'|hamburg(-schleswig-holstein)?|hessen|mecklenburg-vorpommern|niedersachsen'
    r'|nordrhein-westfalen|nrw|rheinland-pfalz|saarland|sachsen|sachsen-anhalt'
    r'|schleswig-holstein|th(ue|ü)ringen': 'Regional',
    r'sport|fussball\w*': 'Sport',
    r'wissen|technik|digital|netzwelt': 'Technik',
    r'wirtschaft|finanzen': 'Wirtschaft',
    r'kultur|feuilleton': 'Kultur',
    r'panorama|gesellschaft': 'Panorama',
    r'politik|inland|ausland': 'Politik',
}
# Longest section name accepted (the nav row has to fit an E-Ink screen)
MAX_SECTION_CHARS = 40


class ConfigError(ValueError):
    """A setting is invalid; the message names the setting."""


@dataclass
class ClusteringConfig:
    """Configuration for average-linkage clustering."""
    # Maximum average cosine distance between the articles of one story
    threshold: float = 0.75
    max_features: int = 5000
    language: str = "german"
    stemming: bool = True
    # Longest word n-gram in the TF-IDF vocabulary (1 = single words)
    ngram_max: int = 1
    # Minimum cosine similarity of a story of only two articles. Average
    # linkage accepts any pair above 1 - threshold (0.25), which one shared
    # rare word reaches; 0 disables the check.
    min_pair_similarity: float = 0.30
    # Maximum average cosine distance of the articles of one topic: stories
    # of one topic (the same event from different angles) take one slot on
    # the front page. Must be above threshold; 0 disables topics.
    topic_threshold: float = 0.9
    # Regular expressions (case-insensitive) for titles that never head a
    # story while another article can (see DEFAULT_NOISE_TITLE_PATTERNS)
    noise_title_patterns: list = field(
        default_factory=lambda: list(DEFAULT_NOISE_TITLE_PATTERNS))


@dataclass
class DeduplicationConfig:
    """Configuration for duplicate detection."""
    threshold: float = 0.85
    canonical_strategy: str = "longest"
    source_scores: dict = field(default_factory=lambda: dict(DEFAULT_SOURCE_SCORES))
    duplicate_action: str = "mark_read"
    # Articles from different feeds are only compared if both bodies (without
    # the title) have at least this many words: teasers like '[ mehr ]' say
    # nothing about the article
    min_body_tokens: int = 25
    # 'visible': mark read only in stories shown on the front page
    # (web.min_sources feeds) and identical copies; 'all': in every cluster
    mark_read_scope: str = "visible"


@dataclass
class SchedulingConfig:
    """Configuration for scheduling."""
    interval_minutes: int = 30
    batch_size: int = 250
    max_entries: int = 2000
    lookback_hours: int = 24
    # Minutes between light checks for articles read in Miniflux, so a story
    # read there leaves the front page before the next clustering run; 0 = off
    status_sync_minutes: int = 5


@dataclass
class StorageConfig:
    """Configuration for the local story database."""
    db_path: str = "/app/data/arsse.db"
    retention_days: int = 7


@dataclass
class WebAuthConfig:
    """Access control for the Top Stories web interface."""
    # 'none': open to everyone who reaches the port; 'basic': HTTP Basic
    # Auth with username/password; 'proxy': a reverse proxy authenticates
    # and passes the user in proxy_header
    mode: str = "none"
    username: str = ""
    password: str = ""
    # File with the password (Docker secret); takes precedence over password
    password_file: str = ""
    proxy_header: str = "Remote-User"
    # Addresses (CIDR) whose proxy_header is trusted; required for 'proxy'
    trusted_proxies: list = field(default_factory=list)


@dataclass
class WebConfig:
    """Configuration for the Top Stories web interface."""
    host: str = "0.0.0.0"
    port: int = 8081
    # Overall cap on the stories listed, over all pages
    max_stories: int = 100
    # Stories per page: short pages suit E-Ink (one refresh per page)
    page_size: int = 10
    articles_per_story: int = 6
    # Stories covered by fewer feeds are not shown (filters feed-internal
    # series and advertising that only resemble each other)
    min_sources: int = 2
    # Articles older than the lookback window listed on a story page
    earlier_articles_max: int = 20
    # Stories whose articles (inside the window) all have a title matching
    # one of these regular expressions (case-insensitive) are not listed
    exclude_patterns: list = field(default_factory=lambda: [r'^Wetter\b'])
    # Host names the interface answers to (DNS rebinding protection);
    # empty = any. localhost and loopback addresses are always allowed.
    allowed_hosts: list = field(default_factory=list)
    # Time zone of the times shown (IANA name, e.g. Europe/Berlin);
    # empty = the TZ environment variable, else DEFAULT_TIMEZONE
    timezone: str = ""
    # Sections of articles in Miniflux's default category, from URL path
    # segments (see DEFAULT_PATH_SECTIONS); {} = only Miniflux categories
    path_sections: dict = field(default_factory=lambda: dict(DEFAULT_PATH_SECTIONS))
    auth: WebAuthConfig = field(default_factory=WebAuthConfig)


@dataclass
class LoggingConfig:
    """Configuration for logging."""
    level: str = "INFO"
    json_format: bool = False
    file: str = ""


@dataclass
class Config:
    """Main configuration container."""
    miniflux_url: str = "http://miniflux:8080"
    miniflux_api_key: str = ""
    # URL under which the browser reaches Miniflux (used for links in the UI)
    miniflux_public_url: str = "http://localhost:8080"
    # Host port of Miniflux; used for links when public_url is a loopback address
    miniflux_public_port: int = 8080
    # Leave out articles of feeds and categories hidden from Miniflux's
    # global views ('hide_globally'), as Miniflux's own lists do
    miniflux_respect_hide_globally: bool = True
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    deduplication: DeduplicationConfig = field(default_factory=DeduplicationConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(config_path: Optional[str] = None,
                user_config_path: Optional[str] = None) -> Config:
    """
    Load configuration from YAML files and environment variables.

    The user file overrides the settings it names in config_path, and
    environment variables override both. Missing files are skipped.

    Args:
        config_path: Path to the YAML reference configuration.
        user_config_path: Path to the optional YAML file with the user's
            own settings. Given only in the service (layered mode): then a
            config_path that differs from the defaults is reported, since
            it was edited in place (see _warn_edited_reference).

    Returns:
        Config object with all settings.

    Raises:
        ConfigError: If a setting is invalid.
    """
    config = Config()

    base = _read_yaml(config_path)
    if base:
        _apply_yaml_config(config, base)
        if user_config_path is not None:
            _warn_edited_reference(config_path, config, user_config_path)
    user = _read_yaml(user_config_path)
    if user:
        _apply_yaml_config(config, user)
        logger.info("Using settings from %s", user_config_path)

    _apply_env_config(config)
    _validate(config)

    return config


def _read_yaml(path: Optional[str]) -> Optional[dict]:
    """Read one YAML configuration file; None if there is none."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"Cannot read {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if content is not None and not isinstance(content, dict):
        raise ConfigError(f"{path} must contain sections (clustering:, web:, ...), "
                          f"got {type(content).__name__}")
    return content


def _flatten(obj, prefix: str = '') -> dict:
    """Dotted setting name -> value for a (nested) configuration dataclass."""
    values = {}
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if dataclasses.is_dataclass(value):
            values.update(_flatten(value, f'{prefix}{f.name}.'))
        else:
            values[prefix + f.name] = value
    return values


def changed_settings(config: Config) -> list:
    """Names of the settings that differ from the defaults."""
    defaults = _flatten(Config())
    return [name for name, value in _flatten(config).items() if value != defaults[name]]


def _warn_edited_reference(path: str, config: Config, user_config_path: str) -> None:
    """
    Point out settings changed in the reference file instead of the user file.

    Before user files existed, the README said to edit
    intelligence/config.yaml, which compose mounted (or the build copied)
    to /app/config.yaml. Those settings still apply, but the published
    image and the next 'git pull' drop them.
    """
    changed = changed_settings(config)
    if changed:
        logger.warning("%s differs from the shipped defaults (%s): it was edited before "
                       "the image was built or is mounted from ./intelligence/config.yaml. "
                       "These settings apply for now, but the published image and "
                       "'git pull' do not keep them. Move them to %s (on the server: "
                       "DATA_PATH/intelligence/config.yaml; README, 'Konfiguration').",
                       path, ', '.join(changed), user_config_path)


def ensure_user_config(path: str, stub_path: str = USER_CONFIG_STUB) -> bool:
    """
    Create the user configuration from the commented stub if it is missing.

    Only inside an existing directory (the data volume); failures are
    logged, the service runs without the file.

    Returns:
        True if the file was created.
    """
    if os.path.lexists(path) or not os.path.isdir(os.path.dirname(path) or '.'):
        return False
    try:
        with open(stub_path, 'rb') as src:
            stub = src.read()
        # Only readable for the service: the file may hold a password
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as dst:
            dst.write(stub)
    except OSError as e:
        logger.warning("Could not create %s: %s", path, e)
        return False
    logger.info("Created %s for your own settings", path)
    return True


def _apply_section(target, values: Optional[dict]) -> None:
    """Copy known keys from a YAML section onto a dataclass instance."""
    if not values:
        return
    for key, value in values.items():
        if hasattr(target, key) and value is not None:
            setattr(target, key, value)
        else:
            logger.warning("Ignoring unknown config option '%s'", key)


def _apply_yaml_config(config: Config, yaml_config: dict) -> None:
    """Apply YAML configuration to Config object."""
    web = dict(yaml_config.get('web') or {})
    for key in ('host', 'port'):
        if web.pop(key, None) is not None:
            logger.warning("web.%s is ignored in config.yaml; the container always "
                           "listens on 8081 (change INTELLIGENCE_PORT in .env)", key)
    clustering = dict(yaml_config.get('clustering') or {})
    for key in _LEGACY_CLUSTERING_KEYS:
        if clustering.pop(key, None) is not None:
            logger.warning("clustering.%s is obsolete (DBSCAN was replaced); "
                           "use clustering.threshold instead", key)
    auth = web.pop('auth', None)
    yaml_config = {**yaml_config, 'web': web, 'clustering': clustering}

    for section in ('clustering', 'deduplication', 'scheduling',
                    'storage', 'web', 'logging'):
        _apply_section(getattr(config, section), yaml_config.get(section))
    if auth is not None and not isinstance(auth, dict):
        raise ConfigError(f"web.auth must be a section (mode, username, ...), got {auth!r}")
    _apply_section(config.web.auth, auth)

    if 'tagging' in yaml_config:
        logger.warning("The 'tagging' section is obsolete: Miniflux cannot "
                       "store tags via its API. Stories are kept locally.")

    if miniflux := yaml_config.get('miniflux'):
        config.miniflux_url = miniflux.get('url', config.miniflux_url)
        config.miniflux_public_url = miniflux.get('public_url', config.miniflux_public_url)
        config.miniflux_public_port = _parse_port(
            miniflux.get('public_port'), 'miniflux.public_port', config.miniflux_public_port)
        config.miniflux_respect_hide_globally = miniflux.get(
            'respect_hide_globally', config.miniflux_respect_hide_globally)


def _parse_port(value, name: str, default: int) -> int:
    """Parse a host port leniently, falling back to ``default``.

    Accepts Docker port syntax with a host address (``127.0.0.1:8080``).
    The port only matters for links when BASE_URL is a loopback address,
    so an unusable value is logged instead of stopping the service.
    """
    if value is None or value == '':
        return default
    try:
        port = int(str(value).rsplit(':', 1)[-1])
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        logger.warning("%s=%r is not a TCP port, using %d", name, value, default)
        return default
    return port


def _env(name: str) -> Optional[str]:
    """Return an environment variable, treating empty values as unset."""
    value = os.getenv(name)
    return value if value else None


def _apply_env_config(config: Config) -> None:
    """Apply environment variable overrides."""
    # Miniflux connection
    if url := _env('MINIFLUX_URL'):
        config.miniflux_url = url
    if api_key := _env('MINIFLUX_API_KEY'):
        config.miniflux_api_key = api_key
    if key_file := _env('MINIFLUX_API_KEY_FILE'):
        # Docker secret: keeps the key out of 'docker inspect'
        config.miniflux_api_key = _read_secret('MINIFLUX_API_KEY_FILE', key_file)
    if public_url := _env('MINIFLUX_PUBLIC_URL'):
        config.miniflux_public_url = public_url
    config.miniflux_public_port = _parse_port(
        _env('MINIFLUX_PORT'), 'MINIFLUX_PORT', config.miniflux_public_port)

    # Clustering
    if threshold := _env('CLUSTERING_THRESHOLD'):
        config.clustering.threshold = _env_number('CLUSTERING_THRESHOLD', threshold, float)
    if ngram_max := _env('CLUSTERING_NGRAM_MAX'):
        config.clustering.ngram_max = _env_number('CLUSTERING_NGRAM_MAX', ngram_max, int)
    if similarity := _env('CLUSTERING_MIN_PAIR_SIMILARITY'):
        config.clustering.min_pair_similarity = _env_number(
            'CLUSTERING_MIN_PAIR_SIMILARITY', similarity, float)
    if topic := _env('CLUSTERING_TOPIC_THRESHOLD'):
        config.clustering.topic_threshold = _env_number('CLUSTERING_TOPIC_THRESHOLD',
                                                        topic, float)
    for obsolete in ('CLUSTERING_EPS', 'CLUSTERING_MIN_SAMPLES'):
        if _env(obsolete):
            logger.warning("%s is obsolete and ignored; use CLUSTERING_THRESHOLD", obsolete)

    # Deduplication
    if threshold := _env('DEDUP_THRESHOLD'):
        config.deduplication.threshold = _env_number('DEDUP_THRESHOLD', threshold, float)
    if action := _env('DEDUP_ACTION'):
        config.deduplication.duplicate_action = action
    if min_tokens := _env('DEDUP_MIN_BODY_TOKENS'):
        config.deduplication.min_body_tokens = _env_number('DEDUP_MIN_BODY_TOKENS',
                                                           min_tokens, int)

    # Scheduling and storage
    if interval := _env('CLUSTERING_INTERVAL'):
        config.scheduling.interval_minutes = _env_number('CLUSTERING_INTERVAL', interval, int)
    if lookback := _env('LOOKBACK_HOURS'):
        config.scheduling.lookback_hours = _env_number('LOOKBACK_HOURS', lookback, int)
    if retention := _env('RETENTION_DAYS'):
        config.storage.retention_days = _env_number('RETENTION_DAYS', retention, int)

    # Web
    if port := _env('WEB_PORT'):
        config.web.port = _env_number('WEB_PORT', port, int)
    if page_size := _env('WEB_PAGE_SIZE'):
        config.web.page_size = _env_number('WEB_PAGE_SIZE', page_size, int)
    if min_sources := _env('WEB_MIN_SOURCES'):
        config.web.min_sources = _env_number('WEB_MIN_SOURCES', min_sources, int)
    if max_stories := _env('WEB_MAX_STORIES'):
        config.web.max_stories = _env_number('WEB_MAX_STORIES', max_stories, int)
    if hosts := _env('WEB_ALLOWED_HOSTS'):
        config.web.allowed_hosts = _split_list(hosts)
    auth = config.web.auth
    if mode := _env('WEB_AUTH_MODE'):
        auth.mode = mode
    if username := _env('WEB_USERNAME'):
        auth.username = username
    if password := _env('WEB_PASSWORD'):
        # Overrides a password_file from config.yaml; WEB_PASSWORD_FILE wins
        auth.password, auth.password_file = password, ''
    if password_file := _env('WEB_PASSWORD_FILE'):
        auth.password_file = password_file
    if header := _env('WEB_AUTH_PROXY_HEADER'):
        auth.proxy_header = header
    if proxies := _env('WEB_TRUSTED_PROXIES'):
        auth.trusted_proxies = _split_list(proxies)

    # Logging
    if level := _env('LOG_LEVEL'):
        config.logging.level = level


def _split_list(value: str) -> list:
    """Split a comma- or space-separated environment variable."""
    return [item for item in re.split(r'[,\s]+', value) if item]


def _read_secret(name: str, path: str) -> str:
    """Read a secret from a file (Docker secret), naming the setting on failure."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            value = f.read().strip()
    except OSError as e:
        raise ConfigError(f"{name}: cannot read '{path}': {e.strerror}") from None
    if not value:
        raise ConfigError(f"{name}: '{path}' is empty")
    return value


def _env_number(name: str, value: str, cast):
    """Convert an environment variable, naming it if that fails."""
    try:
        return cast(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got '{value}'") from None


# Numeric settings: (config path, attribute path, int only)
_NUMERIC_FIELDS = (
    ('clustering.threshold', ('clustering', 'threshold'), False),
    ('clustering.max_features', ('clustering', 'max_features'), True),
    ('clustering.ngram_max', ('clustering', 'ngram_max'), True),
    ('clustering.min_pair_similarity', ('clustering', 'min_pair_similarity'), False),
    ('clustering.topic_threshold', ('clustering', 'topic_threshold'), False),
    ('deduplication.threshold', ('deduplication', 'threshold'), False),
    ('deduplication.min_body_tokens', ('deduplication', 'min_body_tokens'), True),
    ('scheduling.interval_minutes', ('scheduling', 'interval_minutes'), True),
    ('scheduling.batch_size', ('scheduling', 'batch_size'), True),
    ('scheduling.max_entries', ('scheduling', 'max_entries'), True),
    ('scheduling.lookback_hours', ('scheduling', 'lookback_hours'), True),
    ('scheduling.status_sync_minutes', ('scheduling', 'status_sync_minutes'), True),
    ('storage.retention_days', ('storage', 'retention_days'), True),
    ('web.max_stories', ('web', 'max_stories'), True),
    ('web.page_size', ('web', 'page_size'), True),
    ('web.articles_per_story', ('web', 'articles_per_story'), True),
    ('web.min_sources', ('web', 'min_sources'), True),
    ('web.earlier_articles_max', ('web', 'earlier_articles_max'), True),
)


def _check_types(config: Config) -> None:
    """Reject numbers given as strings (e.g. threshold: '0.8') with a clear message."""
    for name, (section, attr), int_only in _NUMERIC_FIELDS:
        value = getattr(getattr(config, section), attr)
        if name == 'clustering.max_features' and value is None:
            continue  # no vocabulary limit
        allowed = (int,) if int_only else (int, float)
        # bool is an int subclass, but 'batch_size: yes' is a mistake
        if isinstance(value, bool) or not isinstance(value, allowed):
            kind = 'an integer' if int_only else 'a number'
            raise ConfigError(f"{name} must be {kind}, got {value!r}")
    for name, value in (('miniflux.url', config.miniflux_url),
                        ('miniflux.public_url', config.miniflux_public_url),
                        ('storage.db_path', config.storage.db_path)):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{name} must be a non-empty string, got {value!r}")


def _string_list(name: str, value) -> list:
    """Accept a YAML list or a comma-separated string; reject anything else."""
    if value is None:
        return []
    if isinstance(value, str):
        return _split_list(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise ConfigError(f"{name} must be a list of strings, got {value!r}")


def _pattern_list(name: str, value) -> list:
    """
    Check a list of regular expressions; a single string is one pattern.

    Unlike _string_list, a string is not split at commas: they are part
    of regular expressions ('{1,3}').
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a list of regular expressions, got {value!r}")
    patterns = [item for item in value if item.strip()]
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ConfigError(f"{name}: '{pattern}' is not a valid regular expression "
                              f"({e})") from None
    return patterns


def _path_sections(value) -> dict:
    """Check web.path_sections: regular expression -> section name."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"web.path_sections must map regular expressions to section "
                          f"names (e.g. 'sport|fussball': Sport), got {value!r}")
    sections = {}
    for pattern, section in value.items():
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError(f"web.path_sections: {pattern!r} is not a regular expression")
        if not isinstance(section, str) or not section.strip() \
                or len(section.strip()) > MAX_SECTION_CHARS:
            raise ConfigError(f"web.path_sections: '{pattern}' needs a section name of at "
                              f"most {MAX_SECTION_CHARS} characters, got {section!r}")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ConfigError(f"web.path_sections: '{pattern}' is not a valid regular "
                              f"expression ({e})") from None
        sections[pattern] = section.strip()
    return sections


def _host_name(name: str, value: str) -> str:
    """
    Normalize an allowed host ('Tower.local:8081', '[fd00::1]') to the name
    the Host header carries: IP addresses in their short form, IDN names as
    punycode (browsers send 'xn--bro-hoa.local' for 'büro.local').
    """
    if '*' in value or value.startswith('.'):
        raise ConfigError(f"{name}: '{value}' - wildcards are not supported, "
                          f"list every host name")
    try:
        # A bare IPv6 address: its colons would be read as a port
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        parts = urlsplit(f'//{value}')
        parts.port  # raises ValueError for 'tower.local:abc'
        host = parts.hostname if '/' not in value else None
    except ValueError:
        host = None
    if host:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            host = None  # empty label as in 'tower..local'
    if not host:
        raise ConfigError(f"{name}: '{value}' is not a host name "
                          f"(e.g. tower.local, without http://)")
    return host


def _zone(name: str) -> Optional[ZoneInfo]:
    """The time zone called name, or None if there is none by that name."""
    try:
        # glibc accepts TZ=':Europe/Berlin' as well
        return ZoneInfo(name.strip().lstrip(':'))
    except (ValueError, KeyError, OSError):
        return None


def resolve_timezone(configured: str) -> ZoneInfo:
    """
    Time zone of the times shown: web.timezone, else the TZ environment
    variable (set in docker-compose.yml), else DEFAULT_TIMEZONE.

    web.timezone is checked by load_config; an unusable TZ (e.g. a POSIX
    rule like 'CET-1CEST') only costs a warning.
    """
    if configured:
        return _zone(configured) or ZoneInfo(DEFAULT_TIMEZONE)
    tz = _env('TZ')
    if tz:
        zone = _zone(tz)
        if zone is not None:
            return zone
        logger.warning("TZ=%r is not a time zone name like %s; Top Stories show times "
                       "in %s (set web.timezone)", tz, DEFAULT_TIMEZONE, DEFAULT_TIMEZONE)
    return ZoneInfo(DEFAULT_TIMEZONE)


def _trusted_network(value: str):
    """Parse one web.auth.trusted_proxies entry; refuse to trust everyone."""
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ConfigError(f"web.auth.trusted_proxies: '{value}' is not an IP "
                          f"address or network (e.g. 172.30.0.10/32)") from None
    if network.prefixlen == 0:
        # Anyone could send the user header and log in as anybody
        raise ConfigError(f"web.auth.trusted_proxies: '{value}' trusts every address; "
                          f"enter the address of your reverse proxy")
    return network


def _validate_web_auth(config: Config) -> None:
    """Check web.auth and web.allowed_hosts; read the password file."""
    web = config.web
    web.allowed_hosts = [_host_name('web.allowed_hosts', host) for host in
                         _string_list('web.allowed_hosts', web.allowed_hosts)]
    auth = web.auth
    auth.trusted_proxies = _string_list('web.auth.trusted_proxies', auth.trusted_proxies)
    networks = [_trusted_network(network) for network in auth.trusted_proxies]
    for name in ('mode', 'username', 'password', 'password_file', 'proxy_header'):
        if not isinstance(getattr(auth, name), str):
            raise ConfigError(f"web.auth.{name} must be a string, "
                              f"got {getattr(auth, name)!r}")

    auth.mode = auth.mode.strip().lower()
    if auth.mode not in WEB_AUTH_MODES:
        raise ConfigError(f"web.auth.mode must be one of {WEB_AUTH_MODES}, got '{auth.mode}'")
    if auth.mode == 'basic':
        if auth.password_file:
            auth.password = _read_secret('web.auth.password_file', auth.password_file)
        if not auth.username or ':' in auth.username:
            raise ConfigError("web.auth.mode 'basic' needs web.auth.username (WEB_USERNAME) "
                              "without ':'")
        if not auth.password:
            raise ConfigError("web.auth.mode 'basic' needs web.auth.password (WEB_PASSWORD) "
                              "or web.auth.password_file (WEB_PASSWORD_FILE)")
    elif auth.mode == 'proxy':
        if not auth.trusted_proxies:
            raise ConfigError("web.auth.mode 'proxy' needs web.auth.trusted_proxies "
                              "(WEB_TRUSTED_PROXIES), the address of your reverse proxy")
        auth.proxy_header = auth.proxy_header.strip()
        # waitress drops headers whose name contains '_' (spoofing guard)
        if not re.fullmatch(r'[A-Za-z0-9-]+', auth.proxy_header):
            raise ConfigError(f"web.auth.proxy_header must be a header name of letters, "
                              f"digits and '-' (e.g. Remote-User), got "
                              f"'{auth.proxy_header}'")
        for network in networks:
            if not network.is_private:
                logger.warning("web.auth.trusted_proxies: %s is not a private network; "
                               "every address in it can log in as anybody by sending "
                               "the %s header", network, auth.proxy_header)


def _validate(config: Config) -> None:
    """Normalize legacy values and reject settings that cannot work."""
    _check_types(config)
    _validate_web_auth(config)
    dedup = config.deduplication
    if dedup.duplicate_action in _LEGACY_DUPLICATE_ACTIONS:
        replacement = _LEGACY_DUPLICATE_ACTIONS[dedup.duplicate_action]
        logger.warning("duplicate_action '%s' is obsolete, using '%s'",
                       dedup.duplicate_action, replacement)
        dedup.duplicate_action = replacement

    if dedup.duplicate_action not in DUPLICATE_ACTIONS:
        raise ConfigError(f"deduplication.duplicate_action must be one of "
                          f"{DUPLICATE_ACTIONS}, got '{dedup.duplicate_action}'")
    if dedup.canonical_strategy not in CANONICAL_STRATEGIES:
        raise ConfigError(f"deduplication.canonical_strategy must be one of "
                          f"{CANONICAL_STRATEGIES}, got '{dedup.canonical_strategy}'")
    if dedup.mark_read_scope not in MARK_READ_SCOPES:
        raise ConfigError(f"deduplication.mark_read_scope must be one of "
                          f"{MARK_READ_SCOPES}, got '{dedup.mark_read_scope}'")
    if not 0.0 < dedup.threshold <= 1.0:
        raise ConfigError("deduplication.threshold must be in (0, 1]")
    if dedup.min_body_tokens < 1:
        raise ConfigError("deduplication.min_body_tokens must be at least 1")
    if not 0.0 < config.clustering.threshold < 1.0:
        raise ConfigError("clustering.threshold must be in (0, 1)")
    if config.clustering.max_features is not None and config.clustering.max_features < 1:
        raise ConfigError("clustering.max_features must be at least 1")
    if not 1 <= config.clustering.ngram_max <= MAX_NGRAM:
        raise ConfigError(f"clustering.ngram_max must be between 1 and {MAX_NGRAM}, "
                          f"got {config.clustering.ngram_max}")
    if not 0.0 <= config.clustering.min_pair_similarity < 1.0:
        raise ConfigError("clustering.min_pair_similarity must be in [0, 1)")
    topic = config.clustering.topic_threshold
    if (topic != 0 and topic == ClusteringConfig().topic_threshold
            and not _env('CLUSTERING_TOPIC_THRESHOLD')
            and not config.clustering.threshold < topic):
        # Only the default (or the shipped config.yaml) sets it: a threshold
        # that used to be valid must not stop the service after an upgrade
        logger.warning("clustering.threshold %s is not below the default "
                       "clustering.topic_threshold %s: topics are off. Set "
                       "CLUSTERING_TOPIC_THRESHOLD above the threshold, or to 0",
                       config.clustering.threshold, topic)
        config.clustering.topic_threshold = topic = 0
    if topic != 0 and not config.clustering.threshold < topic < 1.0:
        raise ConfigError(f"clustering.topic_threshold must be above clustering.threshold "
                          f"({config.clustering.threshold}) and below 1, or 0 (off); "
                          f"got {topic}")
    config.clustering.noise_title_patterns = _pattern_list(
        'clustering.noise_title_patterns', config.clustering.noise_title_patterns)
    config.web.exclude_patterns = _pattern_list('web.exclude_patterns',
                                                config.web.exclude_patterns)
    if not 0 < config.miniflux_public_port < 65536:
        raise ConfigError("miniflux.public_port must be a TCP port (1-65535)")
    if not isinstance(config.miniflux_respect_hide_globally, bool):
        raise ConfigError(f"miniflux.respect_hide_globally must be true or false, got "
                          f"{config.miniflux_respect_hide_globally!r}")
    config.web.path_sections = _path_sections(config.web.path_sections)
    if config.web.min_sources < 1:
        raise ConfigError("web.min_sources must be at least 1")
    if config.web.max_stories < 1:
        raise ConfigError("web.max_stories must be at least 1")
    if config.web.page_size < 1:
        raise ConfigError("web.page_size must be at least 1")
    if not isinstance(config.web.timezone, str) or (
            config.web.timezone and _zone(config.web.timezone) is None):
        raise ConfigError(f"web.timezone must be a time zone name like "
                          f"'{DEFAULT_TIMEZONE}', got {config.web.timezone!r}")
    if config.web.articles_per_story < 0:
        raise ConfigError("web.articles_per_story must not be negative")
    if config.web.earlier_articles_max < 0:
        raise ConfigError("web.earlier_articles_max must not be negative")

    scheduling = config.scheduling
    if scheduling.interval_minutes < 1:
        raise ConfigError("scheduling.interval_minutes must be at least 1")
    if not 1 <= scheduling.batch_size <= MAX_BATCH_SIZE:
        raise ConfigError(f"scheduling.batch_size must be between 1 and {MAX_BATCH_SIZE} "
                          f"(Miniflux limit), got {scheduling.batch_size}")
    if not 1 <= scheduling.max_entries <= MAX_ENTRIES_LIMIT:
        raise ConfigError(f"scheduling.max_entries must be between 1 and "
                          f"{MAX_ENTRIES_LIMIT}, got {scheduling.max_entries}")
    if scheduling.lookback_hours < 1:
        raise ConfigError("scheduling.lookback_hours must be at least 1")
    if scheduling.status_sync_minutes < 0:
        raise ConfigError("scheduling.status_sync_minutes must not be negative (0 = off)")
    retention_days = config.storage.retention_days
    if retention_days < 1:
        raise ConfigError("storage.retention_days must be at least 1")
    if retention_days * 24 < scheduling.lookback_hours:
        # Cleanup would delete stories that are still inside the window
        raise ConfigError(f"storage.retention_days ({retention_days}) must cover "
                          f"scheduling.lookback_hours ({scheduling.lookback_hours} h)")

    if (not isinstance(config.logging.level, str)
            or getattr(logging, config.logging.level.upper(), None) is None):
        raise ConfigError(f"Unknown log level '{config.logging.level}' (logging.level)")

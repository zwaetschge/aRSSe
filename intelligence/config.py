"""
Configuration module for aRSSe Intelligence Layer.

Handles loading and validation of configuration from environment
variables and YAML configuration file.

Precedence: defaults < config.yaml < environment variables.
Empty environment variables are ignored, so docker-compose can pass
variables through without clobbering values from config.yaml.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

logger = logging.getLogger('arsse-intelligence')

DUPLICATE_ACTIONS = ('none', 'mark_read')
CANONICAL_STRATEGIES = ('longest', 'source_priority', 'newest')

# Values accepted by older versions of config.yaml
_LEGACY_DUPLICATE_ACTIONS = {'tag': 'none', 'hide': 'mark_read'}
# DBSCAN settings from before the switch to average-linkage clustering
_LEGACY_CLUSTERING_KEYS = ('eps', 'min_samples', 'metric')


@dataclass
class ClusteringConfig:
    """Configuration for average-linkage clustering."""
    # Maximum average cosine distance between the articles of one story
    threshold: float = 0.75
    max_features: int = 5000
    language: str = "german"
    stemming: bool = True


@dataclass
class DeduplicationConfig:
    """Configuration for duplicate detection."""
    threshold: float = 0.85
    canonical_strategy: str = "longest"
    source_scores: dict = field(default_factory=dict)
    duplicate_action: str = "mark_read"


@dataclass
class SchedulingConfig:
    """Configuration for scheduling."""
    interval_minutes: int = 30
    batch_size: int = 250
    max_entries: int = 2000
    lookback_hours: int = 24


@dataclass
class StorageConfig:
    """Configuration for the local story database."""
    db_path: str = "/app/data/arsse.db"
    retention_days: int = 7


@dataclass
class WebConfig:
    """Configuration for the Top Stories web interface."""
    host: str = "0.0.0.0"
    port: int = 8081
    max_stories: int = 50
    articles_per_story: int = 6
    # Stories covered by fewer feeds are not shown (filters feed-internal
    # series and advertising that only resemble each other)
    min_sources: int = 2


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
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    deduplication: DeduplicationConfig = field(default_factory=DeduplicationConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(config_path: Optional[str] = None) -> Config:
    """
    Load configuration from YAML file and environment variables.

    Environment variables take precedence over YAML configuration.

    Args:
        config_path: Path to YAML configuration file.

    Returns:
        Config object with all settings.

    Raises:
        ValueError: If a setting is invalid.
    """
    config = Config()

    if config_path and os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)

        if yaml_config:
            _apply_yaml_config(config, yaml_config)

    _apply_env_config(config)
    _validate(config)

    return config


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
    yaml_config = {**yaml_config, 'web': web, 'clustering': clustering}

    for section in ('clustering', 'deduplication', 'scheduling',
                    'storage', 'web', 'logging'):
        _apply_section(getattr(config, section), yaml_config.get(section))

    if 'tagging' in yaml_config:
        logger.warning("The 'tagging' section is obsolete: Miniflux cannot "
                       "store tags via its API. Stories are kept locally.")

    if miniflux := yaml_config.get('miniflux'):
        config.miniflux_url = miniflux.get('url', config.miniflux_url)
        config.miniflux_public_url = miniflux.get('public_url', config.miniflux_public_url)


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
    if public_url := _env('MINIFLUX_PUBLIC_URL'):
        config.miniflux_public_url = public_url

    # Clustering
    if threshold := _env('CLUSTERING_THRESHOLD'):
        config.clustering.threshold = float(threshold)
    for obsolete in ('CLUSTERING_EPS', 'CLUSTERING_MIN_SAMPLES'):
        if _env(obsolete):
            logger.warning("%s is obsolete and ignored; use CLUSTERING_THRESHOLD", obsolete)

    # Deduplication
    if threshold := _env('DEDUP_THRESHOLD'):
        config.deduplication.threshold = float(threshold)
    if action := _env('DEDUP_ACTION'):
        config.deduplication.duplicate_action = action

    # Scheduling
    if interval := _env('CLUSTERING_INTERVAL'):
        config.scheduling.interval_minutes = int(interval)

    # Web
    if port := _env('WEB_PORT'):
        config.web.port = int(port)

    # Logging
    if level := _env('LOG_LEVEL'):
        config.logging.level = level


def _validate(config: Config) -> None:
    """Normalize legacy values and reject settings that cannot work."""
    dedup = config.deduplication
    if dedup.duplicate_action in _LEGACY_DUPLICATE_ACTIONS:
        replacement = _LEGACY_DUPLICATE_ACTIONS[dedup.duplicate_action]
        logger.warning("duplicate_action '%s' is obsolete, using '%s'",
                       dedup.duplicate_action, replacement)
        dedup.duplicate_action = replacement

    if dedup.duplicate_action not in DUPLICATE_ACTIONS:
        raise ValueError(f"duplicate_action must be one of {DUPLICATE_ACTIONS}, "
                         f"got '{dedup.duplicate_action}'")
    if dedup.canonical_strategy not in CANONICAL_STRATEGIES:
        raise ValueError(f"canonical_strategy must be one of {CANONICAL_STRATEGIES}, "
                         f"got '{dedup.canonical_strategy}'")
    if not 0.0 < dedup.threshold <= 1.0:
        raise ValueError("deduplication.threshold must be in (0, 1]")
    if not 0.0 < config.clustering.threshold < 1.0:
        raise ValueError("clustering.threshold must be in (0, 1)")
    if config.web.min_sources < 1:
        raise ValueError("web.min_sources must be at least 1")
    if config.scheduling.interval_minutes < 1:
        raise ValueError("scheduling.interval_minutes must be at least 1")
    if config.scheduling.batch_size < 1 or config.scheduling.max_entries < 1:
        raise ValueError("scheduling.batch_size and max_entries must be positive")
    if getattr(logging, config.logging.level.upper(), None) is None:
        raise ValueError(f"Unknown log level '{config.logging.level}'")

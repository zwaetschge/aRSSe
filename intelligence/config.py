"""
Configuration module for aRSSe Intelligence Layer.

Handles loading and validation of configuration from environment
variables and YAML configuration file.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ClusteringConfig:
    """Configuration for DBSCAN clustering."""
    eps: float = 0.4
    min_samples: int = 2
    metric: str = "cosine"
    max_features: int = 5000
    language: str = "german"


@dataclass
class DeduplicationConfig:
    """Configuration for duplicate detection."""
    threshold: float = 0.85
    canonical_strategy: str = "longest"
    source_scores: dict = field(default_factory=dict)
    duplicate_action: str = "tag"
    duplicate_tag: str = "duplicate"


@dataclass
class SchedulingConfig:
    """Configuration for scheduling."""
    interval_minutes: int = 30
    batch_size: int = 1000
    lookback_hours: int = 24


@dataclass
class TaggingConfig:
    """Configuration for tagging behavior."""
    cluster_prefix: str = "story"
    max_tags: int = 5
    preserve_existing: bool = True


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
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    deduplication: DeduplicationConfig = field(default_factory=DeduplicationConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    tagging: TaggingConfig = field(default_factory=TaggingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(config_path: Optional[str] = None) -> Config:
    """
    Load configuration from YAML file and environment variables.

    Environment variables take precedence over YAML configuration.

    Args:
        config_path: Path to YAML configuration file.

    Returns:
        Config object with all settings.
    """
    config = Config()

    # Load from YAML if provided
    if config_path and os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)

        if yaml_config:
            _apply_yaml_config(config, yaml_config)

    # Override with environment variables
    _apply_env_config(config)

    return config


def _apply_yaml_config(config: Config, yaml_config: dict) -> None:
    """Apply YAML configuration to Config object."""
    if 'clustering' in yaml_config:
        c = yaml_config['clustering']
        config.clustering.eps = c.get('eps', config.clustering.eps)
        config.clustering.min_samples = c.get('min_samples', config.clustering.min_samples)
        config.clustering.metric = c.get('metric', config.clustering.metric)
        config.clustering.max_features = c.get('max_features', config.clustering.max_features)
        config.clustering.language = c.get('language', config.clustering.language)

    if 'deduplication' in yaml_config:
        d = yaml_config['deduplication']
        config.deduplication.threshold = d.get('threshold', config.deduplication.threshold)
        config.deduplication.canonical_strategy = d.get('canonical_strategy', config.deduplication.canonical_strategy)
        config.deduplication.source_scores = d.get('source_scores', config.deduplication.source_scores)
        config.deduplication.duplicate_action = d.get('duplicate_action', config.deduplication.duplicate_action)
        config.deduplication.duplicate_tag = d.get('duplicate_tag', config.deduplication.duplicate_tag)

    if 'scheduling' in yaml_config:
        s = yaml_config['scheduling']
        config.scheduling.interval_minutes = s.get('interval_minutes', config.scheduling.interval_minutes)
        config.scheduling.batch_size = s.get('batch_size', config.scheduling.batch_size)
        config.scheduling.lookback_hours = s.get('lookback_hours', config.scheduling.lookback_hours)

    if 'tagging' in yaml_config:
        t = yaml_config['tagging']
        config.tagging.cluster_prefix = t.get('cluster_prefix', config.tagging.cluster_prefix)
        config.tagging.max_tags = t.get('max_tags', config.tagging.max_tags)
        config.tagging.preserve_existing = t.get('preserve_existing', config.tagging.preserve_existing)

    if 'logging' in yaml_config:
        log = yaml_config['logging']
        config.logging.level = log.get('level', config.logging.level)
        config.logging.json_format = log.get('json_format', config.logging.json_format)
        config.logging.file = log.get('file', config.logging.file)


def _apply_env_config(config: Config) -> None:
    """Apply environment variable overrides."""
    # Miniflux connection
    config.miniflux_url = os.getenv('MINIFLUX_URL', config.miniflux_url)
    config.miniflux_api_key = os.getenv('MINIFLUX_API_KEY', config.miniflux_api_key)

    # Clustering
    if eps := os.getenv('CLUSTERING_EPS'):
        config.clustering.eps = float(eps)
    if min_samples := os.getenv('CLUSTERING_MIN_SAMPLES'):
        config.clustering.min_samples = int(min_samples)

    # Deduplication
    if threshold := os.getenv('DEDUP_THRESHOLD'):
        config.deduplication.threshold = float(threshold)

    # Scheduling
    if interval := os.getenv('CLUSTERING_INTERVAL'):
        config.scheduling.interval_minutes = int(interval)

    # Logging
    if level := os.getenv('LOG_LEVEL'):
        config.logging.level = level

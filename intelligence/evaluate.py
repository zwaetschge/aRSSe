#!/usr/bin/env python3
"""
Compare clustering thresholds on the articles currently in Miniflux.

Read-only: nothing is written to Miniflux or the story database.

Usage (inside the container):
    docker compose exec intelligence python evaluate.py
    docker compose exec intelligence python evaluate.py 0.7 0.75 0.8 --show 10
"""

import argparse
import logging
import os
import random
import sys

from config import load_config
from news_clustering import NewsClusterer, setup_logging


class _NoStore:
    """The clusterer needs a store only for full cycles, not for _cluster()."""


def describe(clusters: list, entries: dict, min_sources: int, show: int) -> None:
    def sources(cluster):
        return {entries[i]['feed_id'] for i in cluster.entry_ids}

    shown = [c for c in clusters if len(sources(c)) >= min_sources]
    in_stories = sum(len(c.entry_ids) for c in shown)
    sizes = sorted((len(c.entry_ids) for c in shown), reverse=True)
    print(f"  {len(shown)} stories (>= {min_sources} sources), "
          f"{in_stories}/{len(entries)} articles in stories, largest: {sizes[:8]}")

    # Largest stories reveal chaining, random ones reveal false pairs
    shown.sort(key=lambda c: len(c.entry_ids), reverse=True)
    sample = shown[:show // 2] + random.sample(shown[show // 2:], min(show - show // 2,
                                                                     len(shown[show // 2:])))
    for cluster in sample:
        print(f"  -- {len(cluster.entry_ids)} articles, {len(sources(cluster))} sources")
        for entry_id in cluster.entry_ids[:6]:
            entry = entries[entry_id]
            feed = (entry.get('feed') or {}).get('title', '')[:14]
            print(f"       {feed:14} {entry.get('title', '')[:90]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('thresholds', nargs='*', type=float,
                        default=[0.65, 0.7, 0.75, 0.8])
    parser.add_argument('--show', type=int, default=6,
                        help='stories to print per threshold (half largest, half random)')
    parser.add_argument('--seed', type=int, default=1)
    args = parser.parse_args()

    config = load_config(os.getenv('ARSSE_CONFIG', '/app/config.yaml'))
    config.logging.level = 'WARNING'
    setup_logging(config)
    random.seed(args.seed)

    clusterer = NewsClusterer(config, _NoStore())
    entries = clusterer._fetch_recent_entries().entries
    if len(entries) < 2:
        sys.exit(f"Only {len(entries)} articles in the last "
                 f"{config.scheduling.lookback_hours}h, nothing to compare.")

    by_id = {e['id']: e for e in entries}
    feeds = len({e['feed_id'] for e in entries})
    print(f"{len(entries)} articles from {feeds} feeds "
          f"(last {config.scheduling.lookback_hours}h), "
          f"configured threshold: {config.clustering.threshold}\n")

    for threshold in args.thresholds:
        config.clustering.threshold = threshold
        clusters = clusterer._cluster([dict(e) for e in entries])
        print(f"threshold={threshold}")
        describe(clusters, by_id, config.web.min_sources, args.show)
        print()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    main()

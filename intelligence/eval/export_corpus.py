#!/usr/bin/env python3
"""
Save the articles of the lookback window from Miniflux for evaluate.py.

Read-only towards Miniflux. The file holds the publishers' texts: keep it
to yourself and out of git (eval/corpus.json is ignored).

Usage (from the intelligence directory, MINIFLUX_URL/MINIFLUX_API_KEY set):
    python eval/export_corpus.py --out eval/corpus.json
Inside the container, write to the data volume:
    docker compose exec intelligence python eval/export_corpus.py --out /app/data/corpus.json
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from config import load_config  # noqa: E402
from news_clustering import NewsClusterer  # noqa: E402

# What clustering, the story database and the gold labels (URL) need
FIELDS = ('id', 'feed_id', 'status', 'title', 'content', 'url', 'published_at', 'created_at')
FEED_FIELDS = ('id', 'title', 'site_url')


class _NoStore:
    """Fetching needs no story database."""


def export(clusterer: NewsClusterer) -> list:
    fetch = clusterer._fetch_recent_entries()
    if not fetch.complete:
        logging.warning("The window holds more than scheduling.max_entries articles; "
                        "only the newest were saved")
    entries = []
    for entry in sorted(fetch.entries, key=lambda e: e['id']):
        row = {key: entry.get(key) for key in FIELDS}
        feed = entry.get('feed') or {}
        row['feed'] = {key: feed.get(key) for key in FEED_FIELDS}
        row['feed_id'] = row['feed_id'] or feed.get('id')
        entries.append(row)
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', default=str(HERE / 'corpus.json'),
                        help='output file (default: eval/corpus.json)')
    parser.add_argument('--config', default=os.getenv('ARSSE_CONFIG', '/app/config.yaml'))
    parser.add_argument('--hours', type=int, help='override scheduling.lookback_hours')
    args = parser.parse_args()

    config_path = args.config if os.path.exists(args.config) else str(HERE.parent / 'config.yaml')
    config = load_config(config_path)
    if args.hours:
        config.scheduling.lookback_hours = args.hours
    entries = export(NewsClusterer(config, _NoStore()))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False)
    print(f"{len(entries)} articles of the last {config.scheduling.lookback_hours}h "
          f"saved to {out}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    main()

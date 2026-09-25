#!/usr/bin/env python3
"""
Compare clustering thresholds on the articles currently in Miniflux.

Read-only: nothing is written to Miniflux or the story database.

Usage (inside the container):
    docker compose exec intelligence python evaluate.py
    docker compose exec intelligence python evaluate.py 0.7 0.75 0.8 --show 10

With a saved corpus (eval/export_corpus.py) it clusters the saved articles
instead; with gold labels it scores the clustering instead of printing
samples (see docs/ARCHITECTURE.md):
    python evaluate.py --corpus eval/corpus.json --replay 25
    python evaluate.py --corpus eval/corpus.json --gold eval/gold.json
    python evaluate.py --corpus eval/corpus.json --gold eval/gold.json --replay 25
    python evaluate.py --corpus eval/corpus.json --gold eval/gold.json \\
        --ngram-max 2 --min-pair-similarity 0
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

from config import LEGACY_CONFIG_PATH, USER_CONFIG_PATH, load_config
from news_clustering import NewsClusterer, setup_logging

DEFAULT_THRESHOLDS = [0.65, 0.7, 0.75, 0.8]


class _NoStore:
    """The clusterer needs a store only for full cycles, not for _cluster()."""


class _NoClient:
    """Stands in for Miniflux when the articles come from a saved corpus."""


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


def score(clusterer: NewsClusterer, entries: list, gold, show_junk: bool = False) -> dict:
    """Cluster entries once and score the result against gold labels."""
    from eval.metrics import pairwise_scores, story_quality

    min_sources = clusterer.config.web.min_sources
    started = time.perf_counter()
    clusters = clusterer._cluster([dict(e) for e in entries])
    seconds = time.perf_counter() - started
    url_of = {e['id']: e.get('url') or '' for e in entries}
    feed_of = {e['id']: e.get('feed_id') for e in entries}
    groups = [c.entry_ids for c in clusters]
    row = pairwise_scores(gold, groups, url_of)
    row.update(story_quality(gold, groups, url_of, feed_of, min_sources))
    shown = [c for c in clusters
             if len({feed_of.get(i) for i in c.entry_ids}) >= min_sources]
    row['noise_headlines'] = sum(1 for c in shown if c.headline_entry_id in c.noise_ids)
    row['seconds'] = seconds
    if show_junk:
        titles = {e['id']: e.get('title') or '' for e in entries}
        for group in row['junk_groups']:
            print('    junk: ' + ' || '.join(titles[i][:45] for i in group[:4]))
    return row


def score_thresholds(clusterer: NewsClusterer, entries: list, gold, args) -> None:
    """Print the score table, one row per threshold."""
    config = clusterer.config
    print(" threshold |     P     R    F1 | shown  pure mixed  junk | noise heads | seconds")
    unlabelled = 0
    for threshold in args.thresholds:
        config.clustering.threshold = threshold
        row = score(clusterer, entries, gold, show_junk=args.show_junk)
        unlabelled = max(unlabelled, row['unlabelled'])
        print(f"   {threshold:5.2f}   | {row['precision']:.3f} {row['recall']:.3f} "
              f"{row['f1']:.3f} | {row['shown']:5} {row['pure']:5} {row['mixed']:5} "
              f"{row['junk']:5} | {row['noise_headlines']:11} | {row['seconds']:7.2f}")
    print("\nP/R/F1: pairs of labelled articles in one story (must-link vs. cannot-link); "
          "shown: stories with >= min_sources feeds;\njunk: shown stories without a "
          "real same-event pair across feeds; noise heads: shown stories headed by "
          "a noise title")
    if unlabelled:
        print(f"Up to {unlabelled} shown stories have no labelled pair and count as "
              f"neither pure, mixed nor junk: label their articles in the gold file.")


def evaluate_corpus(config, args) -> None:
    """Score thresholds (and optionally replay stability) on a saved corpus."""
    from eval.metrics import Gold, load_gold
    from eval.replay import published, replay, window

    with open(args.corpus, encoding='utf-8') as f:
        corpus = json.load(f)
    gold = load_gold(args.gold) if args.gold else Gold()
    end = gold.window_end or (max(filter(None, map(published, corpus)))
                              + timedelta(minutes=1))
    hours = gold.window_hours or config.scheduling.lookback_hours
    config.scheduling.lookback_hours = hours
    entries = window(corpus, end, hours)
    labelled = sum(1 for e in entries if e.get('url') in gold.labels)
    print(f"{len(entries)} articles from {len({e.get('feed_id') for e in entries})} feeds "
          f"({hours}h up to {end:%Y-%m-%d %H:%M} UTC)"
          + (f", {labelled} labelled" if args.gold else ""))
    if args.gold and not labelled:
        sys.exit("No article of the corpus is labelled in the gold file "
                 "(labels are keyed by entry URL).")
    print(f"ngram_max={config.clustering.ngram_max} "
          f"min_pair_similarity={config.clustering.min_pair_similarity} "
          f"min_sources={config.web.min_sources}\n")

    clusterer = NewsClusterer(config, _NoStore(), client=_NoClient())
    if args.gold:
        score_thresholds(clusterer, entries, gold, args)
    else:
        # Nothing to score against: show the stories, as without --corpus
        by_id = {e['id']: e for e in entries}
        for threshold in args.thresholds:
            config.clustering.threshold = threshold
            print(f"threshold={threshold}")
            describe(clusterer._cluster([dict(e) for e in entries]), by_id,
                     config.web.min_sources, args.show)
            print()

    if args.replay:
        for threshold in args.thresholds:
            config.clustering.threshold = threshold
            result = replay(clusterer, corpus, end, runs=args.replay,
                            step_minutes=config.scheduling.interval_minutes)
            noise = sum(1 for _, title, _ in result.final_page
                        if any(p.search(title or '') for p in clusterer.noise_patterns))
            print(f"\nreplay threshold={threshold}: {result.runs} runs every "
                  f"{config.scheduling.interval_minutes} min, "
                  f"{result.previous_shown} story transitions")
            print(f"  id_kept          {result.rate('id_kept'):6.1%}")
            print(f"  headline_changed {result.rate('headline_changed'):6.1%} of kept")
            print(f"  vanished         {result.rate('vanished'):6.1%}")
            print(f"  top-10 overlap   {result.top10_overlap / max(result.transitions, 1):.2f}/10")
            print(f"  noise headlines  {noise}/{len(result.final_page)} on the last front page")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('thresholds', nargs='*', type=float,
                        help=f'default: {DEFAULT_THRESHOLDS}, with --corpus the '
                             f'configured threshold')
    parser.add_argument('--show', type=int, default=6,
                        help='stories to print per threshold (half largest, half random; '
                             'not with --gold)')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--config', default=os.getenv('ARSSE_CONFIG', '/app/config.yaml'),
                        help='config.yaml (default: $ARSSE_CONFIG, /app/config.yaml or '
                             'the one next to this script)')
    parser.add_argument('--user-config',
                        default=os.getenv('ARSSE_USER_CONFIG', USER_CONFIG_PATH),
                        help='own settings on top of --config (default: $ARSSE_USER_CONFIG '
                             f'or {USER_CONFIG_PATH}; skipped if missing)')
    parser.add_argument('--corpus', help='saved articles (eval/export_corpus.py) instead '
                                         'of Miniflux')
    parser.add_argument('--gold', help='gold labels (eval/gold.json); needs --corpus')
    parser.add_argument('--replay', type=int, default=0, metavar='RUNS',
                        help='also replay RUNS clustering cycles through the story '
                             'database and measure stability; needs --corpus')
    parser.add_argument('--ngram-max', type=int, help='override clustering.ngram_max')
    parser.add_argument('--min-pair-similarity', type=float,
                        help='override clustering.min_pair_similarity')
    parser.add_argument('--show-junk', action='store_true',
                        help='with --gold: print the junk stories')
    args = parser.parse_args()
    if (args.gold or args.replay) and not args.corpus:
        parser.error('--gold and --replay need --corpus')

    config_path = args.config
    if not os.path.exists(config_path):
        config_path = str(Path(__file__).resolve().parent / 'config.yaml')
    # Like the service: old edits of the checkout's config.yaml still apply
    config = load_config(config_path, args.user_config,
                         os.getenv('ARSSE_LEGACY_CONFIG', LEGACY_CONFIG_PATH))
    if args.ngram_max is not None:
        config.clustering.ngram_max = args.ngram_max
    if args.min_pair_similarity is not None:
        config.clustering.min_pair_similarity = args.min_pair_similarity
    config.logging.level = 'WARNING'
    setup_logging(config)
    random.seed(args.seed)

    if args.corpus:
        args.thresholds = args.thresholds or [config.clustering.threshold]
        evaluate_corpus(config, args)
        return
    args.thresholds = args.thresholds or DEFAULT_THRESHOLDS

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

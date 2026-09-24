#!/usr/bin/env python3
"""
aRSSe Intelligence Layer - News Clustering and Deduplication

This module implements the core clustering and deduplication logic
that transforms a simple RSS reader into a Google News-like aggregator.

Architecture:
    1. Extraction: Fetch recent articles from Miniflux API
    2. Preprocessing: HTML stripping, normalization, stemming
    3. Vectorization: TF-IDF transformation
    4. Clustering: average-linkage agglomerative clustering for topic grouping
    5. Deduplication: Near-duplicate detection within clusters
    6. Persistence: Stories go to a local SQLite database (Miniflux cannot
       store tags via its API); duplicates are optionally marked as read
"""

import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import miniflux
from bs4 import BeautifulSoup
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_distances, cosine_similarity

from config import Config, load_config
from store import (MAX_TITLE_CHARS, ClusterResult, FetchResult, StoreTooNewError,
                   StoryStore)

logger = logging.getLogger('arsse-intelligence')

SNIPPET_LENGTH = 280
# Long articles add little signal but cost a lot of vectorization time
MAX_CONTENT_CHARS = 5000
# HTML beyond this is not even parsed: a broken or hostile feed item of
# several MB would cost seconds and hundreds of MB per run. Real items stay
# far below (at most 6 KB in the calibration corpus).
MAX_HTML_CHARS = 200_000
# Inline data: URIs (Miniflux keeps data:image/*) carry no text but can fill
# the whole HTML budget and push the article text past MAX_HTML_CHARS. The
# lookbehind anchors a match at the start of an unbroken run, so every run
# is scanned once and the pattern stays linear on hostile input.
_DATA_URI_RE = re.compile(r'(?<![^\s"\'<>()])data:[^\s"\'<>()]{250,}', re.IGNORECASE)
# First retry delay after a failed cycle; doubles up to the normal interval
MIN_RETRY_SECONDS = 10

_TOKEN_RE = re.compile(r'\b\w\w+\b')


class NewsClusterer:
    """
    Main clustering engine that groups articles by topic and detects duplicates.

    This class implements a simplified version of Google News' clustering logic
    using TF-IDF vectorization and average-linkage clustering. Unlike DBSCAN,
    average linkage cannot chain unrelated topics through a shared keyword:
    every story must be close to all of its articles on average.
    """

    def __init__(self, config: Config, store: StoryStore,
                 client: Optional[miniflux.Client] = None):
        """
        Initialize the clusterer with configuration.

        Args:
            config: Configuration object with clustering parameters.
            store: Local story database.
            client: Miniflux client; created from config if omitted.
        """
        self.config = config
        self.store = store
        self.client = client or self._create_client()
        self.stopwords = set(self._get_stopwords())
        self.stemmer = self._get_stemmer()

        logger.info("NewsClusterer initialized with threshold=%.2f, stemming=%s",
                    config.clustering.threshold, self.stemmer is not None)

    def _create_client(self) -> miniflux.Client:
        """Create the Miniflux API client."""
        if not self.config.miniflux_api_key:
            raise ValueError("MINIFLUX_API_KEY not set. Please configure the API key.")

        return miniflux.Client(
            self.config.miniflux_url,
            api_key=self.config.miniflux_api_key,
            timeout=60,
        )

    def _get_stopwords(self) -> list:
        """Get stopwords for the configured language."""
        try:
            from nltk.corpus import stopwords
            return stopwords.words(self.config.clustering.language)
        except (LookupError, OSError):
            logger.warning("Could not load stopwords for %s, using empty list",
                           self.config.clustering.language)
            return []

    def _get_stemmer(self):
        """Get a Snowball stemmer for the configured language, if enabled."""
        if not self.config.clustering.stemming:
            return None
        try:
            from nltk.stem.snowball import SnowballStemmer
            return SnowballStemmer(self.config.clustering.language)
        except ValueError:
            logger.warning("No stemmer available for %s, stemming disabled",
                           self.config.clustering.language)
            return None

    def _tokenize(self, text: str) -> list:
        """Split text into stopword-free, stemmed tokens."""
        tokens = [t for t in _TOKEN_RE.findall(text) if t not in self.stopwords]
        if self.stemmer:
            tokens = [self.stemmer.stem(t) for t in tokens]
        return tokens

    def _build_vectorizer(self) -> TfidfVectorizer:
        return TfidfVectorizer(
            max_features=self.config.clustering.max_features,
            tokenizer=self._tokenize,
            token_pattern=None,
            lowercase=False,  # _preprocess_entry already lowercases
            ngram_range=(1, 2),  # Unigrams and bigrams
            min_df=2,  # Ignore terms that appear in less than 2 documents
            max_df=0.95,  # Ignore terms that appear in more than 95% of documents
            sublinear_tf=True,
        )

    def run_clustering_cycle(self) -> dict:
        """
        Execute one complete clustering cycle.

        Returns:
            Dictionary with statistics about the clustering run.
        """
        start_time = time.time()
        stats = {
            'articles_processed': 0,
            'clusters_found': 0,
            'duplicates_detected': 0,
            'marked_read': 0,
            'errors': 0,
        }

        try:
            fetch = self._fetch_recent_entries()
            entries = fetch.entries
            stats['articles_processed'] = len(entries)
            logger.info("Processing %d articles", len(entries))

            clusters = self._cluster(entries)
            stats['clusters_found'] = len(clusters)
            stats['duplicates_detected'] = sum(len(c.duplicate_ids) for c in clusters)

            self.store.save_run(entries, clusters, fetch, self.config.web.min_sources)
            self.store.cleanup(self.config.storage.retention_days)

            if self.config.deduplication.duplicate_action == 'mark_read':
                stats['marked_read'] = self._mark_duplicates_read(entries, clusters)

            self.store.set_meta('last_success', datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.exception("Error during clustering cycle: %s", e)
            stats['errors'] += 1

        elapsed = time.time() - start_time
        stats['duration_seconds'] = round(elapsed, 2)
        try:
            self.store.set_meta('last_stats', stats)
        except Exception as e:  # e.g. disk full; must not kill the scheduler
            logger.error("Failed to store run statistics: %s", e)
        logger.info("Clustering cycle completed in %.2f seconds: %s", elapsed, stats)
        return stats

    def _cluster(self, entries: list) -> list:
        """Group entries into clusters and detect duplicates within each."""
        texts = [self._preprocess_entry(e) for e in entries]
        valid_indices = [i for i, t in enumerate(texts) if t]
        if len(valid_indices) < 2:
            logger.info("Not enough valid articles for clustering (%d)", len(valid_indices))
            return []

        valid_entries = [entries[i] for i in valid_indices]
        valid_texts = [texts[i] for i in valid_indices]

        try:
            tfidf_matrix = self._build_vectorizer().fit_transform(valid_texts)
        except ValueError as e:
            logger.warning("Vectorization failed: %s", e)
            return []

        labels = AgglomerativeClustering(
            n_clusters=None,
            metric='precomputed',
            linkage='average',
            distance_threshold=self.config.clustering.threshold,
        ).fit(cosine_distances(tfidf_matrix)).labels_

        cluster_map = {}
        for idx, label in enumerate(labels):
            cluster_map.setdefault(label, []).append(idx)
        # Articles nobody else wrote about stay out of stories
        cluster_map = {k: v for k, v in cluster_map.items() if len(v) >= 2}

        clusters = []
        for member_indices in cluster_map.values():
            cluster_entries = [valid_entries[i] for i in member_indices]
            duplicates = self._detect_duplicates(cluster_entries,
                                                 [valid_texts[i] for i in member_indices])
            headline_idx = self._select_canonical(cluster_entries,
                                                  list(range(len(cluster_entries))))
            headline_id = cluster_entries[headline_idx]['id']
            # The headline is shown as the story; it must never be marked read
            duplicates.discard(headline_id)
            clusters.append(ClusterResult(
                entry_ids=[e['id'] for e in cluster_entries],
                headline_entry_id=headline_id,
                duplicate_ids=duplicates,
            ))

        logger.info("Found %d clusters", len(clusters))
        return clusters

    def _fetch_recent_entries(self) -> FetchResult:
        """
        Fetch all entries published within the lookback window.

        Pages by entry ID (keyset): each page asks for IDs below the
        smallest one seen so far. Offset paging over published_at returned
        the same entry on two pages whenever publication dates tie (whole
        minutes, feeds without dates) or Miniflux stored new entries while
        paging. New entries always get higher IDs and never shift a page.
        """
        fetched_at = datetime.now(timezone.utc)
        cutoff = fetched_at - timedelta(hours=self.config.scheduling.lookback_hours)
        batch_size = self.config.scheduling.batch_size
        max_entries = self.config.scheduling.max_entries

        # Keyed by ID: an entry must never reach save_run twice
        entries = {}
        before_id = None
        # False once entries of the window may be missing from the result
        complete = True
        while True:
            if len(entries) >= max_entries:
                complete = False
                logger.warning("Reached max_entries=%d, older articles are skipped",
                               max_entries)
                break
            limit = min(batch_size, max_entries - len(entries))
            params = {}
            if before_id is not None:
                params['before_entry_id'] = before_id
            # Read entries are included so stories stay intact after reading
            page = self.client.get_entries(
                status=['unread', 'read'],
                published_after=int(cutoff.timestamp()),
                order='id',
                direction='desc',
                limit=limit,
                **params,
            )
            batch = page.get('entries') or []
            known = len(entries)
            for entry in batch:
                entries.setdefault(entry['id'], entry)
            if len(entries) == known:
                if batch:
                    # With before_entry_id every ID is below all seen ones, so
                    # a page of known IDs means the server ignored the parameter
                    complete = False
                    logger.warning("Miniflux ignored before_entry_id; only the first "
                                   "page was fetched")
                break
            before_id = min(entry['id'] for entry in batch)
            # total counts what is left below before_entry_id, this page included
            if len(batch) < limit or len(batch) >= page.get('total', len(batch) + 1):
                break

        return FetchResult(
            entries=list(entries.values()),
            cutoff=cutoff,
            complete=complete,
            min_id=min(entries) if entries else None,
            fetched_at=fetched_at,
        )

    def _preprocess_entry(self, entry: dict) -> str:
        """
        Preprocess an entry for vectorization.

        Combines title and content, removes HTML, normalizes text.
        Stores a plain-text snippet on the entry for the web interface.
        """
        title = (entry.get('title') or '')[:MAX_TITLE_CHARS]
        content = entry.get('content') or ''
        if 'ata:' in content or 'ATA:' in content:  # plain search, much faster than the regex
            content = _DATA_URI_RE.sub('', content)
        content = content[:MAX_HTML_CHARS]

        if content:
            content = BeautifulSoup(content, 'lxml').get_text(separator=' ')
        content = re.sub(r'\s+', ' ', content).strip()
        entry['_snippet'] = _truncate(content, SNIPPET_LENGTH)

        # Title weighted more heavily
        text = f"{title} {title} {content[:MAX_CONTENT_CHARS]}"

        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)  # Remove punctuation
        text = re.sub(r'\s+', ' ', text)  # Normalize whitespace
        return text.strip()

    def _detect_duplicates(self, entries: list, texts: list) -> set:
        """
        Detect near-duplicate articles within a cluster.

        Uses term frequencies over the full vocabulary: the clustering
        vectorizer drops rare terms (min_df), which would hide exactly the
        text that makes two articles on the same topic different.

        Returns:
            Set of entry IDs that are duplicates (not the canonical version).
        """
        if len(entries) < 2:
            return set()

        vectorizer = TfidfVectorizer(tokenizer=self._tokenize, token_pattern=None,
                                     lowercase=False, ngram_range=(1, 2),
                                     use_idf=False, sublinear_tf=True)
        try:
            similarity_matrix = cosine_similarity(vectorizer.fit_transform(texts))
        except ValueError:  # no tokens left after stopword removal
            return set()
        threshold = self.config.deduplication.threshold

        duplicates = set()
        processed = set()

        for i in range(len(entries)):
            if i in processed:
                continue

            group = [i]
            for j in range(i + 1, len(entries)):
                if j not in processed and similarity_matrix[i, j] >= threshold:
                    group.append(j)
                    processed.add(j)

            if len(group) > 1:
                canonical_idx = self._select_canonical(entries, group)
                duplicates.update(entries[idx]['id'] for idx in group
                                  if idx != canonical_idx)

            processed.add(i)

        return duplicates

    def _select_canonical(self, entries: list, group_indices: list) -> int:
        """
        Select the canonical (best) entry from a group of entries.

        Uses configured strategy: longest, source_priority, or newest.
        Entries with a title always win over untitled ones (e.g. news ticker
        pages), so duplicate detection and the story headline agree.
        """
        strategy = self.config.deduplication.canonical_strategy

        if strategy == 'source_priority':
            scores = self.config.deduplication.source_scores

            def key(i):
                feed = entries[i].get('feed') or {}
                domain = urlparse(feed.get('site_url', '')).netloc.removeprefix('www.')
                return scores.get(domain, 50)

        elif strategy == 'newest':
            def key(i):
                published = entries[i].get('published_at') or ''
                try:
                    return datetime.fromisoformat(published.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    return datetime.min.replace(tzinfo=timezone.utc)

        else:  # 'longest'
            def key(i):
                return len(entries[i].get('content') or '')

        def has_title(i):
            return bool((entries[i].get('title') or '').strip())

        return max(group_indices, key=lambda i: (has_title(i), key(i)))

    def _mark_duplicates_read(self, entries: list, clusters: list) -> int:
        """Mark unread duplicates as read in Miniflux."""
        unread = {e['id'] for e in entries if e.get('status') == 'unread'}
        to_mark = sorted(eid for c in clusters for eid in c.duplicate_ids if eid in unread)
        if not to_mark:
            return 0

        self.client.update_entries(to_mark, status='read')
        self.store.mark_read(to_mark)
        return len(to_mark)


def _truncate(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    return text[:length].rsplit(' ', 1)[0] + ' …'


def setup_logging(config: Config) -> None:
    """Configure root logging according to the config."""
    if config.logging.json_format:
        try:
            from pythonjsonlogger.json import JsonFormatter
        except ImportError:  # python-json-logger < 3
            from pythonjsonlogger.jsonlogger import JsonFormatter
        formatter = JsonFormatter('%(asctime)s %(name)s %(levelname)s %(message)s')
    else:
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    handlers = [logging.StreamHandler(sys.stdout)]
    if config.logging.file:
        handlers.append(logging.FileHandler(config.logging.file, encoding='utf-8'))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(config.logging.level.upper())


def run_scheduler(config: Config, store: StoryStore, stop: threading.Event,
                  make_clusterer: Callable[[], NewsClusterer]) -> None:
    """Run clustering cycles until stopped; retry failures with backoff."""
    clusterer = None
    interval = config.scheduling.interval_minutes * 60
    retry_delay = MIN_RETRY_SECONDS

    while not stop.is_set():
        try:
            if clusterer is None:
                clusterer = make_clusterer()
            failed = clusterer.run_clustering_cycle()['errors'] > 0
        except Exception as e:
            logger.exception("Clustering cycle crashed: %s", e)
            failed = True

        if failed:
            delay = min(retry_delay, interval)
            logger.warning("Clustering failed, retrying in %ds", delay)
            retry_delay = min(retry_delay * 2, interval)
        else:
            delay = interval
            retry_delay = MIN_RETRY_SECONDS
        stop.wait(delay)


def main():
    """Main entry point: clustering scheduler plus Top Stories web server."""
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_config(os.getenv('ARSSE_CONFIG', '/app/config.yaml'))
    except Exception as e:
        logger.error("Failed to load configuration: %s", e)
        sys.exit(1)

    setup_logging(config)
    logger.info("Starting aRSSe Intelligence Layer")

    try:
        store = StoryStore(config.storage.db_path)
    except StoreTooNewError as e:
        logger.error("%s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Cannot open story database %s: %s (is the data directory "
                     "writable for UID %d?)", config.storage.db_path, e, os.getuid())
        sys.exit(1)
    stop = threading.Event()

    scheduler = threading.Thread(
        target=run_scheduler,
        args=(config, store, stop, lambda: NewsClusterer(config, store)),
        name='scheduler',
        daemon=True,
    )
    scheduler.start()

    # Docker sends SIGTERM; exit promptly instead of waiting for SIGKILL
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    from waitress import serve
    from web import create_app

    logger.info("Top Stories available on port %d", config.web.port)
    try:
        serve(create_app(config, store), host=config.web.host, port=config.web.port,
              threads=4, ident='aRSSe')
    finally:
        stop.set()


if __name__ == '__main__':
    main()

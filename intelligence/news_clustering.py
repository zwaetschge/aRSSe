#!/usr/bin/env python3
"""
aRSSe Intelligence Layer - News Clustering and Deduplication

This module implements the core clustering and deduplication logic
that transforms a simple RSS reader into a Google News-like aggregator.

Architecture:
    1. Extraction: Fetch unread articles from Miniflux API
    2. Preprocessing: Text normalization and cleaning
    3. Vectorization: TF-IDF transformation
    4. Clustering: DBSCAN for topic grouping
    5. Deduplication: Near-duplicate detection within clusters
    6. Tagging: Write cluster tags back to Miniflux
"""

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import miniflux
import numpy as np
import schedule
from bs4 import BeautifulSoup
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import Config, load_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('arsse-intelligence')


class NewsClusterer:
    """
    Main clustering engine that groups articles by topic and detects duplicates.

    This class implements a simplified version of Google News' clustering logic
    using TF-IDF vectorization and DBSCAN clustering.
    """

    def __init__(self, config: Config):
        """
        Initialize the clusterer with configuration.

        Args:
            config: Configuration object with clustering parameters.
        """
        self.config = config
        self.client: Optional[miniflux.Client] = None
        self._connect_miniflux()

        # Initialize vectorizer
        self.vectorizer = TfidfVectorizer(
            max_features=config.clustering.max_features,
            stop_words=self._get_stopwords(),
            ngram_range=(1, 2),  # Unigrams and bigrams
            min_df=2,  # Ignore terms that appear in less than 2 documents
            max_df=0.95  # Ignore terms that appear in more than 95% of documents
        )

        logger.info("NewsClusterer initialized with eps=%.2f, min_samples=%d",
                    config.clustering.eps, config.clustering.min_samples)

    def _connect_miniflux(self) -> None:
        """Establish connection to Miniflux API."""
        if not self.config.miniflux_api_key:
            logger.error("MINIFLUX_API_KEY not set. Please configure the API key.")
            raise ValueError("Missing Miniflux API key")

        self.client = miniflux.Client(
            self.config.miniflux_url,
            api_key=self.config.miniflux_api_key
        )

        # Test connection
        try:
            self.client.me()
            logger.info("Connected to Miniflux at %s", self.config.miniflux_url)
        except Exception as e:
            logger.error("Failed to connect to Miniflux: %s", e)
            raise

    def _get_stopwords(self) -> list:
        """Get stopwords for the configured language."""
        try:
            from nltk.corpus import stopwords
            return stopwords.words(self.config.clustering.language)
        except Exception:
            logger.warning("Could not load stopwords for %s, using empty list",
                           self.config.clustering.language)
            return []

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
            'tags_applied': 0,
            'errors': 0
        }

        try:
            # Step 1: Fetch articles
            entries = self._fetch_recent_entries()
            if not entries:
                logger.info("No articles to process")
                return stats

            stats['articles_processed'] = len(entries)
            logger.info("Processing %d articles", len(entries))

            # Step 2: Preprocess texts
            texts = [self._preprocess_entry(e) for e in entries]

            # Filter out empty texts
            valid_indices = [i for i, t in enumerate(texts) if t.strip()]
            if len(valid_indices) < self.config.clustering.min_samples:
                logger.info("Not enough valid articles for clustering (%d)", len(valid_indices))
                return stats

            valid_entries = [entries[i] for i in valid_indices]
            valid_texts = [texts[i] for i in valid_indices]

            # Step 3: Vectorize
            try:
                tfidf_matrix = self.vectorizer.fit_transform(valid_texts)
            except ValueError as e:
                logger.warning("Vectorization failed: %s", e)
                return stats

            # Step 4: Cluster
            clustering = DBSCAN(
                eps=self.config.clustering.eps,
                min_samples=self.config.clustering.min_samples,
                metric=self.config.clustering.metric
            ).fit(tfidf_matrix)

            labels = clustering.labels_
            n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
            stats['clusters_found'] = n_clusters
            logger.info("Found %d clusters", n_clusters)

            # Step 5: Process clusters and detect duplicates
            cluster_map = {}
            for idx, label in enumerate(labels):
                if label == -1:  # Noise point (no cluster)
                    continue
                if label not in cluster_map:
                    cluster_map[label] = []
                cluster_map[label].append(idx)

            for cluster_id, member_indices in cluster_map.items():
                if len(member_indices) < 2:
                    continue

                cluster_entries = [valid_entries[i] for i in member_indices]
                cluster_vectors = tfidf_matrix[member_indices]

                # Detect duplicates within cluster
                duplicates = self._detect_duplicates(cluster_entries, cluster_vectors)
                stats['duplicates_detected'] += len(duplicates)

                # Apply tags
                tags_applied = self._apply_cluster_tags(
                    cluster_entries, cluster_id, duplicates
                )
                stats['tags_applied'] += tags_applied

        except Exception as e:
            logger.exception("Error during clustering cycle: %s", e)
            stats['errors'] += 1

        elapsed = time.time() - start_time
        logger.info("Clustering cycle completed in %.2f seconds: %s", elapsed, stats)
        return stats

    def _fetch_recent_entries(self) -> list:
        """Fetch recent unread entries from Miniflux."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.scheduling.lookback_hours
        )

        try:
            # Fetch unread entries
            entries = self.client.get_entries(
                status='unread',
                limit=self.config.scheduling.batch_size,
                order='published_at',
                direction='desc'
            )

            # Filter by date
            recent = []
            for entry in entries.get('entries', []):
                published = entry.get('published_at')
                if published:
                    # Parse ISO format
                    try:
                        pub_date = datetime.fromisoformat(
                            published.replace('Z', '+00:00')
                        )
                        if pub_date >= cutoff:
                            recent.append(entry)
                    except (ValueError, TypeError):
                        recent.append(entry)  # Include if date parsing fails

            return recent

        except Exception as e:
            logger.error("Failed to fetch entries: %s", e)
            return []

    def _preprocess_entry(self, entry: dict) -> str:
        """
        Preprocess an entry for vectorization.

        Combines title and content, removes HTML, normalizes text.
        """
        title = entry.get('title', '')
        content = entry.get('content', '')

        # Remove HTML tags
        if content:
            soup = BeautifulSoup(content, 'lxml')
            content = soup.get_text(separator=' ')

        # Combine title and content (title weighted more heavily)
        text = f"{title} {title} {content}"

        # Normalize
        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)  # Remove punctuation
        text = re.sub(r'\s+', ' ', text)  # Normalize whitespace
        text = text.strip()

        return text

    def _detect_duplicates(self, entries: list, vectors) -> set:
        """
        Detect near-duplicate articles within a cluster.

        Returns:
            Set of entry IDs that are duplicates (not the canonical version).
        """
        if len(entries) < 2:
            return set()

        # Calculate pairwise similarity
        similarity_matrix = cosine_similarity(vectors)

        # Find duplicates based on threshold
        duplicates = set()
        processed = set()

        for i in range(len(entries)):
            if entries[i]['id'] in processed:
                continue

            group = [i]
            for j in range(i + 1, len(entries)):
                if entries[j]['id'] in processed:
                    continue
                if similarity_matrix[i, j] >= self.config.deduplication.threshold:
                    group.append(j)
                    processed.add(entries[j]['id'])

            if len(group) > 1:
                # Select canonical entry
                canonical_idx = self._select_canonical(entries, group)
                for idx in group:
                    if idx != canonical_idx:
                        duplicates.add(entries[idx]['id'])

            processed.add(entries[i]['id'])

        return duplicates

    def _select_canonical(self, entries: list, group_indices: list) -> int:
        """
        Select the canonical (best) entry from a group of duplicates.

        Uses configured strategy: longest, source_priority, or newest.
        """
        strategy = self.config.deduplication.canonical_strategy
        group_entries = [(i, entries[i]) for i in group_indices]

        if strategy == 'source_priority':
            # Sort by source score (higher is better)
            def get_score(item):
                _, entry = item
                feed_url = entry.get('feed', {}).get('site_url', '')
                domain = urlparse(feed_url).netloc.replace('www.', '')
                return self.config.deduplication.source_scores.get(domain, 50)

            group_entries.sort(key=get_score, reverse=True)

        elif strategy == 'newest':
            # Sort by publication date (newest first)
            def get_date(item):
                _, entry = item
                published = entry.get('published_at', '')
                try:
                    return datetime.fromisoformat(published.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    return datetime.min.replace(tzinfo=timezone.utc)

            group_entries.sort(key=get_date, reverse=True)

        else:  # 'longest' (default)
            # Sort by content length (longest first)
            def get_length(item):
                _, entry = item
                return len(entry.get('content', ''))

            group_entries.sort(key=get_length, reverse=True)

        return group_entries[0][0]

    def _apply_cluster_tags(self, entries: list, cluster_id: int,
                            duplicates: set) -> int:
        """
        Apply cluster and duplicate tags to entries.

        Returns:
            Number of tags successfully applied.
        """
        applied = 0
        cluster_tag = f"{self.config.tagging.cluster_prefix}_{cluster_id}"
        dup_tag = self.config.deduplication.duplicate_tag

        for entry in entries:
            entry_id = entry['id']
            new_tags = []

            # Get existing tags if preserving
            if self.config.tagging.preserve_existing:
                existing = entry.get('tags', []) or []
                new_tags.extend(existing)

            # Add cluster tag
            if cluster_tag not in new_tags:
                new_tags.append(cluster_tag)

            # Add duplicate tag if applicable
            if entry_id in duplicates and dup_tag not in new_tags:
                new_tags.append(dup_tag)

            # Limit number of tags
            new_tags = new_tags[:self.config.tagging.max_tags]

            # Update entry
            try:
                self.client.update_entry(entry_id, tags=new_tags)
                applied += 1

                # Optionally mark duplicates as read
                if (entry_id in duplicates and
                        self.config.deduplication.duplicate_action == 'mark_read'):
                    self.client.update_entry(entry_id, status='read')

            except Exception as e:
                logger.warning("Failed to update entry %d: %s", entry_id, e)

        return applied


def main():
    """Main entry point for the clustering service."""
    logger.info("Starting aRSSe Intelligence Layer")

    # Load configuration
    config_path = '/app/config.yaml'
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error("Failed to load configuration: %s", e)
        sys.exit(1)

    # Set log level
    logging.getLogger().setLevel(getattr(logging, config.logging.level.upper()))

    # Initialize clusterer
    try:
        clusterer = NewsClusterer(config)
    except Exception as e:
        logger.error("Failed to initialize clusterer: %s", e)
        sys.exit(1)

    # Run initial clustering
    logger.info("Running initial clustering cycle...")
    clusterer.run_clustering_cycle()

    # Schedule periodic runs
    interval = config.scheduling.interval_minutes
    logger.info("Scheduling clustering every %d minutes", interval)
    schedule.every(interval).minutes.do(clusterer.run_clustering_cycle)

    # Main loop
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute


if __name__ == '__main__':
    main()

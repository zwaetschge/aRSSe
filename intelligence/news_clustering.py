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

Between clustering runs, a light status sync takes over articles read in
Miniflux (StatusSync), so read stories leave the front page within minutes.
"""

import logging
import logging.handlers
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit

import miniflux
import requests
from bs4 import BeautifulSoup
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_distances, cosine_similarity

from config import (DEFAULT_CONFIG_PATH, MAX_SECTION_CHARS, USER_CONFIG_PATH, Config,
                    ensure_user_config, load_config)
from store import (MAX_TITLE_CHARS, ClusterResult, FetchResult, StoreTooNewError,
                   StoryStore, parse_date)

logger = logging.getLogger('arsse-intelligence')

SNIPPET_LENGTH = 280
# Shorter snippets say nothing ('Video', 'Liveblog') and are not shown
MIN_SNIPPET_CHARS = 20
# Teaser links at the end of feed texts: '[ mehr ]' (Tagesschau, MDR),
# '. mehr...' (taz), 'Weiterlesen'. A bare 'mehr' only counts after the
# end of a sentence, so 'gibt es nicht mehr' stays intact.
_TEASER_TAIL = re.compile(
    r'(?:\s*\[\s*mehr\s*\]|\s*\bweiterlesen\W*'
    r'|(?:^|(?<=[.!?:"“”»)]))\s*mehr\s*(?:\.{2,}|…|»|›)?)\s*$',
    re.IGNORECASE)
# A teaser tail is short, so each pass of clean_snippet() only searches this
# far back from the end: text ending in thousands of tails then costs linear
# instead of quadratic time
_TEASER_TAIL_WINDOW = 64
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
# First retry delay after a failed cycle; doubles up to the normal interval,
# but at most MAX_RETRY_SECONDS, so the service recovers soon after an outage
MIN_RETRY_SECONDS = 10
MAX_RETRY_SECONDS = 300
# logging.file is rotated at this size, keeping LOG_FILE_BACKUPS old files
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3
# Timeout of Miniflux requests: a clustering run pages through many entries,
# the web interface ('Story gelesen') must not keep a page waiting long
CLIENT_TIMEOUT = 60
WEB_CLIENT_TIMEOUT = 15
# Consecutive status syncs overlap by this much: Miniflux compares changed_at
# in whole seconds, and the clocks of the two containers may differ a little
STATUS_SYNC_OVERLAP = timedelta(seconds=60)
# After failures the status sync waits up to this many intervals
STATUS_SYNC_MAX_BACKOFF = 8
# Titles of Miniflux's default category (see section_of)
DEFAULT_CATEGORIES = ('all', 'alle')

_TOKEN_RE = re.compile(r'\b\w\w+\b')
# Query parameters that only track the click, not select the article
_TRACKING_PARAM_RE = re.compile(r'utm_.*|wt_mc', re.IGNORECASE)


class MissingApiKeyError(ValueError):
    """MINIFLUX_API_KEY is not configured."""


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
        self.noise_patterns = [re.compile(p, re.IGNORECASE)
                               for p in config.clustering.noise_title_patterns]
        self.path_sections = [(re.compile(p, re.IGNORECASE), name)
                              for p, name in config.web.path_sections.items()]
        # Whose API key is it? Asked once Miniflux answers (see check_api_key_user)
        self.api_key_checked = False

        logger.info("NewsClusterer initialized with threshold=%.2f, stemming=%s",
                    config.clustering.threshold, self.stemmer is not None)

    def _create_client(self) -> miniflux.Client:
        """Create the Miniflux API client."""
        return create_client(self.config)

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
            # Single words by default: on the reference corpus bigrams cost
            # precision and recall (docs/ARCHITECTURE.md, 'Messungen')
            ngram_range=(1, self.config.clustering.ngram_max),
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
        if not self.api_key_checked:
            self.api_key_checked = check_api_key_user(self.client)
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
            for entry in entries:
                entry['_section'] = section_of(entry, self.path_sections)

            # Read before clustering: an entry aRSSe marked read never stays
            # in place of an unread copy (see _detect_duplicates)
            auto_marked = self.store.auto_marked_ids()
            clusters = self._cluster(entries, auto_marked)
            stats['clusters_found'] = len(clusters)
            stats['duplicates_detected'] = sum(len(c.duplicate_ids) for c in clusters)

            self.store.save_run(entries, clusters, fetch, self.config.web.min_sources,
                                sticky_headline=self._sticky_headline())
            # Statuses as of this fetch are stored now (see StatusSync)
            self.store.set_meta('last_fetch_at', fetch.fetched_at.isoformat())
            self.store.refresh_auto_marked([e['id'] for e in entries])
            self.store.cleanup(self.config.storage.retention_days,
                               self.config.scheduling.lookback_hours)

            if self.config.deduplication.duplicate_action == 'mark_read':
                stats['marked_read'] = self._mark_duplicates_read(entries, clusters)

            self.store.set_meta('last_success', datetime.now(timezone.utc).isoformat())
            clear_error(self.store)
        except Exception as e:
            logger.exception("Error during clustering cycle: %s", e)
            stats['errors'] += 1
            record_error(self.store, self.config, e)

        elapsed = time.time() - start_time
        stats['duration_seconds'] = round(elapsed, 2)
        try:
            self.store.set_meta('last_stats', stats)
        except Exception as e:  # e.g. disk full; must not kill the scheduler
            logger.error("Failed to store run statistics: %s", e)
        logger.info("Clustering cycle completed in %.2f seconds: %s", elapsed, stats)
        return stats

    def _cluster(self, entries: list, auto_marked: frozenset = frozenset()) -> list:
        """
        Group entries into clusters and detect duplicates within each.

        auto_marked holds the IDs of entries that aRSSe marked read before;
        see _detect_duplicates and _canonical_key.

        Untitled multi-topic tickers ('+++ ... +++ ...') are left out: they
        touch every story of the day and would pull unrelated ones together.
        A story of two articles needs a cosine similarity of at least
        clustering.min_pair_similarity: for two articles, average linkage
        only asks for 1 - threshold, which one shared rare word reaches.

        Stories of one topic get the same topic_key (see _topic_keys).
        """
        texts = [self._preprocess_entry(e) for e in entries]
        valid_indices = [i for i, t in enumerate(texts)
                         if t and not _is_ticker(entries[i])]
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

        distances = cosine_distances(tfidf_matrix)
        labels = AgglomerativeClustering(
            n_clusters=None,
            metric='precomputed',
            linkage='average',
            distance_threshold=self.config.clustering.threshold,
        ).fit(distances).labels_

        cluster_map = {}
        for idx, label in enumerate(labels):
            cluster_map.setdefault(label, []).append(idx)
        # Articles nobody else wrote about stay out of stories
        cluster_map = {k: v for k, v in cluster_map.items() if len(v) >= 2}
        min_similarity = self.config.clustering.min_pair_similarity
        cluster_map = {k: v for k, v in cluster_map.items()
                       if len(v) > 2 or 1 - distances[v[0], v[1]] >= min_similarity}

        clusters = []
        topics = []
        topic_labels = self._topic_labels(distances)
        for member_indices in cluster_map.values():
            cluster_entries = [valid_entries[i] for i in member_indices]
            duplicates, copies = self._detect_duplicates(cluster_entries, auto_marked)
            headline_idx = self._select_canonical(cluster_entries,
                                                  list(range(len(cluster_entries))),
                                                  auto_marked)
            headline_id = cluster_entries[headline_idx]['id']
            # The headline is shown as the story; it must never be marked read
            duplicates.discard(headline_id)
            copies.discard(headline_id)
            clusters.append(ClusterResult(
                entry_ids=[e['id'] for e in cluster_entries],
                headline_entry_id=headline_id,
                duplicate_ids=duplicates,
                copy_ids=copies,
                noise_ids={e['id'] for e in cluster_entries if not self._headline_worthy(e)},
            ))
            if topic_labels is not None:
                topics.append(Counter(topic_labels[i] for i in member_indices)
                              .most_common(1)[0][0])
        if topic_labels is not None:
            _set_topic_keys(clusters, topics)

        logger.info("Found %d clusters", len(clusters))
        return clusters

    def _topic_labels(self, distances):
        """
        Topic group of every article, or None if topics are off.

        A second average-linkage pass over the same distances with the
        looser clustering.topic_threshold: stories about one event from
        different angles (the match, the coach, the reactions) end up in
        one topic, while story boundaries stay at clustering.threshold.
        """
        threshold = self.config.clustering.topic_threshold
        if not threshold:
            return None
        return AgglomerativeClustering(
            n_clusters=None,
            metric='precomputed',
            linkage='average',
            distance_threshold=threshold,
        ).fit(distances).labels_

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
            if self.config.miniflux_respect_hide_globally:
                # Like Miniflux's own lists: no feeds or categories the user
                # hid from them ('hide_globally')
                params['globally_visible'] = True
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
        Stores on the entry: a plain-text snippet for the web interface
        ('_snippet'), the length of the plain text ('_text_len', for the
        'longest' strategy) and the normalized text without the title
        ('_body', for duplicate detection).
        """
        title = (entry.get('title') or '')[:MAX_TITLE_CHARS]
        content = entry.get('content') or ''
        if 'ata:' in content or 'ATA:' in content:  # plain search, much faster than the regex
            content = _DATA_URI_RE.sub('', content)
        content = content[:MAX_HTML_CHARS]

        if content:
            content = BeautifulSoup(content, 'lxml').get_text(separator=' ')
        content = re.sub(r'\s+', ' ', content).strip()
        # Cleaned before cutting, so a teaser link cannot survive half cut
        entry['_snippet'] = _truncate(clean_snippet(content, title), SNIPPET_LENGTH)
        entry['_text_len'] = len(content)
        body = _normalize(content[:MAX_CONTENT_CHARS])
        entry['_body'] = body

        # Title weighted more heavily
        title = _normalize(title)
        return ' '.join(part for part in (title, title, body) if part)

    def _detect_duplicates(self, entries: list,
                           auto_marked: frozenset = frozenset()) -> tuple:
        """
        Detect near-duplicate articles within a cluster.

        Two articles are duplicates if

        1. they are the same item twice: same URL (see _norm_url), same
           title and the same or nearly the same text (equal, or a cosine
           similarity of deduplication.threshold), in any feed and of any
           length. Feeds sometimes list an item twice, and Miniflux stores
           both copies;
        2. otherwise never if they come from the same feed: a feed does not
           copy itself, but its series share titles and boilerplate
           ('tagesschau' with the body '[ mehr ]');
        3. from different feeds, if their texts without the title reach a
           cosine similarity of deduplication.threshold and both have at
           least deduplication.min_body_tokens words. Titles are left out:
           two teasers with the same headline and no text of their own
           would otherwise always match.

        Similarity uses term frequencies over the full vocabulary: the
        clustering vectorizer drops rare terms (min_df), which would hide
        exactly the text that makes two articles on the same topic
        different.

        Groups are built in canonical order (see _canonical_key): the best
        entry not yet assigned keeps its status and takes every unassigned
        entry that is a duplicate of it; the rest stays for the next group.
        Every duplicate has thus been compared with the article that stays.
        Entries in auto_marked (marked read by aRSSe before) come last: once
        Miniflux updates an article, the copy marked read may become the
        longest one, and keeping it would mark the unread copy as well.

        A duplicate is a copy (rule 1) if it is the same item as the seed
        or as a better-ranked member of its group: two copies of one feed
        may both join the group of an article from another feed.

        Returns:
            Tuple (duplicates, copies): IDs of all duplicates (not the
            canonical versions), and the subset that matched rule 1.
        """
        if len(entries) < 2:
            return set(), set()

        dedup = self.config.deduplication
        bodies = [self._body(e) for e in entries]
        similarity = self._body_similarity(bodies)
        long_enough = [len(_TOKEN_RE.findall(b)) >= dedup.min_body_tokens for b in bodies]
        urls = [_norm_url(e.get('url')) for e in entries]
        titles = [_normalize((e.get('title') or '')[:MAX_TITLE_CHARS]) for e in entries]
        feeds = [_feed_id(e) for e in entries]

        def similar(i, j):
            return bodies[i] == bodies[j] or (similarity is not None
                                              and similarity[i, j] >= dedup.threshold)

        def same_item(i, j):
            return bool(urls[i]) and urls[i] == urls[j] and titles[i] == titles[j] \
                and similar(i, j)

        def duplicate_of(i, j):
            if feeds[i] == feeds[j]:
                return False
            return long_enough[i] and long_enough[j] and similar(i, j)

        order = sorted(range(len(entries)),
                       key=lambda i: (entries[i]['id'] not in auto_marked,
                                      self._canonical_key(entries[i], auto_marked)),
                       reverse=True)
        duplicates = set()
        copies = set()
        assigned = set()
        for seed in order:
            if seed in assigned:
                continue
            assigned.add(seed)
            group = [seed]
            for member in order:
                if member in assigned:
                    continue
                if not same_item(member, seed) and not duplicate_of(member, seed):
                    continue
                # An article that a duplicate marked in an earlier run still
                # depends on stays unread, unless that duplicate also matches
                # this seed: otherwise the marked one could end up without
                # any unread copy of its text (copies arrive over several runs)
                if any(entries[m]['id'] in auto_marked and m not in assigned
                       and (same_item(m, member) or duplicate_of(m, member))
                       and not (same_item(m, seed) or duplicate_of(m, seed))
                       for m in order):
                    continue
                if any(same_item(member, kept) for kept in group):
                    copies.add(entries[member]['id'])
                duplicates.add(entries[member]['id'])
                assigned.add(member)
                group.append(member)

        return duplicates, copies

    def _body(self, entry: dict) -> str:
        """The normalized text of an entry without its title."""
        if '_body' not in entry:
            self._preprocess_entry(entry)
        return entry['_body']

    def _body_similarity(self, bodies: list):
        """Pairwise cosine similarity of term frequencies; None without any tokens."""
        vectorizer = TfidfVectorizer(tokenizer=self._tokenize, token_pattern=None,
                                     lowercase=False, ngram_range=(1, 2),
                                     use_idf=False, sublinear_tf=True)
        try:
            return cosine_similarity(vectorizer.fit_transform(bodies))
        except ValueError:  # no tokens left after stopword removal
            return None

    def _sticky_headline(self) -> bool:
        """
        Whether a story keeps its headline across runs (StoryStore.save_run).

        Only with 'longest': otherwise every longer report would take over
        the headline. 'newest' and 'source_priority' follow the cluster.
        """
        return self.config.deduplication.canonical_strategy == 'longest'

    def _select_canonical(self, entries: list, group_indices: list,
                          auto_marked: frozenset = frozenset()) -> int:
        """
        Select the canonical (best) entry from a group of entries.

        See _canonical_key.
        """
        return max(group_indices, key=lambda i: self._canonical_key(entries[i], auto_marked))

    def _canonical_key(self, entry: dict, auto_marked: frozenset = frozenset()) -> tuple:
        """
        Rank an entry as canonical version: the higher, the better.

        Uses the configured strategy: longest (plain text, not HTML),
        source_priority or newest. Entries with a title always win over
        untitled ones (e.g. news ticker pages), so duplicate detection and
        the story headline agree. Next, titles that match none of
        clustering.noise_title_patterns (ads, podcasts, live blogs) win,
        then entries that aRSSe has not marked read (not in auto_marked).
        On a tie the lowest (first stored) ID wins: identical copies must
        not swap roles with the order in which Miniflux returns them.
        """
        strategy = self.config.deduplication.canonical_strategy

        if strategy == 'source_priority':
            feed = entry.get('feed') or {}
            domain = urlparse(feed.get('site_url', '')).netloc.removeprefix('www.')
            key = self.config.deduplication.source_scores.get(domain, 50)
        elif strategy == 'newest':
            published = entry.get('published_at') or ''
            try:
                key = datetime.fromisoformat(published.replace('Z', '+00:00'))
            except (ValueError, TypeError, AttributeError):
                key = datetime.min.replace(tzinfo=timezone.utc)
            if key.tzinfo is None:
                key = key.replace(tzinfo=timezone.utc)
        else:  # 'longest'
            if '_text_len' not in entry:
                self._preprocess_entry(entry)
            key = entry['_text_len']

        has_title = bool((entry.get('title') or '').strip())
        return (has_title, not self._is_noise(entry), entry['id'] not in auto_marked,
                key, -entry['id'])

    def _is_noise(self, entry: dict) -> bool:
        """True if the title matches one of clustering.noise_title_patterns."""
        title = (entry.get('title') or '')[:MAX_TITLE_CHARS]
        return any(pattern.search(title) for pattern in self.noise_patterns)

    def _headline_worthy(self, entry: dict) -> bool:
        """An entry may head a story: it has a title that is not noise."""
        return bool((entry.get('title') or '').strip()) and not self._is_noise(entry)

    def _mark_duplicates_read(self, entries: list, clusters: list) -> int:
        """
        Mark unread duplicates as read in Miniflux.

        With mark_read_scope 'visible' only in clusters that the front page
        shows (web.min_sources feeds); identical copies (rule 1 of
        _detect_duplicates) everywhere. An entry is marked only once: if
        the user sets it back to unread, it stays unread.
        """
        feed_of = {e['id']: _feed_id(e) for e in entries}
        min_sources = self.config.web.min_sources
        mark_all = self.config.deduplication.mark_read_scope == 'all'
        candidates = set()
        for cluster in clusters:
            feeds = {feed_of.get(eid, eid) for eid in cluster.entry_ids}
            if mark_all or len(feeds) >= min_sources:
                candidates |= cluster.duplicate_ids
            else:
                candidates |= cluster.copy_ids

        unread = {e['id'] for e in entries if e.get('status') == 'unread'}
        candidates &= unread
        if candidates:
            candidates -= self.store.auto_marked_ids()
        to_mark = sorted(candidates)
        if not to_mark:
            return 0

        self.client.update_entries(to_mark, status='read')
        self.store.record_auto_marked(to_mark)
        return len(to_mark)


def create_client(config: Config, timeout: int = CLIENT_TIMEOUT) -> miniflux.Client:
    """Create a Miniflux API client; each thread uses its own."""
    if not config.miniflux_api_key:
        raise MissingApiKeyError("MINIFLUX_API_KEY not set. Please configure the API key.")
    return miniflux.Client(config.miniflux_url, api_key=config.miniflux_api_key,
                           timeout=timeout)


def _set_topic_keys(clusters: list, topics: list) -> None:
    """
    Give clusters of the same topic group the same topic_key.

    topics holds the topic label of each cluster (the one most of its
    articles have; the looser pass contains the stories, so that is all of
    them). The key is the smallest entry ID among the stories of a topic,
    so it does not depend on how the clustering numbers its groups.
    """
    smallest = {}
    for cluster, topic in zip(clusters, topics):
        smallest[topic] = min(smallest.get(topic, min(cluster.entry_ids)),
                              min(cluster.entry_ids))
    for cluster, topic in zip(clusters, topics):
        cluster.topic_key = int(smallest[topic])


def section_of(entry: dict, path_sections: list) -> Optional[str]:
    """
    Section (Rubrik) of an entry, or None.

    The title of its Miniflux category, unless that is the default
    category ('All', 'Alle'), which says nothing. Otherwise the name of
    the first pattern of path_sections, a list of (compiled regex, name),
    that matches a whole segment of the URL path, such as 'sport' in
    tagesschau.de/sport/fussball/... The last segment names the article
    itself and is skipped. Entries without a feed, category or URL (test
    fixtures, other clients) simply have no section.
    """
    feed = entry.get('feed')
    category = feed.get('category') if isinstance(feed, dict) else None
    title = category.get('title') if isinstance(category, dict) else None
    if isinstance(title, str) and title.strip() \
            and title.strip().casefold() not in DEFAULT_CATEGORIES:
        return title.strip()[:MAX_SECTION_CHARS]
    url = entry.get('url')
    try:
        path = urlsplit(url).path if isinstance(url, str) else ''
    except ValueError:
        return None
    segments = [unquote(segment) for segment in path.split('/') if segment][:-1]
    for pattern, name in path_sections:
        if any(pattern.fullmatch(segment) for segment in segments):
            return name
    return None


class StatusSync:
    """
    Take over articles read in Miniflux between clustering runs.

    Asks Miniflux only for entries of the window marked read since the
    last sync (changed_after; SetEntriesStatus updates changed_at), a page
    at a time, and only updates entries that are stored. A clustering run
    may store statuses it fetched before this sync ran; after each new run
    the sync therefore starts again from that run's fetch time.
    """

    def __init__(self, config: Config, store: StoryStore, client):
        self.config = config
        self.store = store
        self.client = client
        # Marks made in Miniflux up to this time are stored
        self.synced_until: Optional[datetime] = None
        # Fetch time of the clustering run the last sync saw
        self.seen_fetch: Optional[datetime] = None

    def run(self) -> int:
        """
        Sync once.

        Returns:
            Number of stored entries that turned read.
        """
        started = datetime.now(timezone.utc)
        fetched = parse_date(self.store.get_meta('last_fetch_at'))
        if fetched is None:
            return 0  # no clustering run yet, and it fetches every status itself
        since = self.synced_until
        if fetched != self.seen_fetch:
            since = fetched if since is None else min(since, fetched)

        scheduling = self.config.scheduling
        cutoff = started - timedelta(hours=scheduling.lookback_hours)
        limit = scheduling.batch_size
        read_ids = set()
        after_id = None
        for _ in range(-(-scheduling.max_entries // limit)):
            params = {} if after_id is None else {'after_entry_id': after_id}
            page = self.client.get_entries(
                status='read',
                changed_after=int((since - STATUS_SYNC_OVERLAP).timestamp()),
                published_after=int(cutoff.timestamp()),
                order='id',
                direction='asc',
                limit=limit,
                **params,
            )
            batch = [e['id'] for e in page.get('entries') or []]
            if not batch or read_ids.issuperset(batch):
                break  # done, or the server ignored after_entry_id
            read_ids.update(batch)
            if len(batch) < limit:
                break
            after_id = max(batch)
        else:
            logger.info("Status sync stopped after %d entries; the next clustering run "
                        "takes over the rest", len(read_ids))

        updated = self.store.set_read(sorted(read_ids))
        self.synced_until = started
        self.seen_fetch = fetched
        if updated:
            logger.info("Status sync: %d articles were read in Miniflux", updated)
        return updated


def run_status_sync(config: Config, store: StoryStore, stop: threading.Event,
                    make_client: Callable[[], object]) -> None:
    """
    Run StatusSync every scheduling.status_sync_minutes until stopped.

    Never raises: a failure is logged, and the wait doubles up to
    STATUS_SYNC_MAX_BACKOFF intervals, so an unreachable Miniflux is not
    asked more often. Runs in a thread of its own; clustering and the web
    interface do not wait for it.
    """
    interval = config.scheduling.status_sync_minutes * 60
    delay = interval
    sync = None
    while not stop.wait(delay):
        try:
            if sync is None:
                sync = StatusSync(config, store, make_client())
            sync.run()
            delay = interval
        except Exception as e:
            delay = min(delay * 2, interval * STATUS_SYNC_MAX_BACKOFF)
            logger.warning("Status sync with Miniflux failed, next try in %d s: %s", delay, e)


def describe_error(error: BaseException, config: Config) -> tuple:
    """
    Kind and plain German reason of a failed clustering run.

    Shown on the front page and in /healthz, which stays reachable without
    login, so the message never contains details of the exception.

    Returns:
        (kind, message) with kind one of 'config', 'auth', 'connection',
        'database' and 'internal'.
    """
    if isinstance(error, MissingApiKeyError):
        return 'config', 'MINIFLUX_API_KEY fehlt'
    if isinstance(error, miniflux.AccessUnauthorized):
        return 'auth', 'Miniflux lehnt den API-Key ab'
    if isinstance(error, (miniflux.ClientError, requests.RequestException,
                          ConnectionError, TimeoutError)):
        message = f'Miniflux unter {_without_userinfo(config.miniflux_url)} nicht erreichbar'
        status = getattr(error, 'status_code', None)
        return 'connection', f'{message} (HTTP {status})' if status else message
    if isinstance(error, sqlite3.OperationalError):
        return 'database', 'Datenbank nicht beschreibbar'
    return 'internal', f'Clustering fehlgeschlagen ({type(error).__name__}, Details im Log)'


def _without_userinfo(url: str) -> str:
    """URL without user name and password."""
    parts = urlsplit(url)
    if '@' not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit('@', 1)[1]))


def record_error(store: StoryStore, config: Config, error: BaseException) -> None:
    """
    Store why the last clustering run failed (meta 'last_error').

    'at' is when this error first occurred: it stays while the same error
    repeats, so the front page can say 'Fehler seit 10:30'. Never raises.
    """
    kind, message = describe_error(error, config)
    try:
        previous = store.get_meta('last_error')
        if isinstance(previous, dict) and previous.get('message') == message:
            at = previous.get('at')
        else:
            at = datetime.now(timezone.utc).isoformat()
        store.set_meta('last_error', {'at': at, 'kind': kind, 'message': message})
    except Exception as e:  # e.g. the database itself is the problem
        logger.error("Failed to store the error of the clustering run: %s", e)


def clear_error(store: StoryStore) -> None:
    """Forget the last error after a successful run."""
    if store.get_meta('last_error') is not None:
        store.set_meta('last_error', None)


def check_api_key_user(client) -> bool:
    """
    Warn when MINIFLUX_API_KEY belongs to a Miniflux admin.

    Miniflux API keys have no scopes: an admin's key can create users and
    change passwords, far more than reading entries and marking them read.
    Never raises, so an unreachable Miniflux cannot stop the service.

    Returns:
        True once the check ran, False if Miniflux could not be asked
        (the caller retries with the next cycle).
    """
    try:
        user = client.me()
    except Exception as e:
        logger.info("Could not check the Miniflux user of MINIFLUX_API_KEY yet: %s", e)
        return False
    if isinstance(user, dict) and user.get('is_admin'):
        logger.warning("MINIFLUX_API_KEY belongs to the admin user '%s'. A leaked key would "
                       "give full control over Miniflux. Create a user without admin rights "
                       "for reading and use its API key (README, 'Absicherung').",
                       user.get('username', '?'))
    return True


def _is_ticker(entry: dict) -> bool:
    """An untitled news ticker listing many topics ('+++ ... +++ ...')."""
    if (entry.get('title') or '').strip():
        return False
    return entry.get('_snippet', '').startswith('+++')


def _normalize(text: str) -> str:
    """Lowercase, replace punctuation with spaces and collapse whitespace."""
    text = re.sub(r'[^\w\s]', ' ', text.lower())
    return re.sub(r'\s+', ' ', text).strip()


def _norm_url(url) -> str:
    """
    Normalize an article URL for comparison ('' if there is none).

    Scheme and host are case-insensitive; the fragment, tracking
    parameters (utm_*, wt_mc) and a trailing slash do not change the
    article.
    """
    if not isinstance(url, str) or not url.strip():
        return ''
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _TRACKING_PARAM_RE.fullmatch(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip('/'), query, ''))


def _feed_id(entry: dict):
    """The feed an entry belongs to."""
    return entry.get('feed_id') or (entry.get('feed') or {}).get('id')


def clean_snippet(text: str, title: str = '') -> str:
    """
    Make an article's plain text fit to show as its snippet.

    Removes the teaser links feeds append ('[ mehr ]', 'mehr...',
    'Weiterlesen'), and returns '' for text that says nothing: the literal
    'None' some feeds send, fewer than MIN_SNIPPET_CHARS characters, or
    just the title again.
    """
    text = text.strip()
    # Tails are removed from the end one by one; moving an end index instead
    # of re-scanning and copying the whole text keeps this linear
    end = len(text)
    while match := _TEASER_TAIL.search(text, max(0, end - _TEASER_TAIL_WINDOW), end):
        end = match.start()
        while end and text[end - 1].isspace():
            end -= 1
    text = text[:end]
    if len(text) < MIN_SNIPPET_CHARS or text.lower() in ('none', 'null'):
        return ''
    if _comparable(text) == _comparable(title):
        return ''
    return text


def _comparable(text: str) -> str:
    return re.sub(r'\W+', ' ', text).strip().casefold()


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
        handlers.append(logging.handlers.RotatingFileHandler(
            config.logging.file, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS,
            encoding='utf-8'))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(config.logging.level.upper())


def run_scheduler(config: Config, store: StoryStore, stop: threading.Event,
                  make_clusterer: Callable[[], NewsClusterer]) -> None:
    """
    Run clustering cycles until stopped; retry failures with backoff.

    The retry delay doubles from MIN_RETRY_SECONDS up to the interval, but
    at most MAX_RETRY_SECONDS. Errors outside a cycle (no API key) are
    stored like those of a cycle (record_error).
    """
    clusterer = None
    interval = config.scheduling.interval_minutes * 60
    max_retry = min(interval, MAX_RETRY_SECONDS)
    retry_delay = MIN_RETRY_SECONDS

    while not stop.is_set():
        try:
            if clusterer is None:
                clusterer = make_clusterer()
            failed = clusterer.run_clustering_cycle()['errors'] > 0
        except Exception as e:
            if isinstance(e, MissingApiKeyError):
                logger.error("%s", e)
            else:
                logger.exception("Clustering cycle crashed: %s", e)
            record_error(store, config, e)
            failed = True

        if failed:
            delay = min(retry_delay, max_retry)
            logger.warning("Clustering failed, retrying in %ds", delay)
            retry_delay = min(retry_delay * 2, max_retry)
        else:
            delay = interval
            retry_delay = MIN_RETRY_SECONDS
        stop.wait(delay)


def main():
    """Main entry point: clustering scheduler plus Top Stories web server."""
    logging.basicConfig(level=logging.INFO)
    user_config = os.getenv('ARSSE_USER_CONFIG', USER_CONFIG_PATH)
    ensure_user_config(user_config)
    try:
        config = load_config(os.getenv('ARSSE_CONFIG', DEFAULT_CONFIG_PATH), user_config)
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

    # Clients of their own, so a long clustering run never holds up the
    # web interface or the status sync
    web_client = None
    if config.miniflux_api_key:
        web_client = create_client(config, WEB_CLIENT_TIMEOUT)
        if config.scheduling.status_sync_minutes:
            threading.Thread(
                target=run_status_sync,
                args=(config, store, stop, lambda: create_client(config, WEB_CLIENT_TIMEOUT)),
                name='status-sync',
                daemon=True,
            ).start()

    # Docker sends SIGTERM; exit promptly instead of waiting for SIGKILL
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    from waitress import serve
    from web import create_app

    logger.info("Top Stories available on port %d", config.web.port)
    try:
        serve(create_app(config, store, web_client), host=config.web.host, port=config.web.port,
              threads=4, ident='aRSSe')
    finally:
        stop.set()


if __name__ == '__main__':
    main()

"""
Score a clustering against hand-labelled gold data.

gold.json labels articles by entry URL (no article text):

    {"window": {"end": "2026-09-24T18:31:41Z", "hours": 24},
     "articles": {"https://...": {"event": "XI_TRUMP", "topics": ["XI_T"]},
                  "https://...": {}}}

Every listed article is labelled. Two articles are

- a must-link pair if they share an "event" (the same concrete news) or
  the same URL (one item listed twice);
- neutral if they share one of their "topics" (same broader topic, or a
  multi-topic item such as a ticker): neither required nor an error;
- a cannot-link pair otherwise.

Articles whose URL the gold data does not list are ignored.
"""

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from store import parse_date

MUST_LINK, NEUTRAL, CANNOT_LINK = 1, 0, -1


@dataclass
class Label:
    event: Optional[str] = None
    topics: frozenset = frozenset()


@dataclass
class Gold:
    labels: dict = field(default_factory=dict)  # URL -> Label
    # Window the labels were made for (end of the last run), if known
    window_end: Optional[datetime] = None
    window_hours: Optional[int] = None

    def relation(self, url_a: str, url_b: str) -> Optional[int]:
        """MUST_LINK, NEUTRAL or CANNOT_LINK; None if an article is not labelled."""
        a, b = self.labels.get(url_a), self.labels.get(url_b)
        if a is None or b is None:
            return None
        if url_a == url_b or (a.event is not None and a.event == b.event):
            return MUST_LINK
        if a.topics & b.topics:
            return NEUTRAL
        return CANNOT_LINK


def load_gold(path: str) -> Gold:
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    window = data.get('window') or {}
    return Gold(
        labels={url: Label(label.get('event'), frozenset(label.get('topics') or ()))
                for url, label in data['articles'].items()},
        window_end=parse_date(window.get('end')),
        window_hours=window.get('hours'),
    )


def _pairs(ids) -> set:
    return {(a, b) if a < b else (b, a) for a, b in itertools.combinations(ids, 2)}


def pairwise_scores(gold: Gold, groups: list, url_of: dict) -> dict:
    """
    Pairwise precision, recall and F1 of groups of entry IDs.

    groups are the stories (lists of entry IDs, singletons left out);
    url_of maps every entry ID of the evaluated window to its URL, so
    recall counts must-link pairs that no story holds.
    """
    labelled = sorted(i for i, url in url_of.items() if url in gold.labels)
    predicted = set()
    for group in groups:
        predicted |= _pairs([i for i in group if url_of.get(i) in gold.labels])
    relation = {pair: gold.relation(url_of[pair[0]], url_of[pair[1]]) for pair in predicted}
    tp = sum(1 for r in relation.values() if r == MUST_LINK)
    fp = sum(1 for r in relation.values() if r == CANNOT_LINK)
    positives = sum(1 for a, b in itertools.combinations(labelled, 2)
                    if gold.relation(url_of[a], url_of[b]) == MUST_LINK)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {'labelled': len(labelled), 'must_link': positives, 'tp': tp, 'fp': fp,
            'precision': precision, 'recall': recall, 'f1': f1}


def story_quality(gold: Gold, groups: list, url_of: dict, feed_of: dict,
                  min_sources: int = 2) -> dict:
    """
    Classify the stories the front page shows (min_sources feeds).

    pure: no cannot-link pair; mixed: a cannot-link pair, but also a
    must-link pair across feeds (a real story with an intruder); junk:
    no must-link pair across feeds (unrelated articles).
    """
    counts = {'shown': 0, 'pure': 0, 'mixed': 0, 'junk': 0}
    junk = []
    for group in groups:
        if len({feed_of.get(i, i) for i in group}) < min_sources:
            continue
        counts['shown'] += 1
        relations = [(gold.relation(url_of.get(a), url_of.get(b)), feed_of.get(a) != feed_of.get(b))
                     for a, b in _pairs(group)]
        if all(r != CANNOT_LINK for r, _ in relations):
            counts['pure'] += 1
        elif any(r == MUST_LINK and cross for r, cross in relations):
            counts['mixed'] += 1
        else:
            counts['junk'] += 1
            junk.append(group)
    counts['junk_groups'] = junk
    return counts

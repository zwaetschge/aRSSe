"""
Replay a saved corpus through the real StoryStore with a simulated clock.

Every run clusters the articles published inside the lookback window at
that time, saves the result and reads the front page, just like a
clustering cycle followed by a page view. Nothing is marked read.
Between consecutive runs it measures what a reader notices:

- id_kept: stories of the previous front page that are still listed
  under the same ID;
- headline_changed: of those, stories whose headline changed;
- vanished: stories none of whose articles are listed any more;
- top10_overlap: stories that stay in the top 10.
"""

import contextlib
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import store as store_module
from store import FetchResult, StoryStore, parse_date


@dataclass
class ReplayResult:
    runs: int = 0
    transitions: int = 0
    previous_shown: int = 0
    id_kept: int = 0
    headline_changed: int = 0
    vanished: int = 0
    top10_overlap: int = 0
    # Front page of the last run: (story ID, headline title, source count)
    final_page: list = field(default_factory=list)

    def rate(self, name: str) -> float:
        base = self.id_kept if name == 'headline_changed' else self.previous_shown
        return getattr(self, name) / base if base else 0.0


def _frozen_datetime(now: datetime):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    return FrozenDatetime


@contextlib.contextmanager
def frozen_clock(now: datetime):
    """Let the store module see 'now' as the current time."""
    with mock.patch.object(store_module, 'datetime', _frozen_datetime(now)), \
            mock.patch.object(store_module, '_now', lambda: now.isoformat()):
        yield


def published(entry: dict):
    return parse_date(entry.get('published_at'))


def window(entries: list, end: datetime, hours: int) -> list:
    """Copies of the entries published in [end - hours, end]."""
    start = end - timedelta(hours=hours)
    return [dict(e) for e in entries
            if (p := published(e)) is not None and start <= p <= end]


def replay(clusterer, entries: list, end: datetime, runs: int = 25,
           step_minutes: int = 30, db_dir: str = None) -> ReplayResult:
    """
    Run `runs` clustering cycles, step_minutes apart, the last one at end.

    Uses clusterer's config (lookback, min_sources, max_stories,
    exclude_patterns) and a fresh story database.
    """
    config = clusterer.config
    web = config.web
    lookback = config.scheduling.lookback_hours
    result = ReplayResult()
    with tempfile.TemporaryDirectory(dir=db_dir) as tmp:
        store = StoryStore(str(Path(tmp) / 'replay.db'))
        previous = None
        for k in range(runs - 1, -1, -1):
            now = (end - timedelta(minutes=step_minutes * k)).astimezone(timezone.utc)
            run_entries = window(entries, now, lookback)
            clusters = clusterer._cluster(run_entries)
            with frozen_clock(now):
                fetch = FetchResult(entries=run_entries, cutoff=now - timedelta(hours=lookback),
                                    min_id=min((e['id'] for e in run_entries), default=None),
                                    fetched_at=now)
                store.save_run(run_entries, clusters, fetch, web.min_sources)
                store.cleanup(config.storage.retention_days, lookback)
                page = store.top_stories(lookback, web.max_stories, web.min_sources,
                                         web.earlier_articles_max, web.exclude_patterns)
            current = [(s['id'], s['headline']['id'], {a['id'] for a in s['articles']})
                       for s in page]
            if previous is not None:
                _compare(result, previous, current)
            previous = current
            result.runs += 1
            result.final_page = [(s['id'], s['headline']['title'], s['source_count'])
                                 for s in page]
    return result


def _compare(result: ReplayResult, previous: list, current: list) -> None:
    result.transitions += 1
    headline_now = {sid: headline for sid, headline, _ in current}
    listed = set().union(*(articles for _, _, articles in current)) if current else set()
    for sid, headline, articles in previous:
        result.previous_shown += 1
        if sid in headline_now:
            result.id_kept += 1
            if headline_now[sid] != headline:
                result.headline_changed += 1
        elif not articles & listed:
            result.vanished += 1
    top_before = {sid for sid, _, _ in previous[:10]}
    result.top10_overlap += len(top_before & {sid for sid, _, _ in current[:10]})

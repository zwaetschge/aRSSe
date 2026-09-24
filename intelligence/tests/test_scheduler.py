import threading

import news_clustering
from news_clustering import run_scheduler


class StopAfter(threading.Event):
    """Event that records waits and stops the loop after n of them."""

    def __init__(self, n):
        super().__init__()
        self.n = n
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) >= self.n:
            self.set()
        return self.is_set()


class FakeClusterer:
    def __init__(self, results):
        self.results = list(results)

    def run_clustering_cycle(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return {'errors': result}


def test_failed_cycles_retry_with_backoff_then_reset(config, store):
    stop = StopAfter(5)
    clusterer = FakeClusterer([1, 1, 0, 1, 0])
    run_scheduler(config, store, stop, lambda: clusterer)

    base = news_clustering.MIN_RETRY_SECONDS
    interval = config.scheduling.interval_minutes * 60
    assert stop.waits == [base, base * 2, interval, base, interval]


def test_crashing_cycle_does_not_kill_scheduler(config, store):
    stop = StopAfter(2)
    clusterer = FakeClusterer([OSError('disk full'), 0])
    run_scheduler(config, store, stop, lambda: clusterer)

    assert stop.waits == [news_clustering.MIN_RETRY_SECONDS,
                          config.scheduling.interval_minutes * 60]


def test_failing_init_is_retried(config, store):
    stop = StopAfter(2)
    attempts = []

    def make():
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError('no api key')
        return FakeClusterer([0])

    run_scheduler(config, store, stop, make)
    assert len(attempts) == 2


def test_stats_write_failure_is_contained(config, store, monkeypatch):
    from conftest import FakeClient, sample_entries
    clusterer = news_clustering.NewsClusterer(config, store,
                                              client=FakeClient(sample_entries()))

    def broken(*args):
        raise OSError('database or disk is full')
    monkeypatch.setattr(store, 'set_meta', broken)

    stats = clusterer.run_clustering_cycle()
    assert stats['errors'] == 1

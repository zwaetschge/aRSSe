import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import store as store_module
from conftest import make_entry, sample_entries
from store import (MIGRATIONS, REBUILD, SCHEMA_V1, ClusterResult, FetchResult,
                   StoreTooNewError, StoryStore, db_timestamp)
from web import create_app


def user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def make_v1_db(path):
    """A database as written by aRSSe before schema versioning."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1)
    conn.execute("INSERT INTO entries (id, feed_id, feed_title, title, url, published_at) "
                 "VALUES (100, 1, 'Tagesschau', 'Alt', 'https://example.org/100', "
                 "'2026-01-02T03:04:05.678901+00:00')")
    conn.execute("INSERT INTO stories VALUES ('oldstory', 100, '2026-01-02', '2026-01-02')")
    conn.execute("INSERT INTO story_entries VALUES (100, 'oldstory', 0)")
    conn.commit()
    conn.close()


def budget_clusters():
    return [ClusterResult([1, 2, 3], 1, {2}), ClusterResult([4, 5], 4, set())]


def test_new_database_has_latest_schema(tmp_path):
    path = str(tmp_path / 'arsse.db')
    StoryStore(path)
    assert user_version(path) == len(MIGRATIONS)
    assert {'created_at', 'published_at_raw'} <= set(columns(path, 'entries'))


def test_unversioned_v1_database_is_migrated(tmp_path):
    path = str(tmp_path / 'arsse.db')
    make_v1_db(path)
    assert user_version(path) == 0

    st = StoryStore(path)
    assert user_version(path) == len(MIGRATIONS)
    assert {'created_at', 'published_at_raw'} <= set(columns(path, 'entries'))

    conn = sqlite3.connect(path)
    published = conn.execute("SELECT published_at FROM entries WHERE id = 100").fetchone()[0]
    conn.close()
    assert published == '2026-01-02T03:04:05+00:00'

    # The migrated database works for a full run and the web interface
    st.save_run(sample_entries(), budget_clusters())
    assert sorted(len(s['articles']) for s in st.top_stories(24, 50)) == [2, 3]
    # Its only article is from January: outside any recent window
    assert st.get_story('oldstory', 24) is None
    assert st.get_story('oldstory', 24 * 3650)['articles'][0]['title'] == 'Alt'

    # Opening again changes nothing
    StoryStore(path)
    assert user_version(path) == len(MIGRATIONS)


def test_database_from_newer_version_is_refused(tmp_path):
    path = str(tmp_path / 'arsse.db')
    StoryStore(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    with pytest.raises(StoreTooNewError) as error:
        StoryStore(path)
    message = str(error.value)
    assert 'Datenbank stammt von neuerer aRSSe-Version' in message
    assert 'Backup einspielen oder arsse.db löschen' in message
    assert user_version(path) == 99


def test_failing_migration_leaves_database_untouched(tmp_path, monkeypatch):
    path = str(tmp_path / 'arsse.db')
    StoryStore(path)
    monkeypatch.setattr(store_module, 'MIGRATIONS', MIGRATIONS + [
        "ALTER TABLE entries ADD COLUMN author TEXT;\nTHIS IS NOT SQL;"])

    with pytest.raises(sqlite3.OperationalError):
        StoryStore(path)
    assert user_version(path) == len(MIGRATIONS)
    assert 'author' not in columns(path, 'entries')


def test_appended_migration_is_applied(tmp_path, monkeypatch):
    path = str(tmp_path / 'arsse.db')
    StoryStore(path).save_run(sample_entries(), budget_clusters())
    monkeypatch.setattr(store_module, 'MIGRATIONS', MIGRATIONS + [
        "-- a comment; with a semicolon\n"
        "ALTER TABLE stories ADD COLUMN note TEXT NOT NULL DEFAULT 'a;b';\n"
        "CREATE INDEX idx_entries_title ON entries(title);"])

    StoryStore(path)
    assert user_version(path) == len(MIGRATIONS) + 1
    conn = sqlite3.connect(path)
    notes = {row[0] for row in conn.execute("SELECT note FROM stories")}
    conn.close()
    assert notes == {'a;b'}


def test_rebuild_moves_old_database_aside(tmp_path, monkeypatch):
    path = str(tmp_path / 'arsse.db')
    StoryStore(path).save_run(sample_entries(), budget_clusters())
    old_version = len(MIGRATIONS)
    monkeypatch.setattr(store_module, 'MIGRATIONS', MIGRATIONS + [
        REBUILD, "CREATE TABLE extra (x INTEGER);"])

    st = StoryStore(path)
    backup = tmp_path / f'arsse.db.v{old_version}.bak'
    assert backup.exists()
    assert user_version(str(backup)) == old_version
    conn = sqlite3.connect(str(backup))
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 6
    conn.close()

    assert user_version(path) == old_version + 2
    assert 'x' in columns(path, 'extra')
    assert st.top_stories(24, 50) == []


def test_rebuild_is_skipped_for_a_new_database(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, 'MIGRATIONS', MIGRATIONS + [REBUILD])
    path = str(tmp_path / 'arsse.db')
    StoryStore(path)
    assert user_version(path) == len(MIGRATIONS) + 1
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith('.bak')] == []


def test_dates_are_stored_in_one_comparable_format(store):
    # An hour ago, written in local time (UTC+2) with microseconds
    utc = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=123456)
    raw = utc.astimezone(timezone(timedelta(hours=2))).isoformat()
    entries = sample_entries()
    entries[0]['published_at'] = raw
    store.save_run(entries, budget_clusters())

    story = store.get_story(store.top_stories(24, 50)[0]['id'], 24)
    for article in story['articles']:
        assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00', article['published_at'])
    first = next(a for s in store.top_stories(24, 50) for a in s['articles'] if a['id'] == 1)
    assert first['published_at'] == utc.replace(microsecond=0).isoformat()
    assert first['published_at_raw'] == raw
    assert db_timestamp(datetime(2026, 1, 1, 12, 0, 0, 999, tzinfo=timezone.utc)) \
        == '2026-01-01T12:00:00+00:00'


def test_future_dated_article_is_capped_at_ingest_time(store):
    # r11: a feed dates an article 36 h ahead (wrong time zone, scheduled item)
    now = datetime.now(timezone.utc)
    entries = [make_entry(i, i, f'Vier Quellen {i}', 'x', hours_ago=13) for i in (1, 2, 3, 4)]
    future = make_entry(5, 1, 'Zwei Quellen', 'x', hours_ago=-36)
    future['created_at'] = (now - timedelta(hours=1)).isoformat()
    entries += [future, make_entry(6, 2, 'Zwei Quellen', 'x', hours_ago=20)]
    store.save_run(entries, [ClusterResult([1, 2, 3, 4], 1, set()),
                             ClusterResult([5, 6], 5, set())])

    stories = store.top_stories(24, 50)
    assert [s['source_count'] for s in stories] == [4, 2]
    capped = stories[1]['articles'][0]
    assert capped['id'] == 5
    assert capped['published_at'] == db_timestamp(now - timedelta(hours=1))
    assert capped['published_at_raw'] == future['published_at']


def test_future_date_without_created_at_is_capped_at_first_fetch(store):
    first_fetch = datetime.now(timezone.utc) - timedelta(hours=2)
    entries = [make_entry(1, 1, 'Zukunft', 'x', hours_ago=-36),
               make_entry(2, 2, 'Zukunft', 'x', hours_ago=1)]
    clusters = [ClusterResult([1, 2], 1, set())]
    store.save_run(entries, clusters, FetchResult(entries, first_fetch - timedelta(hours=24),
                                                  fetched_at=first_fetch))
    # Later runs must not move the date forward again, or it never ages
    store.save_run(entries, clusters)

    article = next(a for a in store.top_stories(24, 50)[0]['articles'] if a['id'] == 1)
    assert article['published_at'] == db_timestamp(first_fetch)


def test_missing_optional_fields_are_tolerated(store):
    entry = {'id': 1, 'feed_id': 1, 'title': None, 'url': None, 'published_at': None}
    other = make_entry(2, 2, 'Titel', 'x')
    store.save_run([entry, other], [ClusterResult([1, 2], 2, set())])

    article = next(a for a in store.top_stories(24, 50)[0]['articles'] if a['id'] == 1)
    assert article['title'] == '' and article['url'] == ''
    assert article['published_at'] is not None
    assert article['published_at_raw'] is None and article['created_at'] is None


def test_long_titles_are_cut(store):
    entries = [make_entry(1, 1, 'Sehr lang ' * 1000, 'x'), make_entry(2, 2, 'Kurz', 'x')]
    store.save_run(entries, [ClusterResult([1, 2], 2, set())])
    titles = {a['id']: a['title'] for a in store.top_stories(24, 50)[0]['articles']}
    assert len(titles[1]) == store_module.MAX_TITLE_CHARS


def dissolve_during_load(monkeypatch, st, entries):
    """Commit a run that dissolves every story right after the first SELECT."""
    load = StoryStore._load_story
    done = []

    def load_after_commit(conn, row, *args):
        if not done:
            done.append(True)
            st.save_run(entries, [])  # other connection, commits immediately
        return load(conn, row, *args)
    monkeypatch.setattr(StoryStore, '_load_story', staticmethod(load_after_commit))


def test_story_page_reads_one_snapshot(config, store, monkeypatch):
    entries = sample_entries()
    store.save_run(entries, budget_clusters())
    story_id = next(s['id'] for s in store.top_stories(24, 50) if s['article_count'] == 3)
    dissolve_during_load(monkeypatch, store, entries)

    response = create_app(config, store).test_client().get(f'/story/{story_id}')
    assert response.status_code == 200
    assert 'Bundestag' in response.get_data(as_text=True)
    # The commit happened: the story is gone for the next request
    assert store.get_story(story_id, 24) is None


def test_top_stories_read_one_snapshot(store, monkeypatch):
    entries = sample_entries()
    store.save_run(entries, budget_clusters())
    dissolve_during_load(monkeypatch, store, entries)

    stories = store.top_stories(24, 50, min_sources=1)
    assert sorted(s['article_count'] for s in stories) == [2, 3]


def test_story_without_articles_is_not_found(config, store):
    conn = sqlite3.connect(config.storage.db_path)
    conn.execute("INSERT INTO stories VALUES ('empty', 1, '2026-01-01', '2026-01-01')")
    conn.commit()
    conn.close()

    assert store.get_story('empty', 24) is None
    assert create_app(config, store).test_client().get('/story/empty').status_code == 404


def test_service_refuses_to_start_on_newer_database(config, monkeypatch, caplog):
    import news_clustering
    StoryStore(config.storage.db_path)
    conn = sqlite3.connect(config.storage.db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    monkeypatch.setattr(news_clustering, 'load_config', lambda path: config)
    monkeypatch.setattr(news_clustering, 'setup_logging', lambda cfg: None)

    with caplog.at_level('ERROR', logger='arsse-intelligence'):
        with pytest.raises(SystemExit) as exit_info:
            news_clustering.main()
    assert exit_info.value.code == 1
    assert 'neuerer aRSSe-Version' in caplog.text
    assert 'writable' not in caplog.text  # not a permission problem


def marked_rows(path):
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT entry_id, marked_at FROM auto_marked"))
    finally:
        conn.close()


def test_auto_marked_entries_are_remembered_until_they_leave_the_window(store):
    store.save_run(sample_entries(), budget_clusters())
    store.record_auto_marked([2])
    assert store.auto_marked_ids() == {2}
    # Miniflux reports the entry unread again (the user reset it)
    store.save_run(sample_entries(), budget_clusters())
    assert store.auto_marked_ids() == {2}

    now = datetime.now(timezone.utc)
    store.record_auto_marked([3, 4])
    conn = sqlite3.connect(store.db_path)
    with conn:
        conn.execute("UPDATE auto_marked SET marked_at = ? WHERE entry_id = 3",
                     (db_timestamp(now - timedelta(hours=24 + 24, minutes=1)),))
        conn.execute("UPDATE auto_marked SET marked_at = ? WHERE entry_id = 4",
                     (db_timestamp(now - timedelta(hours=24 + 23)),))
    conn.close()
    store.cleanup(7, lookback_hours=24)
    assert set(marked_rows(store.db_path)) == {2, 4}
    store.cleanup(7, lookback_hours=12)
    assert set(marked_rows(store.db_path)) == {2}


def test_v2_database_gets_auto_marked_table(tmp_path, monkeypatch):
    path = str(tmp_path / 'arsse.db')
    monkeypatch.setattr(store_module, 'MIGRATIONS', MIGRATIONS[:2])
    StoryStore(path)
    assert user_version(path) == 2
    monkeypatch.undo()

    st = StoryStore(path)
    assert user_version(path) == len(MIGRATIONS)
    st.record_auto_marked([1])
    assert st.auto_marked_ids() == {1}


def test_auto_marked_entries_still_fetched_are_kept(store):
    store.record_auto_marked([2, 3])
    old = db_timestamp(datetime.now(timezone.utc) - timedelta(hours=24 + 24, minutes=1))
    conn = sqlite3.connect(store.db_path)
    with conn:
        conn.execute("UPDATE auto_marked SET marked_at = ?", (old,))
    conn.close()
    # 2 is still fetched (e.g. dated days ahead), 3 has left the window
    store.refresh_auto_marked([1, 2, 5])
    assert marked_rows(store.db_path)[3] == old
    store.cleanup(7, lookback_hours=24)
    assert store.auto_marked_ids() == {2}

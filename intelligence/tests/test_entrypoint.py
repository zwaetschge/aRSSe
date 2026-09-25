import os

import pytest

import entrypoint


class Recorder:
    """Records privileged calls instead of executing them."""

    def __init__(self, monkeypatch, uid):
        self.calls = []
        monkeypatch.setattr(os, 'getuid', lambda: uid)
        monkeypatch.setattr(os, 'chown', self._record('chown'))
        for name in ('setgroups', 'setgid', 'setuid', 'execvp'):
            monkeypatch.setattr(os, name, self._record(name))

    def _record(self, name):
        def call(*args, **kwargs):
            self.calls.append((name,) + args)
        return call

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    path = tmp_path / 'data'
    monkeypatch.setattr(entrypoint, 'DATA_DIR', str(path))
    monkeypatch.delenv('PUID', raising=False)
    monkeypatch.delenv('PGID', raising=False)
    monkeypatch.setenv('HOME', '/root')
    return path


def test_root_creates_data_dir_chowns_and_drops_privileges(data_dir, monkeypatch):
    # Docker created the bind mount as root; the service must not run as root
    monkeypatch.setenv('PUID', '99')
    monkeypatch.setenv('PGID', '100')
    rec = Recorder(monkeypatch, uid=0)

    entrypoint.main(['python', 'news_clustering.py'])

    assert data_dir.is_dir()
    assert ('chown', str(data_dir), 99, 100) in rec.calls
    assert rec.names()[-4:] == ['setgroups', 'setgid', 'setuid', 'execvp']
    assert ('setgroups', []) in rec.calls
    assert ('setgid', 100) in rec.calls and ('setuid', 99) in rec.calls
    assert rec.calls[-1] == ('execvp', 'python', ['python', 'news_clustering.py'])
    assert os.environ['HOME'] != '/root'


def test_root_defaults_to_1000(data_dir, monkeypatch):
    rec = Recorder(monkeypatch, uid=0)
    entrypoint.main(['true'])
    assert ('setuid', 1000) in rec.calls and ('setgid', 1000) in rec.calls


def test_database_files_are_chowned_only_when_owner_differs(data_dir, monkeypatch):
    data_dir.mkdir()
    for name in ('arsse.db', 'arsse.db-wal', 'arsse.db-shm'):
        (data_dir / name).write_bytes(b'')
    st = os.stat(data_dir)

    rec = Recorder(monkeypatch, uid=0)
    assert entrypoint.prepare_data_dir(str(data_dir), st.st_uid, st.st_gid) == []
    assert rec.calls == []

    changed = entrypoint.prepare_data_dir(str(data_dir), st.st_uid + 1, st.st_gid)
    assert [os.path.basename(p) for p in changed] == \
        ['data', 'arsse.db', 'arsse.db-shm', 'arsse.db-wal']
    assert all(c[0] == 'chown' and c[2:] == (st.st_uid + 1, st.st_gid) for c in rec.calls)


def test_non_root_execs_command_unchanged(data_dir, monkeypatch):
    # Older compose files still set user: 1000:1000
    rec = Recorder(monkeypatch, uid=1000)
    entrypoint.main(['python', 'news_clustering.py'])
    assert rec.calls == [('execvp', 'python', ['python', 'news_clustering.py'])]
    assert not data_dir.exists()


def test_unwritable_data_dir_still_starts_the_service(data_dir, monkeypatch):
    rec = Recorder(monkeypatch, uid=0)

    def refuse(*args, **kwargs):
        raise PermissionError('root_squash')
    monkeypatch.setattr(os, 'chown', refuse)
    entrypoint.main(['true'])
    assert rec.names() == ['setgroups', 'setgid', 'setuid', 'execvp']


def test_invalid_puid_is_rejected(data_dir, monkeypatch):
    monkeypatch.setenv('PUID', 'nobody')
    rec = Recorder(monkeypatch, uid=0)
    with pytest.raises(SystemExit) as exc:
        entrypoint.main(['true'])
    assert exc.value.code == 2
    assert rec.calls == []


def test_empty_puid_uses_default(monkeypatch):
    monkeypatch.setenv('PUID', '')
    assert entrypoint.parse_id('PUID') == 1000

"""
Container entrypoint for the aRSSe Intelligence Layer.

Docker creates a missing bind-mount directory as root, so a container that
starts directly as an unprivileged user cannot write its story database on
a fresh install. This entrypoint therefore starts as root, hands the data
directory to PUID:PGID (default 1000:1000, Unraid: 99:100) and then drops
all privileges before exec'ing the actual command.

When the container already runs unprivileged (older compose files with
``user:``), the command is exec'ed unchanged.

docker-compose.yml drops all capabilities except the ones this needs as
root: CHOWN and DAC_OVERRIDE for the data directory, SETUID and SETGID for
the switch. After the switch the process has none left.

Written in Python because python:3.11-slim does not guarantee setpriv/gosu.
"""

import os
import sys
from typing import List, Optional

DATA_DIR = os.getenv('ARSSE_DATA_DIR', '/app/data')
DEFAULT_ID = 1000


def _log(message: str) -> None:
    print(f"entrypoint: {message}", file=sys.stderr, flush=True)


def parse_id(name: str, default: int = DEFAULT_ID) -> int:
    """Read a numeric user or group ID from the environment."""
    value = (os.getenv(name) or '').strip()
    if not value:
        return default
    if not value.isdigit():
        raise ValueError(f"{name} must be a numeric ID, got '{value}'")
    return int(value)


def _chown_if_needed(path: str, uid: int, gid: int) -> bool:
    """Change owner of path (not following symlinks) unless it already matches."""
    st = os.lstat(path)
    if (st.st_uid, st.st_gid) == (uid, gid):
        return False
    os.chown(path, uid, gid, follow_symlinks=False)
    return True


def prepare_data_dir(path: str, uid: int, gid: int) -> List[str]:
    """
    Create the data directory and hand it to uid:gid.

    Only the directory and its direct entries (arsse.db, -wal, -shm, log
    file) are touched, and only when their owner differs, so restarts do
    not rewrite ownership needlessly.

    Returns:
        Paths whose owner was changed.
    """
    os.makedirs(path, exist_ok=True)
    changed = []
    if _chown_if_needed(path, uid, gid):
        changed.append(path)
    for name in sorted(os.listdir(path)):
        entry = os.path.join(path, name)
        if _chown_if_needed(entry, uid, gid):
            changed.append(entry)
    return changed


def drop_privileges(uid: int, gid: int) -> None:
    """Switch to uid:gid for good; supplementary groups are cleared."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    # HOME=/root is not readable after the switch
    if os.environ.get('HOME', '/root') == '/root':
        try:
            import pwd
            home = pwd.getpwuid(uid).pw_dir
        except KeyError:
            home = '/tmp'
        os.environ['HOME'] = home if os.path.isdir(home) else '/tmp'


def main(argv: Optional[List[str]] = None) -> None:
    """Prepare the data directory as root, then exec the command unprivileged."""
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        _log("no command given")
        sys.exit(2)

    if os.getuid() == 0:
        try:
            uid, gid = parse_id('PUID'), parse_id('PGID')
        except ValueError as e:
            _log(str(e))
            sys.exit(2)
        try:
            for path in prepare_data_dir(DATA_DIR, uid, gid):
                _log(f"owner of {path} set to {uid}:{gid}")
        except OSError as e:
            # e.g. NFS with root_squash; the app reports a clear error later
            _log(f"cannot prepare {DATA_DIR}: {e}")
        if uid == 0:
            _log("PUID=0: running as root is not recommended")
        else:
            drop_privileges(uid, gid)

    os.execvp(command[0], command)


if __name__ == '__main__':
    main()

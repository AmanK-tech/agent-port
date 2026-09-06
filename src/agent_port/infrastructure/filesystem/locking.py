from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_port.domain.errors import RestoreError


@contextmanager
def restore_destination_lock(destination: Path) -> Iterator[None]:
    """Serialize cooperating restore and rollback processes for one native home."""
    destination = destination.expanduser().resolve()
    identity = hashlib.sha256(os.fsencode(os.path.normcase(str(destination)))).hexdigest()
    root = destination.parent / ".agent-port-runs" / ".locks"
    root.mkdir(parents=True, exist_ok=True)
    # Keep the inode after release: unlinking a lock file lets a new process lock
    # a different inode while an existing waiter still holds the old one.
    with (root / f"{identity}.lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RestoreError(
                    "Another restore or rollback is active for this destination."
                ) from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RestoreError(
                    "Another restore or rollback is active for this destination."
                ) from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

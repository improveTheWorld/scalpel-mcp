"""The edit lock, PER FILE -- what makes one call alone in its file while it runs.

WHY A LOCK AT ALL (docs/CHANTIER_VERROU_EDITION.md sections 1 and 4.1). Line numbers are a
RENDERING of the tool, not information the agent holds: an address never carries a paragraph
number, so when the tool moves lines it is the tool's job to translate the numbers the agent
read. A third party who moves those lines in between makes that translation impossible, and
every refusal it then causes is a fault of the tool. The lock is what makes the one-shot
reachable.

WHAT THIS MODULE GUARANTEES, and why each clause is here:

  * per file, never global. Two agents on two different files must not see each other: making an
    agent wait on `b.py` because another agent edits `a.py` burns a turn for nothing.
  * writers are serialised: one at a time, the others WAIT (they are no longer refused).
  * a READER of a file being edited waits too. Not to avoid a torn read -- the write is already
    indivisible, a temp file followed by `os.replace` (patcher.py:6239-6243) -- but because its
    read would be stale BY CONSTRUCTION: it would reason on a snapshot the writer invalidates,
    and pay a turn for it. Readers and writers take the SAME lock; there is no second kind.
  * several files in one call are taken in a FIXED order -- alphabetical, on the normalised
    absolute path. A fixed order is the only thing that forbids a deadlock between two batches
    that cross, [a, b] against [b, a].
  * a DEAD holder is released immediately and exactly: the OS lock dies with its process, so
    nothing has to be dated or reclaimed. The timeout exists to WAIT for a LIVING holder.
  * reentrance is per thread. The call that edits re-reads the block it just wrote, so it must
    never wait for itself; depth is counted, because one `release` must not give back a lock the
    same thread took twice.

WHY THE OS LOCK AND NOT A MEMORY DICTIONARY (decided, not re-litigated): two MCP servers can
share one workspace, and a purely in-memory lock is worth nothing between them. The OS lock is
also the only primitive that gives the dead-holder recovery for free -- Windows
`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` and POSIX `fcntl.flock(fd, LOCK_EX | LOCK_NB)` both
vanish with the process that holds them.

FALLBACK, deliberate: if neither `msvcrt` nor `fcntl` exists (an exotic platform), this module
still works, but IN MEMORY ONLY -- which is worth nothing between two processes. Import never
raises: a lock that kills the import kills the whole server.

THE LOCK FILE IS NEVER THE EDITED FILE. The edited file is replaced by `os.replace`, so an OS lock
taken on it would stay on the old inode/descriptor while the name already points elsewhere. The
lock file name is derived from the normalised absolute path (sha1 plus the file name) so two
different workspaces never collide and the name stays readable to an operator.

MEASURED on this Windows box before a line was written (msvcrt probe, byte 0 of a zero-length
lock file): locking an EMPTY file succeeds; a second handle IN THE SAME PROCESS is refused with
PermissionError; a write into the locked region is refused; closing the holder's descriptor frees
the region. Two consequences are used below -- the lock file is never padded with a byte (writing
one while another process holds the region would fail), and same-process exclusion is not left to
the OS lock alone: an in-process guard is kept per path, which is also what makes the memory-only
fallback correct rather than decorative.
"""

import hashlib
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional


#: Sleep between two attempts on the OS lock. A few milliseconds: the loop waits for a LIVING
#: holder to finish, so it must not spin, and it must not sleep the whole timeout at once either
#: -- the lock is usually free long before that, and the caller is an agent paying for turns.
_POLL_SECONDS = 0.002

try:  # pragma: no cover - the branch taken depends on the platform, not on the code
    if os.name == "nt":
        import msvcrt as _msvcrt

        _fcntl = None
    else:
        _msvcrt = None
        import fcntl as _fcntl

    _HAS_OS_LOCK = True
except ImportError:  # pragma: no cover - exotic platform; see FALLBACK in the module docstring
    _msvcrt = None
    _fcntl = None
    _HAS_OS_LOCK = False


class LockTimeout(Exception):
    """The wait expired without the lock being obtained. The message names the path."""


def _lock_file_name(key: str) -> str:
    """A readable, collision-free lock file name derived from the normalised absolute path.

    The digest carries the WHOLE path, so two workspaces that hold a file of the same name never
    share a lock file; the trailing file name is kept so the lock directory can be read by eye.
    `surrogatepass` because Windows can hand back paths with undecodable bytes in them, and a
    UnicodeEncodeError here would refuse an edit for a reason the agent cannot act on.
    """
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    stem = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in Path(key).name)
    return digest + "-" + (stem[:48] or "file") + ".lock"


def _try_os_lock(fd: int) -> bool:
    """One NON-BLOCKING attempt. False means "someone else holds it", never "it is broken"."""
    if _msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    return True


def _release_os_lock(fd: int) -> None:
    """Unlock then close. Closing alone frees the region, but the explicit unlock keeps POSIX honest."""
    try:
        if _msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        elif _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


class _Entry:
    """The state of ONE path: the in-process guard, the OS descriptor, the depths per thread.

    The guard is what serialises the threads of THIS process (and the whole lock, on a platform
    without an OS lock). The descriptor is what serialises PROCESSES. A thread holds the guard
    for the whole critical section, not just while it probes: that keeps the ordering argument
    simple -- every path is taken as "guard, then OS lock, in alphabetical order", uniformly.
    """

    __slots__ = ("guard", "fd", "depths")

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.fd: Optional[int] = None
        self.depths: Dict[int, int] = {}


#: The registry is MODULE level, not per instance: the lock belongs to the FILE, so two
#: `FileLocks` instances built in one process by mistake must still exclude each other, and a
#: thread that took the lock through one of them must not deadlock on the other. The only
#: per-instance setting left is the timeout. Entries are never pruned: a thread parked on a
#: guard has to keep seeing the SAME guard, so pruning could hand two threads two different
#: guards for one path. An entry is two dozen bytes; the paths a server edits are bounded.
_REGISTRY = threading.Lock()
_ENTRIES: Dict[str, _Entry] = {}


def _entry_for(key: str) -> _Entry:
    with _REGISTRY:
        entry = _ENTRIES.get(key)
        if entry is None:
            entry = _Entry()
            _ENTRIES[key] = entry
        return entry


class FileLocks:
    """The edit lock, PER FILE. One instance per server is enough; see `_ENTRIES` for why.

    `acquire` and `acquires` block until the lock is obtained or the timeout expires, then raise
    `LockTimeout`. Nothing else raises on the caller's behalf, and `release` never raises: an
    edit path that has already failed must not fail a second time on the way out.
    """

    def __init__(self, timeout: float = 30.0, lock_dir=None) -> None:
        self.timeout = float(timeout)
        self.lock_dir = (
            Path(lock_dir) if lock_dir is not None
            else Path(tempfile.gettempdir()) / "scalpel-locks"
        )

    # -- naming ------------------------------------------------------------

    @staticmethod
    def _normalize(path) -> str:
        """The ONE key of a file: absolute, resolved, and lower-cased on Windows.

        Case matters here and the repository has already paid for it: Windows resolves the part
        of a path that EXISTS to its stored spelling but leaves the part that does not exist yet
        as it was typed, so `GHOST.PY` and `ghost.py` are two keys for one future file. Two
        agents would then edit the same file while each believed it was alone. On POSIX the
        lower-casing is NOT applied: two names that differ by case are two different files.
        """
        candidate = Path(path)
        try:
            absolute = candidate.resolve()
        except OSError:
            # A path the OS refuses to resolve (broken reparse point, too long): fall back to the
            # lexical absolute form rather than refusing the edit for a reason the agent cannot fix.
            absolute = Path(os.path.abspath(str(candidate)))
        key = str(absolute)
        if os.name == "nt":
            key = key.lower()
        return key

    def _message(self, path) -> str:
        return (
            "the edit lock on %r was still held after %gs; another call is editing this file"
            % (str(path), self.timeout)
        )

    def _open_lock_file(self, key: str) -> Optional[int]:
        """Open (creating it) the dedicated lock file for this key.

        None means "no OS lock for this path": either the platform has no such primitive, or the
        lock directory could not be created or opened. Degrading to the in-process guard is
        deliberate -- the alternative is refusing every edit because a temp directory is not
        writable, which is a worse failure than a lock that does not cross processes.
        """
        if not _HAS_OS_LOCK:
            return None
        try:
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            return os.open(str(self.lock_dir / _lock_file_name(key)), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None

    # -- the API -----------------------------------------------------------

    def acquire(self, path) -> bool:
        """Take the exclusive lock on THIS path.

        True if THIS thread just obtained it, False if it already held it (reentrance: the depth
        is counted, so one `release` does not give it back). Blocks until it is obtained or the
        timeout expires, then raises `LockTimeout` naming the path.
        """
        key = self._normalize(path)
        entry = _entry_for(key)
        ident = threading.get_ident()

        with _REGISTRY:
            depth = entry.depths.get(ident, 0)
            if depth:
                entry.depths[ident] = depth + 1
                return False

        deadline = time.monotonic() + self.timeout
        remaining = max(deadline - time.monotonic(), 0.0)
        if not entry.guard.acquire(timeout=remaining):
            raise LockTimeout(self._message(path))

        fd: Optional[int] = None
        try:
            fd = self._open_lock_file(key)
            if fd is not None:
                while not _try_os_lock(fd):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LockTimeout(self._message(path))
                    time.sleep(min(_POLL_SECONDS, remaining))
            with _REGISTRY:
                entry.fd = fd
                entry.depths[ident] = 1
            return True
        except BaseException:
            # Leave the file exactly as it was: no descriptor leaked, no guard held.
            if fd is not None:
                _release_os_lock(fd)
            entry.guard.release()
            raise

    def release(self, path) -> None:
        """Give back ONE depth. Never raises if this thread does not hold the lock."""
        key = self._normalize(path)
        with _REGISTRY:
            entry = _ENTRIES.get(key)
            if entry is None:
                return
            ident = threading.get_ident()
            depth = entry.depths.get(ident, 0)
            if depth == 0:
                return
            if depth > 1:
                # A reentrant depth holds NO guard take of its own (see acquire), so giving
                # one back must not release the guard either.
                entry.depths[ident] = depth - 1
                return
            del entry.depths[ident]
            fd = entry.fd
            entry.fd = None

        # The OS lock goes FIRST: the guard is the queue of this process, and a thread woken by
        # it must find the OS lock already free rather than poll a region we still own.
        if fd is not None:
            _release_os_lock(fd)
        entry.guard.release()

    def acquires(self, paths) -> List[bool]:
        """Take SEVERAL locks at once, in the ALPHABETICAL order of the normalised absolute path.

        The fixed order is what forbids a deadlock between two batches that cross: [a, b] and
        [b, a] both take a before b. The answers come back in the DEMANDED order, so the caller
        can zip them with what it asked for. If one lock times out, the ones already taken by
        THIS call are released (one depth each, so a depth the caller already held survives) and
        the `LockTimeout` is propagated: no ghost lock is left behind.
        """
        demanded = [str(path) for path in paths]
        ordered = sorted(
            range(len(demanded)),
            key=lambda index: (self._normalize(demanded[index]), index),
        )
        answers: List[bool] = [False] * len(demanded)
        taken: List[int] = []
        try:
            for index in ordered:
                answers[index] = self.acquire(demanded[index])
                taken.append(index)
        except BaseException:
            for index in reversed(taken):
                self.release(demanded[index])
            raise
        return answers

    def releases(self, paths) -> None:
        """Give back a batch, in the REVERSE of the demanded order. Never raises."""
        for path in reversed(list(paths)):
            self.release(path)

    def held(self, path) -> int:
        """The depth this CURRENT thread holds on this path (0 when it holds none)."""
        key = self._normalize(path)
        ident = threading.get_ident()
        with _REGISTRY:
            entry = _ENTRIES.get(key)
            if entry is None:
                return 0
            return entry.depths.get(ident, 0)


@contextmanager
def hold_file(lock: FileLocks, path):
    """Context manager: acquire, then release even when the body raises."""
    lock.acquire(path)
    try:
        yield lock
    finally:
        lock.release(path)

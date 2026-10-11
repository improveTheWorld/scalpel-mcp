"""Tests for the per-file edit lock (src/ast_graph_server/filelock.py).

WHY these cases, and not others (docs/CHANTIER_VERROU_EDITION.md section 4.1):

  * serialisation and reentrance are two halves of one rule: the call that edits re-reads the
    block it just wrote, so it must never wait for itself;
  * two different files must ignore each other -- making an agent wait on `b.py` because another
    agent edits `a.py` burns a turn for nothing;
  * the fixed order can only be proven under contention: two crossed batches must FINISH. The
    test is bounded twice -- `future.result(timeout=...)` on the test side, and the lock's own
    timeout -- because a deadlock has to FAIL the test, not freeze the suite;
  * a DEAD holder is acceptance criterion 5: the operating system releases the lock when the
    process dies, so the recovery is exact and immediate. That claim needs a REAL child process
    killed by the parent, not an in-memory simulation.
"""

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import pytest

from ast_graph_server.filelock import FileLocks, LockTimeout, hold_file


def _locks(tmp_path: Path, timeout: float = 5.0) -> FileLocks:
    """A lock whose lock files live inside this test's own tmp_path.

    The lock directory is a parameter precisely so that two tests never share a lock file, and
    a test that leaks a held lock cannot contaminate the next one.
    """
    return FileLocks(timeout=timeout, lock_dir=tmp_path / "locks")


def test_writers_are_serialised(tmp_path: Path) -> None:
    """Two threads on the SAME path: one critical section at a time, with no overlap.

    Measures the overlap (peak number of open sections) and the enter/exit trace, so the second
    thread is proven to enter only after the first one left.
    """
    lock = _locks(tmp_path)
    target = tmp_path / "a.py"
    gate = threading.Barrier(2)
    state = threading.Lock()
    active = 0
    peak = 0
    trace: List[str] = []

    def worker(tag: str) -> None:
        nonlocal active, peak
        gate.wait(timeout=10)
        with hold_file(lock, target):
            with state:
                active += 1
                peak = max(peak, active)
                trace.append("enter:" + tag)
            time.sleep(0.15)
            with state:
                active -= 1
                trace.append("exit:" + tag)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, tag) for tag in ("a", "b")]
        for future in futures:
            future.result(timeout=15)

    assert peak == 1, "two critical sections overlapped: " + repr(trace)
    assert [entry.split(":")[0] for entry in trace] == ["enter", "exit", "enter", "exit"]


def test_two_files_do_not_see_each_other(tmp_path: Path) -> None:
    """A is held: B must be taken immediately. A global lock would fail this test."""
    lock = _locks(tmp_path, timeout=2.0)
    path_a = tmp_path / "a.py"
    path_b = tmp_path / "b.py"
    released = threading.Event()
    b_taken = threading.Event()
    b_elapsed: List[float] = []

    def hold_a() -> None:
        with hold_file(lock, path_a):
            released.wait(5)

    def take_b() -> None:
        started = time.monotonic()
        with hold_file(lock, path_b):
            b_elapsed.append(time.monotonic() - started)
            b_taken.set()

    first = threading.Thread(target=hold_a)
    first.start()
    time.sleep(0.2)
    second = threading.Thread(target=take_b)
    second.start()

    assert b_taken.wait(2.0), "B waited while only A was held: the lock is not per file"
    assert b_elapsed and b_elapsed[0] < 1.0, "B was delayed by a lock that does not concern it"

    released.set()
    first.join(5)
    second.join(5)
    assert lock.held(path_a) == 0
    assert lock.held(path_b) == 0


def test_reentrance_counts_depth(tmp_path: Path) -> None:
    """Same thread, same path: True then False, and TWO releases are needed to give it back."""
    lock = _locks(tmp_path)
    target = tmp_path / "a.py"

    assert lock.acquire(target) is True
    assert lock.acquire(target) is False, "reentrance must return False, not raise"
    assert lock.held(target) == 2

    lock.release(target)
    assert lock.held(target) == 1

    entered = threading.Event()

    def other() -> None:
        with hold_file(lock, target):
            entered.set()

    thread = threading.Thread(target=other)
    thread.start()
    assert not entered.wait(0.4), "another thread entered after a single release: depth is not counted"

    lock.release(target)
    assert entered.wait(5.0), "the other thread never entered after the second release"
    thread.join(5)
    assert not thread.is_alive()
    assert lock.held(target) == 0


def test_crossed_batches_do_not_deadlock(tmp_path: Path) -> None:
    """Two crossed batches [a, b] and [b, a]: both finish, on the fixed alphabetical order.

    The lock itself has a 5 s timeout: if the fixed order were broken, each batch would expire
    and the test would fail with LockTimeout -- it would not freeze.
    """
    lock_dir = tmp_path / "locks"
    path_a = tmp_path / "a.py"
    path_b = tmp_path / "b.py"

    def take(lock: FileLocks, paths: List[str]) -> List[bool]:
        got = lock.acquires(paths)
        time.sleep(0.05)
        lock.releases(paths)
        return got

    first = FileLocks(timeout=5.0, lock_dir=lock_dir)
    second = FileLocks(timeout=5.0, lock_dir=lock_dir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(take, first, [str(path_a), str(path_b)])
        future_b = pool.submit(take, second, [str(path_b), str(path_a)])
        result_a = future_a.result(timeout=20)
        result_b = future_b.result(timeout=20)

    assert result_a == [True, True]
    assert result_b == [True, True]
    assert first.held(path_a) == 0 and first.held(path_b) == 0
    assert second.held(path_a) == 0 and second.held(path_b) == 0


def test_acquires_answers_in_demanded_order(tmp_path: Path) -> None:
    """`acquires` answers PER DEMANDED ENTRY, not in the order the locks were taken."""
    lock = _locks(tmp_path)
    path_a = tmp_path / "a.py"
    path_b = tmp_path / "b.py"

    assert lock.acquire(path_a) is True
    got = lock.acquires([path_b, path_a])
    assert got == [True, False], "the answers are not aligned on the demanded order"

    lock.releases([path_b, path_a])
    assert lock.held(path_a) == 1, "the release dropped a depth the caller already held"
    assert lock.held(path_b) == 0
    lock.release(path_a)


def test_timeout_names_the_path_and_frees_it_afterwards(tmp_path: Path) -> None:
    """A living holder that waits too long makes the other raise LockTimeout -- then frees it."""
    lock_dir = tmp_path / "locks"
    holder = FileLocks(timeout=10.0, lock_dir=lock_dir)
    waiter = FileLocks(timeout=0.4, lock_dir=lock_dir)
    target = tmp_path / "a.py"
    released = threading.Event()

    def hold_long() -> None:
        with hold_file(holder, target):
            released.wait(10)

    thread = threading.Thread(target=hold_long)
    thread.start()
    time.sleep(0.2)

    started = time.monotonic()
    with pytest.raises(LockTimeout) as caught:
        waiter.acquire(target)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.35, "the timeout was ignored: the lock was given away too early"
    assert elapsed < 5.0, "the timeout was overshot for no reason"
    assert "a.py" in str(caught.value), "the message does not name the path: " + str(caught.value)

    released.set()
    thread.join(5)
    assert not thread.is_alive()

    started = time.monotonic()
    assert waiter.acquire(target) is True, "the lock stayed taken after the holder left"
    assert time.monotonic() - started < 2.0
    waiter.release(target)


def test_acquires_releases_everything_it_took_on_timeout(tmp_path: Path) -> None:
    """If the SECOND lock of a batch expires, the FIRST must not stay held (no ghost lock)."""
    lock_dir = tmp_path / "locks"
    holder = FileLocks(timeout=5.0, lock_dir=lock_dir)
    waiter = FileLocks(timeout=0.4, lock_dir=lock_dir)
    probe = FileLocks(timeout=0.4, lock_dir=lock_dir)
    path_b = tmp_path / "b.py"
    path_c = tmp_path / "c.py"

    # Alphabetical order: b.py is taken first, c.py is already held -- and held BY ANOTHER
    # THREAD, because a thread that held it would reenter instead of waiting (that is exactly
    # what reentrance is for).
    released = threading.Event()

    def hold_c() -> None:
        with hold_file(holder, path_c):
            released.wait(10)

    holder_thread = threading.Thread(target=hold_c)
    holder_thread.start()
    time.sleep(0.2)
    try:
        with pytest.raises(LockTimeout):
            waiter.acquires([path_c, path_b])
    finally:
        released.set()
        holder_thread.join(5)

    assert waiter.held(path_b) == 0
    assert probe.acquire(path_b) is True, "b.py stayed held by the batch that failed"
    probe.release(path_b)


def test_hold_file_releases_when_the_body_raises(tmp_path: Path) -> None:
    """A critical section that RAISES must still give the lock back."""
    lock = _locks(tmp_path)
    target = tmp_path / "a.py"

    with pytest.raises(ValueError):
        with hold_file(lock, target):
            assert lock.held(target) == 1
            raise ValueError("failure in the middle of the write")

    assert lock.held(target) == 0
    assert lock.acquire(target) is True, "the lock was not given back after an exception"
    lock.release(target)


def test_lock_files_are_dedicated_and_deterministic(tmp_path: Path) -> None:
    """The lock is a DEDICATED file (never the edited file), and its name derives from the path.

    No OS lock is ever taken on the edited file: it is replaced by `os.replace`, so the lock would
    stay on the old descriptor while the name already points somewhere else.
    """
    lock_dir = tmp_path / "locks"
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    before = target.read_bytes()

    first = FileLocks(timeout=2.0, lock_dir=lock_dir)
    second = FileLocks(timeout=2.0, lock_dir=lock_dir)
    first.acquire(target)
    try:
        files = sorted(p.name for p in lock_dir.iterdir())
        assert len(files) == 1, "the lock did not create exactly one dedicated file: " + repr(files)
        assert target.name not in files, "the edited file was used as the lock file"
        assert target.read_bytes() == before, "the edited file was touched while taking the lock"
        assert second.acquire(target) is False, "two instances of one process do not share the lock"
    finally:
        first.release(target)
        second.release(target)

    assert first.held(target) == 0
    assert second.held(target) == 0


@pytest.mark.skipif(os.name != "nt", reason="case-insensitivity is a Windows property")
def test_two_spellings_of_one_windows_path_share_one_lock(tmp_path: Path) -> None:
    """Two spellings of one Windows path must give THE SAME lock.

    `Path.resolve()` normalises the case only for the part that exists; the part that does not
    exist yet keeps the spelling it was given. Without lower-casing, `GHOST_LOWER.PY` and
    `ghost_lower.py` would be two locks, and two agents would write the same file while
    believing they were alone.
    """
    lock = _locks(tmp_path)
    lower = tmp_path / "ghost_lower.py"
    upper = tmp_path / "GHOST_LOWER.PY"
    assert str(lower) != str(upper)

    assert lock.acquire(lower) is True
    assert lock.acquire(upper) is False, "two spellings of one path gave two locks"
    assert lock.held(upper) == 2
    lock.release(lower)
    lock.release(upper)
    assert lock.held(lower) == 0


CHILD_TAKES_LOCK = """
import sys
import time

from ast_graph_server.filelock import FileLocks

lock = FileLocks(timeout=10.0, lock_dir=sys.argv[1])
lock.acquire(sys.argv[2])
print("HELD", flush=True)
time.sleep(600)
"""


def test_a_killed_holder_frees_the_lock(tmp_path: Path) -> None:
    """Acceptance criterion 5: the holder is KILLED and the tool must start again.

    The child takes the lock, announces it, then waits. The parent checks the lock is refused
    while the child lives, kills it, and must then get the lock within a few seconds. It is the
    operating system that frees the lock when the process dies: nothing to reclaim, nothing to
    date. A watchdog bounds the read of the child's stdout -- a child that never announces
    `HELD` must fail the test, not freeze it.
    """
    lock_dir = tmp_path / "locks"
    target = tmp_path / "a.py"
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")

    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_TAKES_LOCK, str(lock_dir), str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    watchdog = threading.Timer(30.0, child.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        announcement = child.stdout.readline()
        assert announcement.strip() == "HELD", (
            "the child did not take the lock: " + repr(announcement) + " " + repr(child.stderr.read())
        )

        parent = FileLocks(timeout=0.5, lock_dir=lock_dir)
        with pytest.raises(LockTimeout):
            parent.acquire(target)

        child.kill()
        child.wait(timeout=15)

        started = time.monotonic()
        assert parent.acquire(target) is True, "the lock was not freed by the death of its holder"
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, "the dead holder still owns the lock: %.2fs" % elapsed
        parent.release(target)
    finally:
        watchdog.cancel()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=15)

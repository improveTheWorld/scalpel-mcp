"""The edit path is ALONE in its file, and it rebases the numbers the caller read.

Two guarantees from docs/CHANTIER_VERROU_EDITION.md, pinned here at the level the SERVER uses
them -- the lock module and the rebaser have their own suites (tests/test_filelock.py,
tests/test_linetranslate.py); what is pinned here is the wiring in Patcher:

  * a READER of a file being edited WAITS. Not to avoid a torn read -- the write is already a
    temp file then os.replace -- but because its read would be stale by construction and would
    cost it a turn. A reader of ANOTHER file is not delayed: the lock is per file.
  * the call that edits RE-READS the block it just wrote (it returns the fresh numbered text),
    so it must never wait for itself: reentrance is what makes that possible.
  * an entry of a grouped call whose stamp is one version behind because ANOTHER ENTRY of the
    same call wrote the file is APPLIED, and the reply says the stamp is not reusable.
"""

import threading
import time
from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

MD = "# Guide\n\n## Part\n\n### 8. Alpha\n- a\n- b\n- c\n"
BLOCK = "docs/spec.md#part/alpha"


def make_env(base: Path):
    ws = base / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text(MD, encoding="utf-8")
    (ws / "docs" / "other.md").write_text("# Other\n\n## Part\n\nBody.\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, Patcher(config, db, indexer)


def test_a_reader_waits_for_the_file_being_edited(tmp_path: Path) -> None:
    """The reader of THAT file blocks until the writer is done, and is then served."""
    ws, patcher = make_env(tmp_path)
    target = str((ws / "docs" / "spec.md").resolve())
    inside = threading.Event()
    reader_done = threading.Event()

    patcher._edits_lock.acquire(target)
    try:
        def reading():
            inside.set()
            patcher.read_block(BLOCK)
            reader_done.set()

        thread = threading.Thread(target=reading)
        thread.start()
        inside.wait(5)
        assert not reader_done.wait(0.2), "the reader did not wait for the writer"
    finally:
        patcher._edits_lock.release(target)
    assert reader_done.wait(5), "the reader was never served after the writer let go"
    thread.join(5)


def test_a_reader_of_ANOTHER_file_is_not_delayed(tmp_path: Path) -> None:
    """Per FILE, never global: the whole point of the lock is that it costs nothing elsewhere."""
    ws, patcher = make_env(tmp_path)
    target = str((ws / "docs" / "spec.md").resolve())
    patcher._edits_lock.acquire(target)
    try:
        began = time.monotonic()
        patcher.read_block("docs/other.md#part")
        elapsed = time.monotonic() - began
    finally:
        patcher._edits_lock.release(target)
    assert elapsed < 0.5, "another file waited on a lock that is not its own"


def test_the_edit_re_reads_its_own_write_without_waiting_for_itself(tmp_path: Path) -> None:
    """Reentrance, and it is not decoration: edit_block re-reads the block to answer. A lock that
    did not count the depth would deadlock the very call that holds it."""
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(BLOCK)
    answer = patcher.edit_block(
        BLOCK, ops=[{"replace": [2, 2], "content": "- A"}], expected_hash=read["content_hash"]
    )
    assert answer["status"] == "success", answer
    assert "2 | - A" in answer["numbered_text"], answer
    assert patcher._edits_lock.held(str((ws / "docs" / "spec.md").resolve())) == 0


def test_a_stamp_behind_because_of_the_same_call_is_applied_and_says_so(tmp_path: Path) -> None:
    """The refusal this chantier removes: the tool moved the lines itself, so it rebases them."""
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(BLOCK)
    reply = patcher._edit_batch([
        {"path": BLOCK, "ops": [{"insert_after": 1, "content": "- X"}],
         "expected_hash": read["content_hash"]},
        {"path": BLOCK, "ops": [{"replace": [2, 2], "content": "- A"}],
         "expected_hash": read["content_hash"]},
    ])
    assert reply["applied"] == 2, reply
    assert reply["refused"] == 0, reply
    second = reply["results"][1]
    assert second["expected_hash_rebased"] is True, second
    assert second["rebased_by"] == 0, second
    assert second["rebase"]["your_lines"] == [2], second
    numbered = patcher.read_block(BLOCK)["numbered_text"]
    # The caller read "- a" on line 2; entry 0 inserted ABOVE it, so the write must land on the
    # line it read -- which is now line 3 -- and never on the inserted one.
    assert "2 | - X" in numbered and "3 | - A" in numbered, numbered


def test_two_entries_that_write_the_SAME_line_are_a_real_conflict(tmp_path: Path) -> None:
    """The one refusal that survives, with the line AND the entry that got there first named."""
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(BLOCK)
    reply = patcher._edit_batch([
        {"path": BLOCK, "ops": [{"replace": [2, 2], "content": "- FIRST"}],
         "expected_hash": read["content_hash"]},
        {"path": BLOCK, "ops": [{"replace": [2, 2], "content": "- SECOND"}],
         "expected_hash": read["content_hash"]},
    ])
    assert reply["applied"] == 1 and reply["refused"] == 1, reply
    refused = reply["results"][1]
    assert refused["conflict"] is True, refused
    assert refused["rebased_by"] == 0, refused
    page = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "- FIRST" in page and "- SECOND" not in page


def test_a_writer_OUTSIDE_the_tool_is_still_refused(tmp_path: Path) -> None:
    """The other surviving refusal: nobody can translate a move this call never made."""
    ws, patcher = make_env(tmp_path)
    stale = patcher.read_block(BLOCK)["content_hash"]
    spec = ws / "docs" / "spec.md"
    spec.write_text(MD.replace("- b", "- B"), encoding="utf-8")
    before = spec.read_text(encoding="utf-8")
    reply = patcher._edit_batch([
        {"path": BLOCK, "ops": [{"insert_after": 4, "content": "- x"}], "expected_hash": stale},
    ])
    assert reply["applied"] == 0, reply
    assert "Stale read" in reply["results"][0]["error"], reply
    assert spec.read_text(encoding="utf-8") == before


def test_an_unreadable_operation_is_not_reported_as_a_conflict(tmp_path: Path) -> None:
    """The THIRD refusal, declared for what it is.

    A payload the rebaser cannot PARSE is neither of the brief's two legitimate refusals:
    nothing is stale, and no second entry wrote anything. It used to leave through the
    CONFLICT return, because one return served both families -- so a batch of a SINGLE
    entry answered "the tool cannot know which one you meant to win: serving both would let
    the second overwrite a line the first already rewrote" about a second entry that did
    not exist, carrying conflict: true and rebased_by: null. Six distinct payloads were
    measured taking that branch (a bogus key, a non-object op, a malformed span, a span
    outside the block, a non-numeric anchor, a string in place of the list).
    """
    ws, patcher = make_env(tmp_path)
    reply = patcher._edit_batch([
        {"path": BLOCK, "ops": [{"frobnicate": 1}]},
    ])
    assert reply["applied"] == 0 and reply["refused"] == 1, reply
    refused = reply["results"][0]
    assert refused.get("conflict") is not True, refused
    assert "rebased_by" not in refused, refused
    assert "serving both would let the second overwrite" not in refused["error"], refused
    assert refused["unreadable_operation"] is True, refused
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == MD


def test_a_readable_stamp_with_an_unreadable_op_is_not_a_writer_outside(tmp_path: Path) -> None:
    """The refusal the tool caused ITSELF, and the one it must never print again.

    An entry whose own payload cannot be read returns from _edit_block_locked BEFORE
    _remember_stamps runs, so its block is absent from the stamp table. Reading that
    ABSENCE as "not the version the call started from" printed "Stale read ... a writer
    outside these tools moved it" on a file nobody had touched -- and the refusal carried
    its own expected_hash EQUAL to its own content_hash, contradicting itself in the same
    payload. The repair it prescribed ("re-send your operations against these numbers")
    could never work: no line number repairs an operation that cannot be read.
    """
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(BLOCK)
    reply = patcher._edit_batch([
        {"path": BLOCK, "ops": [{"frobnicate": 1}], "expected_hash": read["content_hash"]},
    ])
    assert reply["applied"] == 0 and reply["refused"] == 1, reply
    refused = reply["results"][0]
    assert "Stale read" not in refused["error"], refused
    assert "writer outside" not in refused["error"], refused
    assert refused["unreadable_operation"] is True, refused
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == MD


def test_the_real_conflict_keeps_its_author_and_its_wording(tmp_path: Path) -> None:
    """The negative control: narrowing one branch must not silence the other."""
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(BLOCK)
    reply = patcher._edit_batch([
        {"path": BLOCK, "ops": [{"replace": [2, 2], "content": "- FIRST"}],
         "expected_hash": read["content_hash"]},
        {"path": BLOCK, "ops": [{"replace": [2, 2], "content": "- SECOND"}],
         "expected_hash": read["content_hash"]},
    ])
    assert reply["applied"] == 1 and reply["refused"] == 1, reply
    refused = reply["results"][1]
    assert refused["conflict"] is True, refused
    assert refused["rebased_by"] == 0, refused
    assert "line 2 -- entry 0 of this call wrote on it" in refused["error"], refused
    assert "- SECOND" not in (ws / "docs" / "spec.md").read_text(encoding="utf-8")

"""Line-addressed editing: the primitives behind read_block / edit_block.

read_block shows a block with 1-based line numbers; edit_block takes operations on
those numbers. The version hash pins the numbers, so a stale read is refused
rather than applied to the wrong lines.
"""

import hashlib
from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.lineops import apply_ops, numbered
from ast_graph_server.parsers.base import compute_content_hash
from ast_graph_server.patcher import Patcher


def literal(content, _column):
    return content


def test_lineops_replace_delete_insert() -> None:
    text = "a\nb\nc\nd\n"
    assert apply_ops(text, [{"replace": [2, 3], "content": "B"}], literal).text == "a\nB\nd\n"
    assert apply_ops(text, [{"delete": [2, 2]}], literal).text == "a\nc\nd\n"
    assert apply_ops(text, [{"insert_after": 0, "content": "top"}], literal).text == "top\na\nb\nc\nd\n"
    assert apply_ops(text, [{"insert_after": 4, "content": "end"}], literal).text == "a\nb\nc\nd\nend\n"


def test_lineops_apply_bottom_up_so_ranges_do_not_shift() -> None:
    """Two edits in one call: the later lines must not move because of the earlier edit."""
    text = "a\nb\nc\nd\n"
    result = apply_ops(
        text,
        [{"replace": [1, 1], "content": "A\nA2"}, {"replace": [4, 4], "content": "D"}],
        literal,
    )
    assert result.text == "A\nA2\nb\nc\nD\n"



def test_an_insert_and_a_range_edit_at_the_same_index_compose_bottom_up() -> None:
    """`insert_after: n` lands after the line that `replace: [n+1, n+1]` rewrote.

    Measured on mapab2 b r2 of the base f1fb645: the pair answered `status: success`, but
    the inserted heading had been overwritten by the renumbered line meant to FOLLOW it,
    and the original line still stood below. The agent declared the misordered list and
    repaired it in a second call. The promise is order-independence, so both listings must
    give the same text.
    """
    text = "L1\nL2\nL3\nL4\n"
    inserted_first = apply_ops(
        text,
        [{"insert_after": 2, "content": "NEW\nNEW2"}, {"replace": [3, 3], "content": "TROIS"}],
        literal,
    )
    replaced_first = apply_ops(
        text,
        [{"replace": [3, 3], "content": "TROIS"}, {"insert_after": 2, "content": "NEW\nNEW2"}],
        literal,
    )
    assert inserted_first.error is None
    assert inserted_first.text == "L1\nL2\nNEW\nNEW2\nTROIS\nL4\n"
    assert replaced_first.text == inserted_first.text

def test_lineops_reject_overlap_and_out_of_range() -> None:
    text = "a\nb\nc\n"
    assert apply_ops(text, [{"replace": [1, 2], "content": "x"}, {"delete": [2, 3]}], literal).error
    assert apply_ops(text, [{"replace": [1, 9], "content": "x"}], literal).error
    assert apply_ops(text, [{"delete": [0, 1]}], literal).error
    # Two inserts at the same anchor are chained in FIFO order.
    res = apply_ops(text, [{"insert_after": 0, "content": "x"}, {"insert_after": 0, "content": "y"}], literal)
    assert res.error is None
    assert res.text == "x\ny\na\nb\nc\n"


def test_lineops_rebase_the_inserted_content() -> None:
    def indent(content, column):
        return "\n".join((column + line) if line else line for line in content.splitlines())

    text = "class A:\n    def m(self):\n        pass\n"
    result = apply_ops(text, [{"insert_after": 2, "content": "return 1"}], indent)
    assert result.text == "class A:\n    def m(self):\n        return 1\n        pass\n"


def test_lineops_insert_after_skips_blank_lines_for_indentation() -> None:
    """A blank line after the anchor must not put the insert at column 0."""
    def indent(content, column):
        return "\n".join((column + line) if line else line for line in content.splitlines())

    text = "class C\n{\n    int A;\n\n    int B;\n}\n"
    result = apply_ops(text, [{"insert_after": 3, "content": "int X;"}], indent)
    assert result.text == "class C\n{\n    int A;\n    int X;\n\n    int B;\n}\n"


def test_lineops_replace_uses_the_first_non_blank_line_of_the_range() -> None:
    def indent(content, column):
        return "\n".join((column + line) if line else line for line in content.splitlines())

    text = "try:\n\n    pass\nexcept:\n    pass\n"
    result = apply_ops(text, [{"replace": [1, 3], "content": "try:\n    pass"}], indent)
    assert result.text == "try:\n    pass\nexcept:\n    pass\n"


def test_numbered_is_one_based_and_right_aligned() -> None:
    assert numbered("a\nb") == "1 | a\n2 | b"
    many = numbered("\n".join(str(i) for i in range(10)))
    assert many.splitlines()[0] == " 1 | 0"
    assert many.splitlines()[-1] == "10 | 9"


def make_patcher(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("class A:\n    def m(self):\n        return 1\n", encoding="utf-8")
    (ws / "src" / "b.py").write_text(
        "class B:\n    def m(self):\n        if True:\n            return 1\n", encoding="utf-8"
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


def test_read_block_numbers_lines_and_returns_a_hash(tmp_path: Path) -> None:
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")
    assert block["status"] == "success"
    assert block["kind"] == "symbol"
    assert block["line_count"] == 2
    assert block["numbered_text"].startswith("1 | ")
    assert len(block["content_hash"]) == 8


def test_block_metrics_are_read_block_numbers(tmp_path: Path) -> None:
    """A block a discovery tool showed is editable with no read_block in between."""
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/b.py::B.m")
    metrics = patcher.block_metrics("src/b.py::B.m")
    assert metrics is not None
    assert metrics["numbered_text"] == block["numbered_text"]
    assert metrics["content_hash"] == block["content_hash"]
    assert metrics["line_count"] == block["line_count"]
    # Edit using ONLY what the discovery path would have handed over.
    result = patcher.edit_block(
        "src/b.py::B.m",
        ops=[{"replace": [3, 3], "content": "            return 9"}],
        expected_hash=metrics["content_hash"],
    )
    assert result["status"] == "success", result
    assert "            return 9" in (ws / "src" / "b.py").read_text(encoding="utf-8")


def test_edit_block_replaces_a_line_by_number(tmp_path: Path) -> None:
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")

    # The fragment carries the indentation read_block showed (8 columns here), and it
    # is inserted literally: no re-basing, so the agent's own indentation is final.
    result = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 2"}],
        expected_hash=block["content_hash"],
    )

    assert result["status"] == "success", result
    assert "\n        return 2\n" in (ws / "src" / "a.py").read_text(encoding="utf-8")
    assert result["numbered_text"].startswith("1 | ")
    assert result["content_hash"] != block["content_hash"]


def test_edit_block_indentation_contract(tmp_path: Path) -> None:
    """The fragment is literal, the block base is anchored to its AST level.

    Both halves were measured on a real run: the agent sends a fragment already at
    the column it read (12/12), and writes a whole-block rewrite from column 0.
    """
    ws, db, patcher = make_patcher(tmp_path)
    path = ws / "src" / "b.py"
    block = patcher.read_block("src/b.py::B.m")
    assert block["numbered_text"] == "1 |     def m(self):\n2 |         if True:\n3 |             return 1"

    # A fragment deeper than the block base keeps its own column: no re-basing.
    deeper = patcher.edit_block(
        "src/b.py::B.m",
        ops=[{"replace": [3, 3], "content": "            return 2"}],
        expected_hash=block["content_hash"],
    )
    assert deeper["status"] == "success", deeper
    assert "\n        if True:\n            return 2\n" in path.read_text(encoding="utf-8")

    # Rewriting the whole block from column 0 re-anchors it under its container.
    block = patcher.read_block("src/b.py::B.m")
    rewritten = patcher.edit_block(
        "src/b.py::B.m",
        ops=[{"replace": [1, 3], "content": "def m(self):\n    if True:\n        return 7"}],
        expected_hash=block["content_hash"],
    )
    assert rewritten["status"] == "success", rewritten
    assert path.read_text(encoding="utf-8") == (
        "class B:\n    def m(self):\n        if True:\n            return 7\n"
    )


def test_a_stale_read_refusal_carries_the_fresh_block(tmp_path: Path) -> None:
    """The guard already re-read the block, so the repair must not cost a turn.

    Measured on the 25/09 corpus (subagent s3): 12 stale-read refusals, none carried
    an actionable_hint, and every one cost at least 2 turns because the caller had to
    call read_block again before it could re-send its operations. The refusal now
    returns the fresh buffer, and the operations can be replayed from it directly.
    """
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")
    first = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 9"}],
        expected_hash=block["content_hash"],
    )
    assert first["status"] == "success"

    stale = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 3"}],
        expected_hash=block["content_hash"],
    )
    assert stale["status"] == "error"
    assert "Stale" in stale["error"]
    # The fresh buffer is IN the refusal and matches a fresh read_block exactly.
    assert stale["content_hash"] != block["content_hash"]
    assert stale["numbered_text"]
    assert stale["line_count"] == len(stale["numbered_text"].splitlines())
    assert stale["actionable_hint"]
    fresh = patcher.read_block("src/a.py::A.m")
    assert stale["numbered_text"] == fresh["numbered_text"]
    assert stale["content_hash"] == fresh["content_hash"]
    # And the repair succeeds in ONE call, using only what the refusal returned.
    repaired = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 3"}],
        expected_hash=stale["content_hash"],
    )
    assert repaired["status"] == "success", repaired
    assert "        return 3" in (ws / "src" / "a.py").read_text(encoding="utf-8")


def test_a_replace_that_re_emits_the_next_line_is_warned(tmp_path: Path) -> None:
    """The range is INCLUSIVE: re-emitting line b+1 duplicates it.

    The complaint recurred in ~9 of 21 campaign runs. The previous count-and-gap check
    missed a genuine 4-line-apart duplication (a r6) and warned about two real
    `builder = builder.Config(...)` statements (b r2). The boundary rule is exact.
    """
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")
    # Block line 2 is "        return 1"; replacing only line 1 while re-emitting line 2
    # leaves it standing twice.
    result = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [1, 1], "content": "    def m(self):\n        return 1"}],
        expected_hash=block["content_hash"],
    )
    # REFUSED before any write, with the block left untouched and its current numbered text
    # returned: the old apply-and-warn behaviour left the copy in the file (base f831a4e, four
    # times in one run) and cost the repair turn anyway.
    assert result["status"] == "error", result
    assert result["duplicated_line"] == "return 1"
    assert "INCLUSIVE" in result["error"]
    # The two lines are NAMED: "within a few lines of the range" was read as a proximity rule.
    assert "line 2 and line 3" in result["error"], result["error"]
    assert patcher.read_block("src/a.py::A.m")["content_hash"] == block["content_hash"]


def test_an_insert_that_re_emits_a_nearby_line_is_warned_not_refused(tmp_path: Path) -> None:
    """An INSERT has no range: the refusal's repair ("extend the range") does not exist for it.

    Measured on a r1 of the base fba3450 (t11 -> t13): the agent was adding a SECOND loop whose body
    is the same statement as the first ("builder = builder.Config(key, value);"), was refused three
    times, and got out only by renaming its own local variables (t14) -- the tool dictating the code.
    The copy is named with its two line numbers and the write goes through.
    """
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")
    result = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"insert_after": 2, "content": "        return 1"}],
        expected_hash=block["content_hash"],
    )
    assert result["status"] == "success", result
    warning = (result.get("warnings") or [""])[0]
    assert "return 1" in warning and "twice" in warning, result
    # BOTH copies are named, so the caller can check the claim against the numbered text it holds.
    assert "line 2 and line 3" in warning, warning
    assert (ws / "src" / "a.py").read_text(encoding="utf-8").count("return 1") == 2


def test_only_a_range_op_justifies_the_duplicate_refusal() -> None:
    """The refusal is kept where it diagnoses something real: a range that was too short."""
    assert Patcher._has_range_op([{"insert_after": 1, "content": "x"}]) is False
    assert Patcher._has_range_op([{"replace": [1, 2], "content": "x"}]) is True
    assert Patcher._has_range_op([{"delete": [1, 1]}]) is True
    assert Patcher._has_range_op(
        [{"insert_after": 1, "content": "x"}, {"replace": [3, 3], "content": "y"}]
    ) is True
    assert Patcher._duplicate_line_numbers("a\nb\nb\n", "b") == [2, 3]
    assert Patcher._duplicate_line_numbers("a\nb\n", "zz") == []


def test_a_ubiquitous_line_is_not_reported() -> None:
    """A line the block already carried many times is not a copy this edit made.

    '/// </summary>' occurs eleven times in one C# property group; adding a doc comment
    legitimately makes it twelve, and warning about that was the recurring false positive
    (a r1 and a r3 of the base 9e506c5).
    """
    before = "/// <summary>\n/// </summary>\n/// <summary>\n/// </summary>\nint x;\n"
    after = "/// <summary>\n/// </summary>\n/// <summary>\n/// </summary>\n/// </summary>\nint x;\n"
    assert Patcher._newly_repeated_line(before, after) is None


def test_a_duplication_four_lines_apart_is_reported() -> None:
    """A unique line repeated within a few lines is the copy a short range leaves.

    Declared on a r6 of the base b2c82d5: "with lines 15-18 duplicating lines 11-14, and
    no warning was emitted".
    """
    before = "aaaaaaaa\nbbbbbbbb\ncccccccc\ndddddddd\neeeeeeee\nffffffff\n"
    after = "aaaaaaaa\nbbbbbbbb\ncccccccc\ndddddddd\neeeeeeee\nffffffff\ncccccccc\n"
    # BOTH FACTS ARE RETURNED: the line, AND the position it held in what the CALLER read.
    # Measured twice (fix1 t27, s10 t27): a refusal announced "at line 12 and line 16" for a
    # 12-line block -- positions in the AFTER text the caller never saw -- and the agent
    # answered that it had to "re-derive the cause myself". "cccccccc" was line 3 before, and
    # it is line 3 that the caller can act on.
    assert Patcher._newly_repeated_line(before, after) == ("cccccccc", 3)


def test_two_real_statements_far_apart_are_not_a_duplicate() -> None:
    """Two identical statements far apart are two statements, not a copy.

    'builder = builder.Config(...)' appeared once and twice after the edit, sixteen lines
    apart, both real statements of a fluent chain (run 38, base b2c82d5).
    """
    filler = "filler xxxx\n" * 13
    before = "line 1 xxxxxxxx\nbuilder = builder.Config(key, value);\n" + filler + "end\n"
    after = "line 1 xxxxxxxx\nbuilder = builder.Config(key, value);\n" + filler + "builder = builder.Config(key, value);\nend\n"
    assert Patcher._newly_repeated_line(before, after) is None


def test_common_syntax_guard_lines_are_not_reported_as_duplicates() -> None:
    """Guard lines such as 'if self.suppression:' or 'return None' repeat legitimately."""
    before = "def foo():\n    if self.suppression:\n        pass\n"
    after = "def foo():\n    if self.suppression:\n        pass\n    if self.suppression:\n        return None\n"
    assert Patcher._newly_repeated_line(before, after) is None


def test_a_replace_that_consumes_the_boundary_blank_line_is_named(tmp_path: Path) -> None:
    """The range is INCLUSIVE: a range ending on a BLANK line deletes that blank line.

    Observed on a r1 of the base 0489adc: `replace [17,18]` where line 18 was blank glued a new
    property to the `/// <summary>` of the next one; the reply said `success` and only the
    returned text showed it. The answer NAMES the fact (the agent read it and repaired) -- and it
    is not a refusal, because deleting a blank line is a legitimate edit.
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text(
        "class A:\n    def m(self):\n        x = 1\n\n        return x\n\n    def n(self):\n        return 2\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    block = patcher.read_block("src/a.py::A.m")
    assert block["line_count"] == 4  # the blank line 3 is INSIDE the block
    result = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 3], "content": "        x = 10"}],
        expected_hash=block["content_hash"],
    )
    assert result["status"] == "success", result
    assert any("line 3" in w and "BLANK" in w for w in result.get("warnings") or []), result
    # The blank line IS gone: the warning describes the file, it does not forbid the edit.
    assert (ws / "src" / "a.py").read_text(encoding="utf-8").startswith(
        "class A:\n    def m(self):\n        x = 10\n        return x\n"
    )


def test_keeping_the_boundary_blank_line_is_silent() -> None:
    """Keeping the blank line is what most edits do -- the warning must not cry wolf."""
    before = "    def m(self):\n        x = 1\n\n        return x"
    # ends on the blank line, content without a trailing blank line -> named
    assert Patcher._blank_boundary_lost([{"replace": [2, 3], "content": "        x = 10"}], before) == 3
    # ...with a trailing blank line -> the blank line survives -> silent
    assert Patcher._blank_boundary_lost([{"replace": [2, 3], "content": "        x = 10\n"}], before) is None
    # the range ends on a NON-blank line -> silent
    assert Patcher._blank_boundary_lost([{"replace": [2, 2], "content": "        x = 10"}], before) is None
    # a `replace` that is only a deletion has no content to end anywhere -> silent
    assert Patcher._blank_boundary_lost([{"delete": [3, 3]}], before) is None


def test_an_insert_after_the_last_line_is_reported_as_outside_the_block(tmp_path: Path) -> None:
    """A success whose block did not move is only honest if the reply SAYS the write went elsewhere.

    Measured on a r0 of the base 4f3ae58 (t14): an insert_after the LAST line of
    Spark::IsLocalMode wrote the new method into the FILE, but the reply reported a diff for the
    block -- "status: success, - 3 lines, + 39 lines" -- with an unchanged content_hash and
    line_count. The agent read that as "the insert silently did not apply" (t15), redid the work
    with insert_block and would have written the helper TWICE. The fact is named, and the two
    evidence fields describe the block the write CREATED: the addressed block's own hash is the one
    the caller already sent, so returning it as proof of the write proved nothing.
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Read.Spark.cs").write_text(
        "public static class Spark\n{\n    private static bool IsLocalMode(string master)\n"
        '        => master.StartsWith("local[", StringComparison.OrdinalIgnoreCase)\n'
        '        || master.Equals("local", StringComparison.OrdinalIgnoreCase);\n}\n',
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    block = patcher.read_block("src/Read.Spark.cs::Spark::IsLocalMode")
    assert block["line_count"] == 3
    result = patcher.edit_block(
        "src/Read.Spark.cs::Spark::IsLocalMode",
        ops=[{"insert_after": 3, "content": "\n    private static void ApplyAuth()\n    {\n    }\n"}],
        expected_hash=block["content_hash"],
    )
    assert result["status"] == "success", result
    assert result["written_outside_block"] is True
    # The evidence fields describe what was WRITTEN, and name it: the addressed block's own hash
    # is the one the caller sent, so it proves nothing about the write (W5 4.1).
    written = result["written_block_id"]
    assert "ApplyAuth" in written, result
    assert result["content_hash"] == patcher.block_metrics(written)["content_hash"]
    assert "ApplyAuth" in result["numbered_text"]
    assert result["content_hash"] != block["content_hash"]
    assert result["line_count"] == patcher.block_metrics(written)["line_count"]
    assert any("NOT inside" in one for one in result.get("warnings") or []), result
    assert "ApplyAuth" in (ws / "src" / "Read.Spark.cs").read_text(encoding="utf-8")
    assert "ApplyAuth" in str(result.get("file_listing")), result.get("file_listing")
    assert "created_symbols" in result
    assert any("ApplyAuth" in sym["id"] for sym in result["created_symbols"])


def test_a_hash_from_another_file_names_the_block_it_belongs_to(tmp_path: Path) -> None:
    """A stamp carried from another block must NAME that block -- not blame a change.

    Measured on the corpus (30/09/2026): 53 stale-read refusals, 26 with hashes differing at the
    FIRST character (a stamp from another block, never a version drift), and none of them named the
    owner. Observed on a r2 of the base fdb4b18 (t13, t22): the agent carried the hash the Dossier
    attached to SparkConnectOptions.cs into the test class and into guidance.md, and the reply said
    "the block changed since you read it" -- false, the block had not moved.
    """
    ws, db, patcher = make_patcher(tmp_path)
    other = patcher.read_block("src/b.py::B.m")
    result = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [1, 1], "content": "    def m(self):"}],
        expected_hash=other["content_hash"],
    )
    assert result["status"] == "error", result
    assert result["expected_hash_belongs_to"] == "src/b.py::B::m", result
    assert "Wrong block" in result["error"], result["error"]
    # The block that was addressed is untouched and its own stamp is returned.
    assert patcher.read_block("src/a.py::A.m")["content_hash"] != other["content_hash"]


def test_a_stale_refusal_says_where_the_hashes_differ(tmp_path: Path) -> None:
    """Two 64-char hashes that differ at one character look identical.

    On a r2 the agent re-sent its old hash four times because it read the returned one
    as identical (they differed at character 40, e vs f). The refusal now says where.
    """
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")
    patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 9"}],
        expected_hash=block["content_hash"],
    )
    stale = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 3"}],
        expected_hash=block["content_hash"],
    )
    assert stale["status"] == "error"
    assert stale["expected_hash"] == block["content_hash"]
    assert stale["content_hash"] != block["content_hash"]
    assert 1 <= stale["hash_differs_at"] <= 8
    assert "differ at character" in stale["actionable_hint"]


def test_edit_block_refuses_a_stale_hash(tmp_path: Path) -> None:
    ws, db, patcher = make_patcher(tmp_path)
    block = patcher.read_block("src/a.py::A.m")
    first = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "        return 9"}],
        expected_hash=block["content_hash"],
    )
    assert first["status"] == "success"

    stale = patcher.edit_block(
        "src/a.py::A.m",
        ops=[{"replace": [2, 2], "content": "return 3"}],
        expected_hash=block["content_hash"],
    )
    assert stale["status"] == "error"
    assert "Stale" in stale["error"]
    assert "return 3" not in (ws / "src" / "a.py").read_text(encoding="utf-8")


def test_a_stale_hash_on_a_file_id_never_offers_an_empty_block_as_the_fresh_one(tmp_path: Path) -> None:
    """A file id IS a block, and its own text is EMPTY when the first line is a heading.

    The generic stale refusal then hands that empty buffer back as "the FRESH block ...
    returned below" and prescribes a replay "using the line numbers in numbered_text above"
    -- there are none, and the replay answers "op 0: replace range must be inside 1..0".
    Reported on the corpus (30/09/2026): 10 such refusals, 3 followed by a second identical
    refusal, 2 by that out-of-range error. The refusal must name what the identifier IS, and
    a remedy that exists.
    """
    ws, db, patcher = make_patcher(tmp_path)
    (ws / "docs").mkdir()
    (ws / "docs" / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- first\n", encoding="utf-8")
    patcher.indexer.index_file(ws / "docs" / "CHANGELOG.md")

    section = patcher.read_block("docs/CHANGELOG.md::Changelog::1.0.0")
    stale = section["content_hash"][:-1] + ("0" if section["content_hash"][-1] != "0" else "1")
    refusal = patcher.edit_block("docs/CHANGELOG.md", ops=[{"replace": [1, 1], "content": "x"}], expected_hash=stale)

    assert refusal["status"] == "error", refusal
    assert refusal["content_hash"] == compute_content_hash(b""), refusal
    assert refusal["line_count"] == 0 and refusal["numbered_text"] == ""
    assert refusal["root_text_empty"] is True, refusal
    assert "empty" in refusal["error"].lower(), refusal["error"]
    # The stamp the caller sent is not this block's, and the refusal says so.
    assert refusal["expected_hash"] == stale, refusal
    assert refusal["hash_differs_at"] >= 1, refusal
    assert "using the line numbers in numbered_text above" not in refusal["actionable_hint"]
    assert "read_block(" in refusal["actionable_hint"]
    inserted = patcher.edit_block("docs/CHANGELOG.md", ops=[{"insert_after": 0, "content": "Preamble."}], expected_hash=refusal["content_hash"])
    assert inserted["status"] == "success", inserted


def test_a_stale_hash_on_a_zero_byte_file_does_not_claim_a_heading_caused_it(tmp_path: Path) -> None:
    """A 0-byte .md has NO first line: the empty root has TWO causes, and neither may be invented.

    Found by an independent verification of the previous fix: the refusal asserted "because its
    first line is a heading", a cause that does not exist on a file with no lines at all. The
    empty content is reached by BOTH routes -- a file with no heading makes _document_root_end
    return len(text), a file whose first line is a heading makes it return 0 -- so the message
    must hold for both, and the remedy must work on both.
    """
    ws, db, patcher = make_patcher(tmp_path)
    (ws / "docs").mkdir()
    (ws / "docs" / "EMPTY.md").write_bytes(b"")
    patcher.indexer.index_file(ws / "docs" / "EMPTY.md")

    stale = hashlib.sha256(b"a hash from some other block").hexdigest()
    refusal = patcher.edit_block("docs/EMPTY.md", ops=[{"replace": [1, 1], "content": "x"}], expected_hash=stale)

    assert refusal["status"] == "error", refusal
    assert refusal["content_hash"] == compute_content_hash(b""), refusal
    assert refusal["line_count"] == 0 and refusal["numbered_text"] == ""
    assert refusal["root_text_empty"] is True, refusal
    assert refusal["expected_hash"] == stale and refusal["hash_differs_at"] >= 1, refusal
    assert "empty" in refusal["error"].lower(), refusal["error"]
    # The file has NO first line, so no cause may claim one -- and it has no child block either.
    assert "first line" not in refusal["error"].lower(), refusal["error"]
    assert "a child block" not in refusal["error"].lower(), refusal["error"]
    # Both causes are named, neither one alone.
    assert "either the file itself is empty" in refusal["error"], refusal["error"]
    assert "sits under a heading" in refusal["error"], refusal["error"]
    assert "using the line numbers in numbered_text above" not in refusal["actionable_hint"]
    assert "read_block(" in refusal["actionable_hint"]
    inserted = patcher.edit_block("docs/EMPTY.md", ops=[{"insert_after": 0, "content": "First line."}], expected_hash=refusal["content_hash"])
    assert inserted["status"] == "success", inserted
    assert (ws / "docs" / "EMPTY.md").read_text(encoding="utf-8") == "First line.\n"


def test_a_stale_hash_on_a_file_id_with_its_own_text_keeps_the_generic_refusal(tmp_path: Path) -> None:
    """The empty-root refusal is for an EMPTY root only: a non-empty one is untouched.

    Two non-empty shapes: a file whose own text is a real preamble, and a file with no
    heading at all (its root IS the whole file). Both still return the fresh buffer and the
    "replay against these numbers" hint -- the numbers EXIST there, so that prescription is
    not the impasse it is on a 0-line root.
    """
    ws, db, patcher = make_patcher(tmp_path)
    (ws / "docs").mkdir()
    (ws / "docs" / "PRE.md").write_text("Preamble line.\n\n# Title\n\nBody.\n", encoding="utf-8")
    (ws / "docs" / "NOHEAD.md").write_text("just one line\nand another\n", encoding="utf-8")
    patcher.indexer.index_file(ws / "docs" / "PRE.md")
    patcher.indexer.index_file(ws / "docs" / "NOHEAD.md")

    for path in ("docs/PRE.md", "docs/NOHEAD.md"):
        fresh = patcher.read_block(path)
        assert fresh["content_hash"] != hashlib.sha256(b"").hexdigest(), fresh
        assert "root_text_empty" not in fresh, fresh
        stale = fresh["content_hash"][:-1] + ("0" if fresh["content_hash"][-1] != "0" else "1")
        refusal = patcher.edit_block(path, ops=[{"replace": [1, 1], "content": "x"}], expected_hash=stale)
        assert refusal["status"] == "error", refusal
        assert refusal.get("root_text_empty") is not True, refusal
        assert refusal["line_count"] > 0 and refusal["numbered_text"], refusal
        # The fresh buffer is the one a re-read gives: the old contract, unchanged.
        assert refusal["numbered_text"] == fresh["numbered_text"], refusal
        assert refusal["content_hash"] == fresh["content_hash"], refusal
        assert "using the line numbers in numbered_text above" in refusal["actionable_hint"], refusal


def test_a_replace_that_swallows_the_bullet_under_a_title_is_named(tmp_path: Path) -> None:
    """`replace [5,6]` on a titled item drops the bullet at line 6 -- refused preventively (#26).

    Measured in `p1_r16` t41, `p1_r17` t27, and `v0.4.1` `run14` t17 / `run21` t16: when a replace
    range covers a titled item AND its sub-bullet while the replacement content carries no bullet,
    preventing the write saves the repair turn and leaves the file intact.
    """
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text(
        "# Plan\n\n## Part\n\n"
        "1. **Alpha:**\n   - bullet A\n"
        "2. **Beta:**\n   - bullet B\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    block = patcher.read_block("docs/spec.md#part")
    result = patcher.edit_block(
        "docs/spec.md#part",
        ops=[{"replace": [5, 6], "content": "2. **Beta:**"}],
        expected_hash=block["content_hash"],
    )
    assert result["status"] == "error", result
    assert "bullet" in result["error"].lower() and "line 6" in result["error"], result
    assert result.get("dropped_bullet_line") == 6
    # The bullet is preserved because the destructive replace was refused before writing.
    assert "bullet B" in (ws / "docs" / "spec.md").read_text(encoding="utf-8")


def test_keeping_or_replacing_the_bullet_is_silent() -> None:
    """The detector must not cry wolf on an ordinary edit that keeps its bullet.

    The defect is the BARE title: the content carries no bullet at all while the range dropped
    one. A rewrite that keeps or replaces the bullet never matches.
    """
    before = "1. **Alpha:**\n   - bullet A\n2. **Beta:**\n   - bullet B"
    # A rewrite that re-emits the bullet verbatim -> silent.
    assert Patcher._heading_range_drops_a_bullet(
        [{"replace": [3, 4], "content": "2. **Beta:**\n   - bullet B"}], before
    ) is None
    # A rewrite that replaces the bullet with another bullet -> silent (the content HAS a bullet).
    assert Patcher._heading_range_drops_a_bullet(
        [{"replace": [3, 4], "content": "2. **Beta:**\n   - bullet B (v2)"}], before
    ) is None
    # The unit-level shape of the defect IS named, with the line of the caller's read.
    assert Patcher._heading_range_drops_a_bullet(
        [{"replace": [3, 4], "content": "2. **Beta:**"}], before
    ) == (4, "- bullet B")
    # A single-line replace cannot swallow its neighbour.
    assert Patcher._heading_range_drops_a_bullet(
        [{"replace": [3, 3], "content": "2. **Beta:**"}], before
    ) is None

def test_lineops_error_clarifies_block_relative_lines() -> None:
    text = "line1\nline2\n"
    res = apply_ops(text, [{"replace": [1, 99], "content": "x"}], literal)
    assert res.error is not None
    assert "block-relative lines 1..2, not absolute file line numbers" in res.error["error"]


def test_multiple_inserts_at_same_anchor_and_empty_ops() -> None:
    text = "first\nsecond\nthird\n"
    # Empty ops return text unchanged without error
    res_empty = apply_ops(text, [], literal)
    assert res_empty.error is None
    assert res_empty.text == text

    # Multiple inserts at line 2 in FIFO order
    ops = [
        {"insert_after": 2, "content": "insA"},
        {"insert_after": 2, "content": "insB"},
        {"insert_after": 2, "content": "insC"},
    ]
    res_multi = apply_ops(text, ops, literal)
    assert res_multi.error is None
    assert res_multi.text == "first\nsecond\ninsA\ninsB\ninsC\nthird\n"

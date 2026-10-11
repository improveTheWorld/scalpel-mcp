"""The line-number rebaser behind a grouped edit_block.

Every case here is one of the seven shapes measured in benchmarks/.work/CAS_ONE_SHOT.md
plus the two the brief calls out: a multi-operation entry, and a third party writing
between two entries. The one case that must still FAIL is the real conflict -- two
entries aiming at the SAME line.
"""

import pytest

from ast_graph_server.linetranslate import (
    LineTranslation,
    LineTranslationError,
    OpTranslation,
)


def block() -> str:
    """The four lines case A/B/C/F/G/H were all measured on."""
    return "### 8. Alpha\n- a\n- b\n- c"


def operation_anchor(op):
    """The position an operation is resolved against, in the version the caller read."""
    if "delete" in op:
        return int(op["delete"][0])
    if "replace" in op:
        return int(op["replace"][0])
    return int(op["insert_after"]) + 1


def apply_literal(lines, ops):
    """Apply ops the way lineops.apply_ops does: against ONE version, bottom-up.

    That order is the module's premise (lineops.py:13-15): the operations of one entry
    do not shift each other, so a helper that applied them top-down would silently test
    a different contract.
    """
    out = list(lines)
    for op in sorted(ops, key=operation_anchor, reverse=True):
        if "replace" in op:
            first, last = op["replace"]
            out[first - 1 : last] = str(op.get("content", "")).splitlines()
        elif "delete" in op:
            first, last = op["delete"]
            del out[first - 1 : last]
        else:
            anchor = op["insert_after"]
            out[anchor:anchor] = str(op.get("content", "")).splitlines()
    return out


def translate_after(before, after, first_ops, second_ops):
    """Entry 0 writes, the state is observed, then entry 1 is translated."""
    engine = LineTranslation(before, before)
    first = engine.translate(first_ops)
    engine.observe_lines(before.splitlines(), after.splitlines())
    return engine, first, engine.translate(second_ops)


# ------------------------------------------------------------------ the seven measured cases


def test_case_a_an_insert_above_the_aimed_line_rebases_it() -> None:
    """Case A: entry 0 inserts above line 2, entry 1 rewrites the line it READ as 2.

    The caller read line 2 = '- a'. Entry 0 pushes it to the third line, so the write
    must land there. Refusing this is the tool refusing a move it made itself.
    """
    before = block()
    after = "### 8. Alpha\n- X\n- a\n- b\n- c"
    engine, _first, second = translate_after(
        before, after, [{"insert_after": 1, "content": "- X"}], [{"replace": [2, 2], "content": "- A"}]
    )
    assert second.touched == {2}
    assert second.ops == [{"replace": [3, 3], "content": "- A"}]
    # Entry 0 moved that identity: the reply has to say so (brief 4.3).
    assert second.rebased == [0]
    assert second.by_entry == {0: [2]}
    assert engine.writer_of(2) is None  # moved, not written

    assert apply_literal(before.splitlines(), [{"insert_after": 1, "content": "- X"}]) == after.splitlines()
    assert apply_literal(after.splitlines(), second.ops) == ["### 8. Alpha", "- X", "- A", "- b", "- c"]


def test_case_b_an_insert_below_the_aimed_line_does_not_move_it() -> None:
    """Case B: the write lands BELOW the aimed line, so no number is rebased.

    Reporting a rebase here would be as wrong as refusing the call: the line did not
    move, and the caller must be able to tell the two apart.
    """
    before = block()
    after = "### 8. Alpha\n- a\n- X\n- b\n- c"
    _engine, _first, second = translate_after(
        before, after, [{"insert_after": 2, "content": "- X"}], [{"replace": [2, 2], "content": "- A"}]
    )
    assert second.touched == {2}
    assert second.ops == [{"replace": [2, 2], "content": "- A"}]
    assert second.rebased == []
    assert second.by_entry == {}


def test_case_h_a_duplicated_line_is_followed_by_identity_not_by_text() -> None:
    """Case H: the aimed line is a COPY, and entry 0 inserts one more copy above it.

    Block: '### 8. Block / - dup / - dup / - tail'. Entry 0 inserts a third '- dup'
    after line 2; entry 1 aims at the line it READ as 3, the SECOND '- dup'. Comparing
    text sends the write to line 3 (the copy just inserted); identity sends it to line 4.
    This is the measurement that ruled text comparison out.
    """
    before = "### 8. Block\n- dup\n- dup\n- tail"
    after = "### 8. Block\n- dup\n- dup\n- dup\n- tail"
    engine, _first, second = translate_after(
        before, after, [{"insert_after": 2, "content": "- dup"}], [{"replace": [3, 3], "content": "- REPLACED"}]
    )
    assert second.touched == {3}
    assert second.ops == [{"replace": [4, 4], "content": "- REPLACED"}]
    assert second.rebased == [0]
    assert engine.writer_of(3) is None

    written = apply_literal(after.splitlines(), second.ops)
    assert written == ["### 8. Block", "- dup", "- dup", "- REPLACED", "- tail"]
    # The text-based answer would have produced this instead -- the wrong copy.
    assert written != ["### 8. Block", "- dup", "- REPLACED", "- dup", "- tail"]


def test_case_f_a_rewritten_sibling_does_not_move_the_aimed_line() -> None:
    """Case F: entry 0 rewrites its OWN line, entry 1's line has not moved at all."""
    before = block()
    after = "### 8. Alpha\n- a\n- b\n- cc"
    engine, _first, second = translate_after(
        before, after, [{"replace": [4, 4], "content": "- cc"}], [{"replace": [2, 2], "content": "- A"}]
    )
    assert second.ops == [{"replace": [2, 2], "content": "- A"}]
    assert second.rebased == []
    # The sibling was written, but not by the entry doing the asking.
    assert engine.writer_of(4) == 0
    assert engine.writer_of(2) is None


def test_case_g_a_deleted_sibling_does_not_move_the_aimed_line() -> None:
    """Case G: the delete is BELOW the aimed line, so line 2 keeps its number."""
    before = block()
    after = "### 8. Alpha\n- a\n- b"
    engine, _first, second = translate_after(
        before, after, [{"delete": [4, 4]}], [{"replace": [2, 2], "content": "- A"}]
    )
    assert second.ops == [{"replace": [2, 2], "content": "- A"}]
    assert second.rebased == []
    assert engine.writer_of(4) == 0


def test_case_c_an_insert_two_lines_lower_leaves_the_aimed_line_alone() -> None:
    """Case C: applied=2, refused=0 even today -- the translation must not disturb it."""
    before = block()
    after = "### 8. Alpha\n- a\n- b\n- c\n- X"
    _engine, _first, second = translate_after(
        before, after, [{"insert_after": 4, "content": "- X"}], [{"replace": [2, 2], "content": "- A"}]
    )
    assert second.ops == [{"replace": [2, 2], "content": "- A"}]
    assert second.rebased == []


def test_case_v3_the_same_line_twice_is_the_one_real_conflict() -> None:
    """Case V3 stays refused: two entries write the SAME line, nobody can arbitrate."""
    before = block()
    after = "### 8. Alpha\n- PREMIER\n- b\n- c"
    engine = LineTranslation(before, before)
    engine.translate([{"replace": [2, 2], "content": "- PREMIER"}])
    engine.observe_lines(before.splitlines(), after.splitlines())
    with pytest.raises(LineTranslationError) as raised:
        engine.translate([{"replace": [2, 2], "content": "- SECOND"}])
    message = str(raised.value)
    assert "line 2" in message
    assert "entry 0" in message
    assert engine.writer_of(2) == 0


def test_a_line_deleted_by_an_earlier_entry_is_refused_by_name() -> None:
    """Cause 1: a line that no longer exists must not be silently retargeted."""
    before = block()
    after = "### 8. Alpha\n- a\n- b"
    engine = LineTranslation(before, before)
    engine.translate([{"delete": [4, 4]}])
    engine.observe_lines(before.splitlines(), after.splitlines())
    with pytest.raises(LineTranslationError) as raised:
        engine.translate([{"replace": [4, 4], "content": "- c"}])
    message = str(raised.value)
    assert "line 4" in message
    assert "entry 0" in message


# ------------------------------------------------------------------ the brief's own cases


def test_two_translations_of_one_unchanged_state_agree() -> None:
    """No write between them: the span must be identical, not drift."""
    engine = LineTranslation(block(), block())
    first = engine.translate([{"replace": [3, 3], "content": "- B"}])
    second = engine.translate([{"replace": [3, 3], "content": "- B"}])
    assert first.ops == second.ops
    assert first.touched == second.touched == {3}
    assert second.rebased == []
    assert engine.shifts_of(3) == []


def test_insert_after_zero_addresses_no_line_and_never_fails() -> None:
    """The head boundary has no identity: touch nothing, translate to 0, always safe."""
    engine = LineTranslation(block(), block())
    result = engine.translate([{"insert_after": 0, "content": "# Preamble"}])
    assert result.touched == set()
    assert result.rebased == []
    assert result.by_entry == {}
    assert result.ops == [{"insert_after": 0, "content": "# Preamble"}]
    # And when a previous entry has already pushed the block down, the head is still head.
    engine.observe_lines(block().splitlines(), ("- X\n" + block()).splitlines())
    again = engine.translate([{"insert_after": 0, "content": "# Preamble"}])
    assert again.ops == [{"insert_after": 0, "content": "# Preamble"}]
    assert again.touched == set()


def test_one_entry_with_several_ops_translates_against_one_state_only() -> None:
    """lineops.py:13-15: the ops of ONE entry are resolved against a single version.

    They do not shift each other, so the translation must not shift them either: both
    spans in the result are the numbers the caller would have sent a moment earlier.
    """
    before = "a\nb\nc\nd\ne"
    engine = LineTranslation(before, before)
    result = engine.translate(
        [
            {"replace": [2, 2], "content": "B1\nB2"},
            {"insert_after": 3, "content": "NEW"},
        ]
    )
    assert result.ops == [
        {"replace": [2, 2], "content": "B1\nB2"},
        {"insert_after": 3, "content": "NEW"},
    ]
    assert result.touched == {2, 3}
    assert result.rebased == []

    after = apply_literal(before.splitlines(), result.ops)
    # Bottom-up: insert after line 3 ("c") first, then replace line 2. "b" is consumed.
    assert after == ["a", "B1", "B2", "c", "NEW", "d", "e"]
    engine.observe_lines(before.splitlines(), after)
    # One observation, one shift record per moved line, and the entry's own lines written.
    assert engine.writer_of(2) == 0
    assert engine.shifts_of(2) == []


def test_one_entry_that_produces_several_lines_records_its_whole_extent() -> None:
    """note_written records the entry ONCE, over the extent it produced."""
    before = "a\nb\nc"
    engine = LineTranslation(before, before)
    result = engine.translate([{"replace": [2, 2], "content": "B1\nB2\nB3"}])
    assert result.ops == [{"replace": [2, 2], "content": "B1\nB2\nB3"}]
    after = apply_literal(before.splitlines(), result.ops)
    assert after == ["a", "B1", "B2", "B3", "c"]
    engine.note_written(0, 1, len(before.splitlines()), len(after))
    assert engine.writer_of(2) == 0
    # The line that WAS 3 is still the caller's line 3, two positions lower: it moved.
    assert engine.shifts_of(2) == []
    assert engine.shifts_of(3) == [0]
    # The line that used to be 3 is still the caller's line 3, now one slot lower.
    later = engine.translate([{"replace": [3, 3], "content": "C"}])
    assert later.ops == [{"replace": [5, 5], "content": "C"}]
    assert later.rebased == [0]
    assert later.by_entry == {0: [3]}


def test_a_third_party_that_changed_the_text_is_refused_by_name() -> None:
    """Cause 2, the only one a lock cannot prevent: an outside writer."""
    before = block()
    outside = "### 8. Alpha\n- a\n- CHANGED\n- c"
    engine = LineTranslation(before, before)
    engine.observe_lines(before.splitlines(), outside.splitlines())
    with pytest.raises(LineTranslationError) as raised:
        engine.translate([{"replace": [3, 3], "content": "- B"}])
    message = str(raised.value)
    assert "line 3" in message
    assert "text changed" in message
    # No entry of this call wrote on that line, so no entry is named -- not entry -1, not
    # entry 0. An outside writer is never given a fictitious entry index.
    assert "entry" not in message
    assert engine.writer_of(3) is None


def test_a_third_party_that_inserted_above_only_moves_the_line() -> None:
    """An outside insert is not a conflict: the aimed line's text is intact."""
    before = block()
    outside = "### 8. Alpha\n# inserted\n- a\n- b\n- c"
    engine = LineTranslation(before, before)
    engine.observe_lines(before.splitlines(), outside.splitlines())
    result = engine.translate([{"replace": [3, 3], "content": "- B"}])
    assert result.ops == [{"replace": [4, 4], "content": "- B"}]
    # The third party is NOT an entry of this call: it moves the line, it is never named
    # as the entry that rebased it, and shifts_of -- which answers in ENTRY indices --
    # stays empty. The outside writer has its own answer.
    assert result.rebased == []
    assert result.by_entry == {}
    assert engine.shifts_of(3) == []
    # Every identity BELOW the outside insert moved, not just the one being aimed at.
    assert engine.moved_outside() == [2, 3, 4]


def test_a_third_party_that_deleted_the_aimed_line_is_refused_by_name() -> None:
    """The line the caller read is gone, and no entry of this call deleted it."""
    before = block()
    outside = "### 8. Alpha\n- a\n- c"
    engine = LineTranslation(before, before)
    engine.observe_lines(before.splitlines(), outside.splitlines())
    with pytest.raises(LineTranslationError) as raised:
        engine.translate([{"delete": [3, 3]}])
    assert "line 3" in str(raised.value)


# ------------------------------------------------------------------ the module's own rules


def test_observe_lines_is_idempotent_on_an_unchanged_state() -> None:
    engine = LineTranslation(block(), block())
    before = block().splitlines()
    engine.observe_lines(before, before)
    engine.observe_lines(before, before)
    assert engine.translate([{"replace": [2, 2], "content": "- A"}]).ops == [
        {"replace": [2, 2], "content": "- A"}
    ]


def test_deletes_and_inserts_both_rebase_by_identity() -> None:
    """A delete above shifts the aimed line up; an insert above shifts it down."""
    before = block()
    engine = LineTranslation(before, before)
    engine.translate([{"delete": [1, 1]}])
    engine.observe_lines(before.splitlines(), "- a\n- b\n- c".splitlines())
    assert engine.translate([{"replace": [3, 3], "content": "- B"}]).ops == [
        {"replace": [2, 2], "content": "- B"}
    ]

    engine = LineTranslation(before, before)
    engine.translate([{"insert_after": 1, "content": "- X\n- Y"}])
    engine.observe_lines(before.splitlines(), "### 8. Alpha\n- X\n- Y\n- a\n- b\n- c".splitlines())
    assert engine.translate([{"replace": [3, 3], "content": "- B"}]).ops == [
        {"replace": [5, 5], "content": "- B"}
    ]


def test_a_malformed_operation_is_a_translation_failure() -> None:
    """The module does not know how to translate it, and says exactly that."""
    engine = LineTranslation(block(), block())
    for bad in [
        "not an object",
        {"frobnicate": 1},
        {"replace": [1]},
        {"replace": ["x", "y"]},
        {"replace": [3, 1]},
        {"replace": [1, 9]},
        {"delete": [0, 1]},
        {"insert_after": -1},
        {"insert_after": 9},
        {"insert_after": "top"},
    ]:
        with pytest.raises(LineTranslationError):
            engine.translate([bad])


def test_out_of_range_in_the_current_state_is_still_a_refusal() -> None:
    """A span outside the version the caller READ cannot be translated at all."""
    engine = LineTranslation(block(), block())
    with pytest.raises(LineTranslationError):
        engine.translate([{"replace": [4, 5], "content": "x"}])


def test_writer_of_and_shifts_of_answer_in_the_callers_numbers() -> None:
    """The two questions are distinct: who WROTE this line, and who MOVED it."""
    before = "1\n2\n3\n4\n5"
    engine = LineTranslation(before, before)
    engine.translate([{"insert_after": 1, "content": "X"}])
    engine.observe_lines(before.splitlines(), "1\nX\n2\n3\n4\n5".splitlines())
    assert engine.shifts_of(1) == []       # above the insert: did not move
    assert engine.shifts_of(2) == [0]      # the entry that moved it, named as an entry
    assert engine.moved_outside() == []    # no outside writer in this call
    assert engine.writer_of(1) is None
    engine.translate([{"replace": [4, 4], "content": "FOUR"}])
    engine.observe_lines("1\nX\n2\n3\n4\n5".splitlines(), "1\nX\n2\n3\nFOUR\n5".splitlines())
    assert engine.writer_of(4) == 1
    assert engine.shifts_of(4) == [0]      # entry 0 inserted above it
    assert engine.shifts_of(5) == [0]      # same arithmetic, different line
    assert engine.moved_outside() == []


def test_the_string_observe_route_rebases_like_the_line_list_one() -> None:
    """observe(str) is the whole-file spelling of observe_lines: same correspondence."""
    before = "a\nb\nc"
    engine = LineTranslation(before, before)
    engine.translate([{"insert_after": 1, "content": "X"}])
    engine.observe("a\nX\nb\nc")
    assert engine.translate([{"replace": [2, 2], "content": "B"}]).ops == [
        {"replace": [3, 3], "content": "B"}
    ]
    assert engine.moved_outside() == []


def test_a_block_already_stale_when_the_call_opens_is_absorbed() -> None:
    """before and current may differ at construction: a writer got there first.

    The map must be built against before, not against the current text -- reading the aimed
    line at the wrong position is the failure this whole module exists to remove.
    """
    engine = LineTranslation("a\nb\nc", "a\nX\nb\nc")
    result = engine.translate([{"replace": [2, 2], "content": "B"}])
    assert result.ops == [{"replace": [3, 3], "content": "B"}]
    assert engine.moved_outside() == [2, 3]
    assert engine.writer_of(2) is None


def test_an_observation_that_contradicts_the_plan_is_nobody_s_write() -> None:
    """The plan describes what the tool meant to write; the file is what it is.

    When the two disagree, something else wrote. Naming the entry anyway would accuse it
    on a plan that no longer holds, so the move is reported as an outside one.
    """
    engine = LineTranslation("a\nb\nc", "a\nb\nc")
    engine.translate([{"insert_after": 1, "content": "X"}])   # the plan: one line added
    engine.observe_lines("a\nb\nc".splitlines(), "a\nQ\nR\nb\nc".splitlines())
    assert engine.moved_outside() == [2, 3]
    assert engine.shifts_of(2) == []
    later = engine.translate([{"replace": [3, 3], "content": "C"}])
    assert later.ops == [{"replace": [5, 5], "content": "C"}]


def test_the_recording_entry_points_never_raise_on_unreadable_arguments() -> None:
    """observe, observe_lines and note_written are documented to never raise."""
    engine = LineTranslation(block(), block())
    engine.observe(object())            # not a string
    engine.observe_lines(3, 4)          # not iterable
    engine.note_written("nope", 0, 0, 0)
    engine.note_written(0, "nope", 0, 0)
    # The engine is still usable and still knows the version the caller read.
    assert engine.translate([{"replace": [2, 2], "content": "- A"}]).ops == [
        {"replace": [2, 2], "content": "- A"}
    ]


def test_the_first_translate_does_not_rebase_anything() -> None:
    engine = LineTranslation(block(), block())
    result = engine.translate([{"replace": [1, 1], "content": "### 8. Alpha"}])
    assert isinstance(result, OpTranslation)
    assert result.ops == [{"replace": [1, 1], "content": "### 8. Alpha"}]
    assert result.rebased == []
    assert result.by_entry == {}


def test_insert_after_the_last_line_of_the_read_version_is_allowed() -> None:
    """n == the line count is the tail boundary: legal, and it moves nothing."""
    engine = LineTranslation(block(), block())
    result = engine.translate([{"insert_after": 4, "content": "- tail2"}])
    assert result.ops == [{"insert_after": 4, "content": "- tail2"}]
    assert result.touched == {4}
    assert result.rebased == []

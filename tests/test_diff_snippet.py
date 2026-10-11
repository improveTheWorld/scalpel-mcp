"""Tests for render_edit_snippet and edit confirmation formatting."""

import pytest
from ast_graph_server.lineops import render_edit_snippet


def test_snippet_empty_blocks():
    assert render_edit_snippet("", "") == "(empty block)"
    assert render_edit_snippet("   ", "   ") == "(no changes)"


def test_snippet_no_changes():
    text = "line 1\nline 2\nline 3"
    assert render_edit_snippet(text, text) == "(no changes)"


def test_snippet_single_line_replace_small_block():
    old = "line 1\nline 2 (old)\nline 3"
    new = "line 1\nline 2 (new)\nline 3"
    snippet = render_edit_snippet(old, new)
    lines = snippet.splitlines()
    assert len(lines) == 4
    assert lines[0] == "  1 | line 1"
    assert lines[1] == "-   | line 2 (old)"
    assert lines[2] == "+ 2 | line 2 (new)"
    assert lines[3] == "  3 | line 3"


def test_snippet_insert_at_top():
    old = "line 1\nline 2"
    new = "line 0\nline 1\nline 2"
    snippet = render_edit_snippet(old, new)
    lines = snippet.splitlines()
    assert lines[0] == "+ 1 | line 0"
    assert lines[1] == "  2 | line 1"
    assert lines[2] == "  3 | line 2"


def test_snippet_insert_at_bottom():
    old = "line 1\nline 2"
    new = "line 1\nline 2\nline 3"
    snippet = render_edit_snippet(old, new)
    lines = snippet.splitlines()
    assert lines[0] == "  1 | line 1"
    assert lines[1] == "  2 | line 2"
    assert lines[2] == "+ 3 | line 3"


def test_snippet_delete_middle():
    old = "line 1\nline 2\nline 3"
    new = "line 1\nline 3"
    snippet = render_edit_snippet(old, new)
    lines = snippet.splitlines()
    assert lines[0] == "  1 | line 1"
    assert lines[1] == "-   | line 2"
    assert lines[2] == "  2 | line 3"


def test_snippet_delete_all():
    old = "line 1\nline 2"
    new = ""
    snippet = render_edit_snippet(old, new)
    lines = snippet.splitlines()
    assert lines[0] == "-   | line 1"
    assert lines[1] == "-   | line 2"


def test_snippet_from_empty():
    old = ""
    new = "line 1\nline 2"
    snippet = render_edit_snippet(old, new)
    lines = snippet.splitlines()
    assert lines[0] == "+ 1 | line 1"
    assert lines[1] == "+ 2 | line 2"


def test_snippet_crlf_handling():
    old = "line 1\r\nline 2 (old)\r\nline 3\r\n"
    new = "line 1\r\nline 2 (new)\r\nline 3\r\n"
    snippet = render_edit_snippet(old, new)
    assert "\r" not in snippet
    assert "-   | line 2 (old)" in snippet
    assert "+ 2 | line 2 (new)" in snippet


def test_snippet_large_block_truncation_and_hunks():
    old = "\n".join(f"code_{i}" for i in range(1, 101))
    lines = [f"code_{i}" for i in range(1, 101)]
    # Modify line 10 and line 90
    lines[9] = "code_10_MOD"
    lines[89] = "code_90_MOD"
    new = "\n".join(lines)
    
    snippet = render_edit_snippet(old, new, context=2)
    # Check that preceding, intermediate, and following lines are omitted
    assert "preceding lines omitted" in snippet
    assert "lines omitted" in snippet
    assert "following lines omitted" in snippet
    # Check that the modified lines and their context are present
    assert "+  10 | code_10_MOD" in snippet
    assert "+  90 | code_90_MOD" in snippet
    assert "    8 | code_8" in snippet
    assert "   12 | code_12" in snippet
    assert "   88 | code_88" in snippet
    assert "   92 | code_92" in snippet
    # Lines far away should NOT be present
    assert "code_50" not in snippet


def test_snippet_expansion_and_contraction():
    # 1 line replaced by 3 lines
    old = "A\nB\nC"
    new = "A\nB1\nB2\nB3\nC"
    snippet = render_edit_snippet(old, new)
    assert "-   | B" in snippet
    assert "+ 2 | B1" in snippet
    assert "+ 3 | B2" in snippet
    assert "+ 4 | B3" in snippet
    assert "  5 | C" in snippet

    # 3 lines replaced by 1 line
    old = "A\nB1\nB2\nB3\nC"
    new = "A\nB\nC"
    snippet2 = render_edit_snippet(old, new)
    assert "-   | B1" in snippet2
    assert "-   | B2" in snippet2
    assert "-   | B3" in snippet2
    assert "+ 2 | B" in snippet2
    assert "  3 | C" in snippet2


def test_snippet_large_block_deletion_at_start():
    old = "\n".join(f"line_{i}" for i in range(1, 60))
    # delete first 3 lines
    new = "\n".join(f"line_{i}" for i in range(4, 60))
    snippet = render_edit_snippet(old, new, context=2)
    lines = snippet.splitlines()
    assert lines[0].startswith("- ") and "line_1" in lines[0]
    assert lines[1].startswith("- ") and "line_2" in lines[1]
    assert lines[2].startswith("- ") and "line_3" in lines[2]
    assert "  1 | line_4" in lines[3]
    assert "  2 | line_5" in lines[4]
    assert "following lines omitted" in snippet


def test_snippet_large_block_deletion_at_end():
    old = "\n".join(f"line_{i}" for i in range(1, 60))
    # delete last 3 lines
    new = "\n".join(f"line_{i}" for i in range(1, 57))
    snippet = render_edit_snippet(old, new, context=2)
    assert "preceding lines omitted" in snippet
    assert " 55 | line_55" in snippet
    assert " 56 | line_56" in snippet
    assert "line_57" in snippet and "line_58" in snippet and "line_59" in snippet
    assert "following lines omitted" not in snippet


def test_snippet_large_block_adjacent_modifications_merged():
    import re
    old = "\n".join(f"line_{i}" for i in range(1, 60))
    lines = [f"line_{i}" for i in range(1, 60)]
    # Modify line 30 and line 32 (separated by only 1 unchanged line)
    lines[29] = "line_30_MOD"
    lines[31] = "line_32_MOD"
    new = "\n".join(lines)
    snippet = render_edit_snippet(old, new, context=2)
    # The two edits should be in the SAME hunk, not broken by omitted lines
    assert "line_30_MOD" in snippet and "line_32_MOD" in snippet
    assert not re.search(r"\(\d+ lines omitted\)", snippet)  # only preceding and following



def test_snippet_unicode_and_emojis():
    old = "## 1. Introduction\n- Bullet 1 🛡️\n- Bullet 2 🚀\n"
    new = "## 1. Introduction\n- Bullet 1 🛡️ (mis à jour)\n- Bullet 2 🚀\n"
    snippet = render_edit_snippet(old, new)
    assert "- Bullet 1 🛡️ (mis à jour)" in snippet
    assert "- Bullet 2 🚀" in snippet


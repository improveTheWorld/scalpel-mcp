"""Tests for Lot 4: Graceful line-boundary truncation and truncation metadata in build_context."""
import re
from unittest.mock import MagicMock
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.context_builder import ContextBuilder


def _make_builder() -> ContextBuilder:
    return ContextBuilder(MagicMock(spec=ServerConfig), MagicMock(), backend=MagicMock())


def test_stamped_entry_cuts_cleanly_at_line_boundary() -> None:
    builder = _make_builder()
    lines = [f"{i} | line number {i:03d} with padding to reach a good length" for i in range(1, 60)]
    numbered_text = "\n".join(lines)
    assert len(numbered_text) > 2000

    metrics = {
        "numbered_text": numbered_text,
        "content_hash": "dummy_hash_123",
        "line_count": len(lines),
    }

    result = builder._stamped_entry(
        address="src/service.py::Service::method",
        block_ref="sym_1",
        max_chars=2000,
        metrics=metrics,
    )
    assert result is not None
    rendered_text, extra = result
    assert extra == {}

    # Check address is at the top
    assert rendered_text.startswith("src/service.py::Service::method\n")
    body = rendered_text[len("src/service.py::Service::method\n"):]

    # Must contain omission notice
    assert "\n[... " in body
    code_part, _, notice = body.partition("\n[... ")

    # The code part must contain only whole lines from the original text
    shown_lines = code_part.splitlines()
    assert len(shown_lines) > 0
    assert len(shown_lines) < len(lines)

    # Every shown line must match exactly the corresponding line in lines (no partial/split lines)
    for idx, shown_line in enumerate(shown_lines):
        assert shown_line == lines[idx]
        assert re.match(r"^\d+ \| ", shown_line)


def test_truncation_notice_reports_shown_lines_vs_total_lines() -> None:
    builder = _make_builder()
    lines = [f"{i} | line content {i:03d}" for i in range(1, 80)]
    numbered_text = "\n".join(lines)
    total_lines = len(lines)

    metrics = {
        "numbered_text": numbered_text,
        "content_hash": "hash_xyz",
        "line_count": total_lines,
    }

    # Test with explicit line_count in metrics
    result = builder._stamped_entry(
        address="docs/guide.md::Intro",
        block_ref="doc_sec_1",
        max_chars=500,
        metrics=metrics,
    )
    assert result is not None
    text, _ = result

    # Check the notice format
    # Expected pattern: [... {omitted} chars omitted ({shown_lines}/{total_lines} lines shown): the text is NOT complete, so no hash is given; call read_block for complete block and hash ...]
    match = re.search(r"\[\.\.\. (\d+) chars omitted \((\d+)/(\d+) lines shown\): the text is NOT complete, so no hash is given; call read_block for complete block and hash \.\.\.\]", text)
    assert match is not None, f"Notice does not match expected format in:\n{text}"
    omitted_chars = int(match.group(1))
    shown_count = int(match.group(2))
    total_count = int(match.group(3))

    assert total_count == total_lines
    assert shown_count > 0
    assert shown_count < total_lines
    assert omitted_chars > 0

    # Test fallback when line_count is missing from metrics
    metrics_no_lc = {
        "numbered_text": numbered_text,
        "content_hash": "hash_xyz",
    }
    result_no_lc = builder._stamped_entry(
        address="docs/guide.md::Intro",
        block_ref="doc_sec_1",
        max_chars=500,
        metrics=metrics_no_lc,
    )
    assert result_no_lc is not None
    text_no_lc, _ = result_no_lc
    match_no_lc = re.search(r"\((\d+)/(\d+) lines shown\)", text_no_lc)
    assert match_no_lc is not None
    assert int(match_no_lc.group(2)) == total_lines


def test_bounded_content_cuts_cleanly_at_line_boundary() -> None:
    builder = _make_builder()
    raw_lines = [f"line {i:03d}: some raw text without numbers that goes on and on" for i in range(1, 100)]
    raw_text = "\n".join(raw_lines)
    assert len(raw_text) > 2000

    bounded = builder._bounded_content(raw_text, max_chars=1000)
    assert "\n[... " in bounded
    content_part, _, notice = bounded.partition("\n[... ")

    shown_lines = content_part.splitlines()
    assert len(shown_lines) > 0
    assert len(shown_lines) < len(raw_lines)

    # Every shown line must be identical to raw_lines[idx], never cut mid-line
    for idx, shown_line in enumerate(shown_lines):
        assert shown_line == raw_lines[idx]

    # Check notice reports shown and total lines
    match = re.search(r"\((\d+)/(\d+) lines shown\)", notice)
    assert match is not None
    assert int(match.group(1)) == len(shown_lines)
    assert int(match.group(2)) == len(raw_lines)


def test_truncation_giant_line_without_newline_falls_back_to_cap() -> None:
    builder = _make_builder()
    giant_line = "A" * 2500
    bounded = builder._bounded_content(giant_line, max_chars=1000)
    assert "[... 1500 chars omitted" in bounded
    content_part, _, _ = bounded.partition("\n[... ")
    assert len(content_part) == 1000

    metrics = {
        "numbered_text": giant_line,
        "content_hash": "hash_giant",
    }
    result = builder._stamped_entry("address", "ref", max_chars=1000, metrics=metrics)
    assert result is not None
    text, _ = result
    assert "1500 chars omitted" in text
"""Line-addressed operations inside an addressable block.

read_block shows a block with 1-based line numbers. edit_block takes operations on
those numbers, so the caller never resends the text it wants to replace -- only
the new content. The block's content hash pins the version the numbers refer to:
identical numbers against a different version are refused, never applied silently.

Operations (1-based, inclusive):
  {"replace": [first, last], "content": "..."}   replace lines first..last
  {"delete": [first, last]}                       remove lines first..last
  {"insert_after": n, "content": "..."}           insert after line n (0 = top)

All operations are resolved against the version that was read and applied from
the bottom up, so one operation never shifts the line numbers of another.
"""

import difflib
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class OpResult:
    text: str
    error: Optional[Dict[str, Any]] = None


def line_style(text: str) -> str:
    return "\r\n" if "\r\n" in text else ("\r" if "\r" in text else "\n")


def indentation_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _nearest_indent(lines: List[str], index: int, step: int) -> Optional[str]:
    """Indentation of the nearest NON-BLANK line walking from index by step.

    A blank line has no indentation, so using it as the reference put inserted
    members at column 0. Observed on a real run: the agent inserted after line 17,
    line 18 was blank, the whole block landed at column 0, its fix was re-based on
    the same broken line and answered noop, and it fell back to rewriting the whole
    block -- losing the entire point of line addressing.
    """
    position = index
    while 0 <= position < len(lines):
        if lines[position].strip():
            return indentation_of(lines[position])
        position += step
    return None


def numbered(text: str, start: int = 1) -> str:
    """The block as the caller sees it: every line prefixed with its 1-based number."""
    lines = text.splitlines()
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    return "\n".join(
        str(start + index).rjust(width) + " | " + line for index, line in enumerate(lines)
    )


def _rebased_lines(content: str, column: str, rebase: Callable[[str, str], str]) -> List[str]:
    if not content.strip():
        return []
    return rebase(content, column).splitlines()


def apply_ops(
    text: str,
    ops: List[Dict[str, Any]],
    rebase: Callable[[str, str], str],
) -> OpResult:
    """Apply line operations, all or nothing."""
    newline = line_style(text)
    trailing = text.endswith("\n") or text.endswith("\r")
    lines = text.splitlines()
    total = len(lines)

    actions: List[Tuple[int, int, List[str], int]] = []
    insert_points: Dict[int, int] = {}

    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            return OpResult(text, {"status": "error", "error": "op " + str(index) + ": expected an object."})
        try:
            if "replace" in op:
                first, last = int(op["replace"][0]), int(op["replace"][1])
                if first < 1 or last < first or last > total:
                    return OpResult(text, {"status": "error", "error":
                        "op " + str(index) + ": replace range must be inside 1.." + str(total) + " (block-relative lines 1.." + str(total) + ", not absolute file line numbers)."})
                column = (
                    _nearest_indent(lines, first - 1, 1)
                    or _nearest_indent(lines, first - 2, -1)
                    or ""
                )
                actions.append((first - 1, last, _rebased_lines(str(op.get("content", "")), column, rebase), index))
            elif "delete" in op:
                first, last = int(op["delete"][0]), int(op["delete"][1])
                if first < 1 or last < first or last > total:
                    return OpResult(text, {"status": "error", "error":
                        "op " + str(index) + ": delete range must be inside 1.." + str(total) + " (block-relative lines 1.." + str(total) + ", not absolute file line numbers)."})
                actions.append((first - 1, last, [], index))
            elif "insert_after" in op:
                position = int(op["insert_after"])
                if position < 0 or position > total:
                    return OpResult(text, {"status": "error", "error":
                        "op " + str(index) + ": insert_after must be inside 0.." + str(total) + " (block-relative lines 0.." + str(total) + ", not absolute file line numbers)."})
                if position in insert_points:
                    return OpResult(text, {"status": "error", "error":
                        "op " + str(index) + ": another insert already targets position " + str(position) + "."})
                insert_points[position] = index
                # The new lines take the place the *following* line occupies, so
                # they align with it. Blank lines are skipped: a blank line carries
                # no indentation, and using it put whole inserted blocks at column 0.
                column = (
                    _nearest_indent(lines, position, 1)
                    or _nearest_indent(lines, position - 1, -1)
                    or ""
                )
                actions.append((position, position, _rebased_lines(str(op.get("content", "")), column, rebase), index))
            else:
                return OpResult(text, {"status": "error", "error":
                    "op " + str(index) + ": expected replace, delete or insert_after."})
        except (TypeError, ValueError, IndexError):
            return OpResult(text, {"status": "error", "error": "op " + str(index) + ": malformed range or content."})

    ranges = [(start, end) for start, end, _repl, _i in actions if start != end]
    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            if ranges[i][0] < ranges[j][1] and ranges[j][0] < ranges[i][1]:
                return OpResult(text, {"status": "error", "error":
                    "operations overlap: " + str(ranges[i]) + " and " + str(ranges[j]) + "."})

    # `insert_after: n` and `replace: [n+1, n+1]` anchor at the SAME index, because the
    # new lines take the place the following line occupies. Sorting on that index alone
    # left their order to the caller's, and inserting first pushed the line to be
    # rewritten down: the next operation then overwrote the first inserted line while the
    # original line survived below it. Measured on mapab2 b r2 of the base f1fb645 -- the
    # agent sent `insert_after: 21` then `replace: [22, 22]`, got `status: success`, and
    # found a misordered list whose heading had been replaced by the renumbered line that
    # was meant to follow it. A range edit therefore applies before an insertion at the
    # same index, whichever order the caller listed them in.
    ordered = sorted(
        actions, key=lambda item: (item[0], 0 if item[0] == item[1] else 1), reverse=True
    )
    for start, end, replacement, _index in ordered:
        lines[start:end] = replacement

    result = newline.join(lines) + (newline if trailing and lines else "")
    return OpResult(result, None)


def render_edit_snippet(
    old_text: str,
    new_text: str,
    context: int = 2,
    max_full_lines: int = 20,
) -> str:
    """Render a compact, contextual diff snippet of an edit.

    Shows unchanged context lines (marked ' '), added lines (marked '+'),
    and deleted lines (marked '-'), with exact 1-based line numbers for the new text.
    Small blocks (<= max_full_lines) are shown in full with markers.
    Larger blocks group changes into hunks with omitted lines noted.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    if not old_lines and not new_lines:
        return "(empty block)"
    if old_text == new_text:
        return "(no changes)"

    width = len(str(max(1, len(new_lines))))
    space_pad = " " * width

    if not old_lines:
        return "\n".join(f"+ {i + 1:>{width}} | {l}" for i, l in enumerate(new_lines))
    if not new_lines:
        return "\n".join(f"- {space_pad} | {l}" for l in old_lines)

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    opcodes = matcher.get_opcodes()

    diff_opcodes = [op for op in opcodes if op[0] != "equal"]
    if not diff_opcodes:
        return "(no changes)"

    # If the new block is small, display the whole block with diff markers
    if len(new_lines) <= max_full_lines:
        hunks = [(0, len(new_lines))]
    else:
        raw_hunks: List[List[int]] = []
        for tag, i1, i2, j1, j2 in diff_opcodes:
            h_start = max(0, j1 - context)
            h_end = min(len(new_lines), j2 + context)
            raw_hunks.append([h_start, h_end])

        # Merge overlapping or touching hunks
        merged: List[List[int]] = []
        for h in raw_hunks:
            if not merged:
                merged.append(h)
            else:
                prev = merged[-1]
                if h[0] <= prev[1] + 1:
                    prev[1] = max(prev[1], h[1])
                else:
                    merged.append(h)
        hunks = [(h[0], h[1]) for h in merged]

    new_tags = {}
    for tag, i1, i2, j1, j2 in opcodes:
        t = "+" if tag in ("insert", "replace") else " "
        for j in range(j1, j2):
            new_tags[j] = t

    deleted_map: List[Tuple[int, str]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag in ("delete", "replace"):
            for i in range(i1, i2):
                deleted_map.append((j1, old_lines[i]))

    out: List[str] = []
    last_end = 0

    for h_start, h_end in hunks:
        if h_start > last_end:
            omitted = h_start - last_end
            if last_end == 0:
                out.append(f"  ... ({omitted} preceding lines omitted)")
            else:
                out.append(f"  ... ({omitted} lines omitted)")

        for j in range(h_start, h_end):
            for at_pos, del_line in deleted_map:
                if at_pos == j:
                    out.append(f"- {space_pad} | {del_line}")
            tag = new_tags.get(j, " ")
            out.append(f"{tag} {j + 1:>{width}} | {new_lines[j]}")

        if h_end == len(new_lines):
            for at_pos, del_line in deleted_map:
                if at_pos >= len(new_lines):
                    out.append(f"- {space_pad} | {del_line}")

        last_end = h_end

    if last_end < len(new_lines):
        omitted = len(new_lines) - last_end
        out.append(f"  ... ({omitted} following lines omitted)")

    return "\n".join(out)


def format_single_edit_result(res: Dict[str, Any]) -> str:
    """Format a single edit_block result into clean, actionable text."""
    block_id = str(res.get("block_id") or res.get("id") or res.get("section_id") or "block")
    chash = str(res.get("content_hash") or "")
    h_str = f" [h:{chash[:8]}]" if chash else ""
    lc = res.get("line_count")
    lc_str = f" ({lc} lines)" if lc is not None else ""

    header = f"[ok] {block_id}{h_str}{lc_str}"
    lines = [header]
    for w in res.get("warnings") or []:
        lines.append(f"  warning: {w}")
    snippet = res.get("diff_snippet")
    if snippet:
        lines.append(snippet)
    return "\n".join(lines)


def format_batch_edit_result(res: Dict[str, Any]) -> str:
    """Format an edit_block batch result into clean, actionable text."""
    applied = res.get("applied", 0)
    refused = res.get("refused", 0)
    results = res.get("results") or []
    total = len(results)
    status_str = f"[ok] applied {applied}/{total} edits"
    if refused:
        status_str += f" ({refused} refused)"
    status_str += ":"

    lines = [status_str]
    for w in res.get("warnings") or []:
        lines.append(f"warning: {w}")
    lines.append("")

    for i, r in enumerate(results):
        if isinstance(r, dict) and r.get("status") == "success":
            lines.append(format_single_edit_result(r))
        elif isinstance(r, dict):
            idx = r.get("entry_index")
            if idx is None:
                idx = i
            bid = str(r.get("id") or r.get("block_id") or r.get("section_id") or "entry")
            err = str(r.get("error") or "refused")
            if bid == "entry" or bid == f"entry {idx}":
                lines.append(f"[refused] entry {idx}: {err}")
            else:
                lines.append(f"[refused] entry {idx} ({bid}): {err}")
            hint = r.get("actionable_hint")
            if hint:
                lines.append(f"  hint: {hint}")
        lines.append("")
    return "\n".join(lines).strip()



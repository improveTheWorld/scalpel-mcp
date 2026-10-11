"""Indentation-insensitive search/replace edits.

An agent that holds an identifier must not have to resend a whole block to change
one line. Measured on 11 recent runs: 173 edits, 330 958 characters sent, ~1 913
characters per edit -- and on the repeated SparkConnectOptions edits, 99.3% of the
payload was identical to the previous version. A fragment edit sends only the
changed text.

Matching deliberately ignores indentation and intra-line spacing: the agent may
quote the code at column 0 whatever the file's nesting, because the tool owns
indentation. The replacement is re-based to the column where the match actually
starts (symbols) or kept literal (Markdown, whose indentation is content).

The contract is strict where it matters: the quoted text must match exactly once,
otherwise nothing is written. A silent wrong target is worse than an error.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


def _normalize(text: str) -> Tuple[str, List[int]]:
    """Collapse every whitespace run to a single space, keeping original offsets."""
    out: List[str] = []
    offsets: List[int] = []
    pending = False
    for index, char in enumerate(text):
        if char in " \t\r\n":
            if not pending and out:
                out.append(" ")
                offsets.append(index)
            pending = True
        else:
            out.append(char)
            offsets.append(index)
            pending = False
    return "".join(out), offsets


@dataclass
class EditResult:
    text: str
    error: Optional[Dict[str, Any]] = None


def apply_edits(
    text: str,
    edits: List[Dict[str, Any]],
    rebase: Callable[[str, int], str],
) -> EditResult:
    """Apply edits (objects with 'old', 'new' and optional 'replace_all'), all or nothing."""
    current = text
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return EditResult(current, {
                "status": "error",
                "error": "edit " + str(index) + ": expected an object with 'old' and 'new'.",
            })
        old = str(edit.get("old", ""))
        new = str(edit.get("new", ""))
        if not old.strip():
            return EditResult(current, {
                "status": "error",
                "error": "edit " + str(index) + ": 'old' is empty; quote the text you want to replace.",
            })

        haystack, offsets = _normalize(current)
        needle = _normalize(old)[0].strip()
        if not needle:
            return EditResult(current, {
                "status": "error",
                "error": "edit " + str(index) + ": 'old' contains no matchable text.",
            })

        positions: List[int] = []
        found = haystack.find(needle)
        while found != -1:
            positions.append(found)
            found = haystack.find(needle, found + 1)

        if not positions:
            return EditResult(current, {
                "status": "error",
                "error": (
                    "edit " + str(index) + ": the quoted text was not found in this block "
                    "(indentation and spacing are ignored). Re-read the block, then quote it again."
                ),
                "quoted": old[:200],
            })
        if len(positions) > 1 and not edit.get("replace_all"):
            return EditResult(current, {
                "status": "error",
                "error": (
                    "edit " + str(index) + ": the quoted text appears " + str(len(positions))
                    + " times; quote more context, or pass replace_all."
                ),
                "quoted": old[:200],
            })

        for position in reversed(positions):
            begin = offsets[position]
            end = offsets[position + len(needle) - 1] + 1
            line_start = current.rfind("\n", 0, begin) + 1
            prefix = current[line_start:begin]
            if prefix.strip() == "":
                # The fragment begins its line: swallow the existing indentation
                # and re-base the replacement to that column. Otherwise the
                # original indent would remain AND the re-based text would add a
                # second one (16 columns instead of 8 -- observed).
                target_column = len(prefix)
                begin = line_start
                replacement = rebase(new, target_column)
            else:
                # Inline fragment: inserted literally, mid-line.
                replacement = new
            current = current[:begin] + replacement + current[end:]

    return EditResult(current, None)

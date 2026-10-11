"""Opt-in tracing: one JSON line per event, and NOTHING at all when it is off.

The campaign runs with `SCALPEL_TOOL_TRACE=<file>`. A run whose tool failed internally is not
counted, and its traces are then the only witness left -- so the points that can fail are marked,
and every line carries enough to reconstruct what the call was doing when it died.

Design rules, both non-negotiable:
  * OFF costs one boolean test per marked point. The variable is read ONCE, at import.
  * No trace ever changes behaviour: a failure to write is swallowed, and no exception escapes.
"""

import json
import os
import threading
import time
from typing import Any

#: Read once. The harness (or the operator) sets it in the environment before Python starts.
PATH = os.environ.get("SCALPEL_TOOL_TRACE") or ""
_PID = os.getpid()


def enabled() -> bool:
    return bool(PATH)


def note(event: str, **fields: Any) -> None:
    """Append one event. Called from the hot paths, so it stays a dict and a write."""
    if not PATH:
        return
    line: dict = {
        "t": round(time.time(), 4),
        "pid": _PID,
        "thread": threading.get_ident(),
        "event": event,
    }
    line.update(fields)
    try:
        with open(PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, default=str)[:4000] + chr(10))
    except Exception:
        pass
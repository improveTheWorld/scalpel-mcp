"""Compact text renderings of the read tools' results.

Measured on a real run (journal ab_manual2/scalpel__r2): 46% of everything the
agent read was JSON scaffolding. The envelopes repeat derivable fields -- a
document path appears in both id and document_id for every section, the file
path is repeated in every symbol match -- and nothing of that helps the model.

These renderers keep the identifiers the reading tools actually accept, drop the
scaffolding, and emit one line per item. They are the default emission; the
format argument still offers the structured JSON for programmatic callers.
"""

from typing import Any, Dict, List


def render_outline(document_id: str, sections: List[Dict[str, Any]]) -> str:
    """Table of contents: one line per section, as file#N :: heading."""
    lines = [document_id + " (" + str(len(sections)) + " sections)"]
    for section in sections:
        identifier = section.get("id") or (document_id + "#" + str(section.get("order_index", "")))
        heading = str(section.get("heading") or "").strip()
        lines.append(str(identifier) + " :: " + heading if heading else str(identifier))
    return "\n".join(lines)


def format_callers(callers: List[str]) -> str:
    """Format callers with file-level grouping to reduce verbosity.

    Multiple callers in the same file are grouped as:
      file (member1, member2)
    A single caller in a file is formatted as:
      file::member
    Callers without '::' are preserved as-is.
    """
    if not callers:
        return ""

    groups: Dict[str, List[str]] = {}
    tokens: List[tuple] = []
    seen_files = set()
    seen_raw = set()

    for caller in callers:
        caller_str = str(caller).strip()
        if not caller_str:
            continue
        if "::" in caller_str:
            file_path, member = caller_str.split("::", 1)
            file_path = file_path.strip()
            member = member.strip()
            if file_path not in groups:
                groups[file_path] = []
            if member and member not in groups[file_path]:
                groups[file_path].append(member)
            if file_path not in seen_files:
                seen_files.add(file_path)
                tokens.append(("file", file_path))
        else:
            if caller_str not in seen_raw:
                seen_raw.add(caller_str)
                tokens.append(("raw", caller_str))

    formatted_parts: List[str] = []
    for kind, key in tokens:
        if kind == "raw":
            formatted_parts.append(key)
        else:
            members = groups[key]
            if not members:
                formatted_parts.append(key)
            elif len(members) == 1:
                formatted_parts.append(f"{key}::{members[0]}")
            else:
                formatted_parts.append(f"{key} ({', '.join(members)})")

    return ", ".join(formatted_parts)


def render_symbol_view(payload: Dict[str, Any]) -> str:
    """The detail block a search inlines, so finding and reading cost one turn."""
    if payload.get("view") == "implementation":
        lines = payload.get("lines") or ["?", "?"]
        lc = payload.get("line_count")
        block_span = f" (block lines 1..{lc})" if lc else ""
        header = (
            "  implementation " + str(payload.get("file", ""))
            + " L" + str(lines[0]) + "-" + str(lines[1]) + block_span
        )
        content_hash = str(payload.get("content_hash") or "")
        if content_hash:
            header += " | hash " + content_hash
        callers = payload.get("callers") or []
        if callers:
            formatted = format_callers(callers)
            if formatted:
                header += " | callers: " + formatted
        numbered = str(payload.get("numbered_code") or "")
        if numbered:
            # Block-relative numbers, identical to read_block's: this view is the
            # editing buffer, so the edit needs no prior read_block.
            return header + "\n" + "\n".join("  " + line for line in numbered.splitlines())
        code = str(payload.get("code") or "")
        if not code:
            return header
        return header + "\n" + "\n".join("  | " + line for line in code.splitlines())
    parts = ["  contract " + " ".join(str(payload.get("signature") or "").split())]
    docstring = str(payload.get("docstring") or "").strip()
    if docstring:
        parts.append("  doc: " + docstring.splitlines()[0][:200])
    calls = payload.get("calls") or []
    if calls:
        parts.append("  calls: " + ", ".join(str(name) for name in calls))
    callers = payload.get("callers") or []
    if callers:
        formatted = format_callers(callers)
        if formatted:
            parts.append("  callers: " + formatted)
    return "\n".join(parts)


def render_symbol_matches(
    matches: List[Dict[str, Any]],
    views: Dict[str, Any] | None = None,
    errors: Dict[str, str] | None = None,
) -> str:
    """One line per match, as symbol_id [kind] :: signature.

    When the caller asked for a view, each match is followed by the detail block
    it would otherwise have had to request in a second call.
    """
    lines: List[str] = []
    for match in matches:
        # `displayed_id` is the path the surface prints; `symbol_id` is the stored coordinate,
        # which carries a sha1 for an overload and must never reach the agent.
        line = str(match.get("displayed_id") or match.get("symbol_id", ""))
        kind = match.get("kind")
        if kind:
            line += " [" + str(kind) + "]"
        signature = " ".join(str(match.get("signature") or "").split())
        if signature:
            line += " :: " + signature
        lines.append(line)
        payload = (views or {}).get(str(match.get("symbol_id", "")))
        if payload:
            lines.append(render_symbol_view(payload))
    for symbol_id, message in (errors or {}).items():
        lines.append(str(symbol_id) + " [error] " + str(message))
    return "\n".join(lines)


def render_document_matches(matches: List[Dict[str, Any]]) -> str:
    """One block per match: standard block header and numbered text when inlined complete,
    or section id, heading and excerpt/body otherwise."""
    blocks: List[str] = []
    for match in matches:
        if match.get("content_hash") and match.get("numbered_text"):
            addr = str(match.get("displayed_id") or match.get("section_id", ""))
            kind = match.get("kind", "section")
            hash_val = match.get("content_hash")
            line_count = match.get("line_count")
            if line_count is None:
                line_count = len(match["numbered_text"].splitlines())
            header = f"{addr} [{kind}] [h:{hash_val}] ({line_count} lines)"
            blocks.append(header + "\n" + match["numbered_text"])
            continue

        # `displayed_id` is the path the surface prints (rank included when two headings of one
        # title sit under the same parent); `section_id` is the stored coordinate.
        header = str(match.get("displayed_id") or match.get("section_id", ""))
        heading = str(match.get("heading") or "").strip()
        if heading:
            header += " :: " + heading
        body = str(match.get("content") or match.get("snippet") or "").strip()
        if match.get("content_truncated"):
            body += (
                "\n[... cut here, " + str(match.get("content_total_chars"))
                + " chars total; read " + str(match.get("displayed_id") or match.get("section_id")) + " for the rest ...]"
            )
        blocks.append(header + ("\n" + body if body else ""))
    return "\n\n".join(blocks)


def render_section_batch(sections: List[Dict[str, Any]], errors: Dict[str, str]) -> str:
    """One block per section: id and heading, then its text (or its excerpt)."""
    blocks: List[str] = []
    for section in sections:
        header = str(section.get("section_id") or section.get("id") or "")
        heading = str(section.get("heading") or "").strip()
        if heading:
            header += " :: " + heading
        content_hash = str(section.get("content_hash") or "")
        if content_hash and section.get("view") == "full":
            # Same hash as read_block: the section can be edited from this very read.
            header += " | hash " + content_hash
        body = section.get("numbered_text") or section.get("content")
        if body is None:
            excerpt = section.get("snippet")
            body = ("[excerpt] " + str(excerpt)) if excerpt else ""
        block = header + ("\n" + str(body) if body else "")
        # The cut marker must NOT sit on the `id :: heading` line: a caller reading
        # that line would take `Heading [truncated, 1568 chars total]` for the
        # heading itself. Keep it after the body, like render_document_matches.
        if section.get("content_truncated"):
            block += (
                "\n[... cut here, " + str(section.get("content_total_chars"))
                + " chars total; read " + str(section.get("section_id") or section.get("id"))
                + " for the rest ...]"
            )
        blocks.append(block)
    for section_id, message in (errors or {}).items():
        blocks.append(str(section_id) + " [error] " + str(message))
    return "\n\n".join(blocks)


def _empty_map_section(payload: Dict[str, Any], kind: str) -> str:
    """Why a section of the map carries nothing -- said, not left blank.

    A bare `files:` under a filter reads as "nothing here", and an agent could not tell
    a script this tool never lists from a path that does not exist: "I could not confirm
    whether those two scripts exist on disk from this output" (declared on mapab2 b r2
    of the base c819545).
    """
    pattern = payload.get("file_pattern")
    if not pattern:
        return "  (none is indexed)" if kind == "document" else "  (none declares a container)"
    matched = payload.get("filtered_files") or 0
    if not matched:
        return "  (no indexed file matches '" + str(pattern) + "')"
    if kind == "document":
        return (
            "  (no document among the " + str(matched) + " indexed file(s) matching '"
            + str(pattern) + "')"
        )
    return (
        "  (none of the " + str(matched) + " indexed file(s) matching '" + str(pattern)
        + "' declares a container: a script, or a file without symbols, is not listed here)"
    )


def _format_doc_map_entry(filename: str, doc: Dict[str, Any], depth: int, max_sections: int = 6) -> str:
    indent = "  " * depth
    child_indent = indent + "  "

    secs = doc.get("sections")
    sec_titles = doc.get("section_titles")
    if sec_titles is None and isinstance(secs, list):
        sec_titles = secs
        total_sections = len(secs)
    elif isinstance(secs, int):
        total_sections = secs
    else:
        total_sections = len(sec_titles) if isinstance(sec_titles, list) else 0

    lines = [f"{indent}{filename} ({total_sections} sections)"]

    if sec_titles and isinstance(sec_titles, list):
        shown = sec_titles[:max_sections]
        for heading in shown:
            lines.append(f"{child_indent}::{heading}")
        remaining = total_sections - len(shown)
        if remaining > 0:
            lines.append(f"{child_indent}::... (+{remaining} autres sections)")

    return "\n".join(lines)


def _format_file_map_entry(
    filename: str,
    entry: Dict[str, Any],
    depth: int,
    max_symbols_per_file: int = 6,
) -> str:
    indent = "  " * depth
    child_indent = indent + "  "

    sym_count = entry.get("sym_count")
    symbol_details = entry.get("symbol_details")
    raw_symbols = entry.get("symbols") or []

    if sym_count is None:
        if symbol_details is not None:
            sym_count = len(symbol_details)
        else:
            sym_count = len(raw_symbols)

    loc = entry.get("loc")
    if loc is not None:
        header = f"{indent}{filename} ({sym_count} symboles, {loc}L)"
    else:
        header = f"{indent}{filename} ({sym_count} symboles)"

    lines = [header]

    if symbol_details:
        shown = symbol_details[:max_symbols_per_file]
        for sym in shown:
            kind = sym.get("kind") or ""
            name = sym.get("name") or ""
            prefix = f"{kind} {name}".strip()
            start = sym.get("start_line")
            end = sym.get("end_line")
            if start is not None and end is not None:
                lines.append(f"{child_indent}::{prefix} (L{start}-L{end})")
            else:
                lines.append(f"{child_indent}::{prefix}")
        remaining = sym_count - len(shown)
        if remaining > 0:
            lines.append(f"{child_indent}::... (+{remaining} autres symboles)")
    elif raw_symbols:
        shown = raw_symbols[:max_symbols_per_file]
        for sym in shown:
            lines.append(f"{child_indent}::{sym}")
        remaining = sym_count - len(shown)
        if remaining > 0:
            lines.append(f"{child_indent}::... (+{remaining} autres symboles)")

    return "\n".join(lines)


def _render_tree_entries(
    items: List[Dict[str, Any]],
    format_entry: Any,
    base_depth: int = 1,
) -> List[str]:
    """Render items into a directory tree where parent folders are factored out with indentation."""
    class _DirNode:
        def __init__(self) -> None:
            self.subdirs: Dict[str, "_DirNode"] = {}
            self.files: List[Tuple[str, Dict[str, Any]]] = []

    root = _DirNode()
    for item in items:
        raw_path = str(item.get("file_path") or item.get("id") or "").replace("\\", "/").strip("/")
        if not raw_path:
            continue
        parts = raw_path.split("/")
        curr = root
        for segment in parts[:-1]:
            if segment not in curr.subdirs:
                curr.subdirs[segment] = _DirNode()
            curr = curr.subdirs[segment]
        curr.files.append((parts[-1], item))

    lines: List[str] = []

    def _render_node(node: _DirNode, depth: int) -> None:
        indent = "  " * depth
        for dirname in sorted(node.subdirs.keys()):
            lines.append(f"{indent}{dirname}/")
            _render_node(node.subdirs[dirname], depth + 1)
        for fname, it in sorted(node.files, key=lambda x: x[0]):
            formatted = format_entry(fname, it, depth)
            if isinstance(formatted, list):
                lines.extend(formatted)
            elif "\n" in formatted:
                lines.extend(formatted.splitlines())
            else:
                lines.append(formatted)

    _render_node(root, base_depth)
    return lines


def render_project_map(payload: Dict[str, Any]) -> str:
    """A compact structural map: a summary line, then documents, then files in hierarchical tree format."""
    summary = payload.get("summary") or {}
    lines = [
        "budget " + str(payload.get("budget_tokens")) + " tokens | used ~"
        + str(payload.get("used_tokens_estimate")) + " | truncated: "
        + ("yes" if payload.get("truncated") else "no"),
        "symbols " + str(summary.get("symbols")) + " | documents " + str(summary.get("documents"))
        + " | sections " + str(summary.get("sections")),
        "documents:",
    ]
    documents = payload.get("documents") or []
    if documents:
        lines.extend(_render_tree_entries(documents, _format_doc_map_entry, base_depth=1))
    else:
        lines.append(_empty_map_section(payload, "document"))
    lines.append("files:")
    files = payload.get("files") or []
    if files:
        lines.extend(_render_tree_entries(files, _format_file_map_entry, base_depth=1))
    else:
        lines.append(_empty_map_section(payload, "file"))
    return "\n".join(lines)


def render_documents(documents: List[Dict[str, Any]]) -> str:
    """One line per document, as path :: title (N sections)."""
    lines: List[str] = []
    for document in documents:
        path = str(document.get("file_path") or document.get("id") or "")
        title = str(document.get("title") or "").strip()
        if title:
            path += " :: " + title
        count = document.get("section_count")
        if count is not None:
            path += " (" + str(count) + " sections)"
        lines.append(path)
    return "\n".join(lines)


def render_project_tree(paths: List[str], budget_chars: int = 6000, max_names_per_dir: int = 8) -> str:
    """A budgeted directory view: folders, sub-folders and file names.

    The Dossier answers "what is relevant for this intent"; the map answers
    "how is this repository organised", which the agent otherwise rebuilds with
    list_documents, get_project_map or repeated searches. It is deliberately
    independent of the query: it is rendered from the persistent list of indexed
    files, so two different intents get the same map.
    """
    by_directory: Dict[str, List[str]] = {}
    for raw in paths:
        path = str(raw).replace("\\", "/").lstrip("/")
        if not path:
            continue
        parent, _separator, name = path.rpartition("/")
        by_directory.setdefault(parent, []).append(name)

    total_files = sum(len(names) for names in by_directory.values())
    lines = ["[carte] " + str(total_files) + " fichiers, " + str(len(by_directory)) + " dossiers"]
    used = len(lines[0]) + 1
    truncated = False
    # Shallow directories first, then deeper: the reader sees the top of the tree
    # before it is spent on leaves. Alphabetical order alone let dot-directories
    # (`.archive`, `_archive`) consume the whole budget before `spark/src`.
    for directory in sorted(
        by_directory, key=lambda name: (0 if not name else name.count("/") + 1, name)
    ):
        names = sorted(by_directory[directory])
        shown = names[:max_names_per_dir] if max_names_per_dir > 0 else names
        more = "" if len(shown) == len(names) else " (+" + str(len(names) - len(shown)) + " autres)"
        depth = 0 if not directory else directory.count("/") + 1
        label = (directory + "/") if directory else "./"
        line = ("  " * (depth + 1)) + label + " (" + str(len(names)) + "): " + ", ".join(shown) + more
        room = budget_chars - used
        if room <= 0:
            truncated = True
            break
        if len(line) + 1 > room:
            line = line[: max(0, room - 24)] + " ... (+autres)"
            lines.append(line)
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if truncated:
        lines.append("[carte tronquee au budget]")
    return "\n".join(lines)

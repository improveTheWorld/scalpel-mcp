"""Surgical byte-range file patcher and mutation manager."""

import bisect
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import textwrap
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from .config import ServerConfig
from .database import DatabaseManager
from .documents import (
    DocumentSection,
    heading_lines,
    markdown_layout,
    section_title,
    stable_section_ids,
    unclosed_fence_line,
)
from .filelock import FileLocks, LockTimeout
from .fragments import apply_edits
from .linetranslate import LineTranslation, LineTranslationError
from .lineops import apply_ops, numbered, render_edit_snippet
from .indentation import IndentationManager
from .indexer import Indexer
from .parsers import ParserRegistry
from .parsers.base import compute_content_hash
from .watcher import SuppressionRegistry
from .history import HistoryManager


#: The rule a BATCH answers by, stated once: a batch is ONE intent whose entries were read
#: against ONE version of the file, so a syntax barrier on any entry refuses them all. Half a
#: batch changes the file, and the 'expected_hash' of the entries that were refused with it no
#: longer opens their block: the caller would have to repair the tool's partial write before it
#: could retry what IT got wrong (measured on scalpel_16k_round006_ordre1 t15/t16: a batch of 7
#: left one method rewritten and six refusals whose hashes were stale by the next call, and the
#: agent spent the following two turns re-reading before it could write the six).
BATCH_ATOMICITY_RULE = (
    "A batch is ONE intent: its entries were read against one version of the file, so a syntax "
    "barrier on any entry refuses them ALL. Nothing was written by this batch, every "
    "expected_hash you sent still opens its block, and fixing the entry named above is the "
    "whole repair."
)


def _block_lines(text: str) -> List[str]:
    """The lines of a block, with no phantom last line.

    A block read from the graph carries no trailing newline while the same block written to
    disk ends in one, so the two split into a different LAST element for the same content
    ("- c" against ""). Comparing them in that state makes the rebaser see a change that is
    not there and lose an identity -- measured. Dropping the empty tail is the whole
    normalisation, and it invents no line: it only refuses to count a bare newline as one.
    """
    return [line.rstrip("\r") for line in text.split("\n")][:-1] if text.endswith("\n") \
        else [line.rstrip("\r") for line in text.split("\n")]


class Patcher:
    """Performs surgical byte-range replacements and insertions on disk with syntax validation."""

    def __init__(
        self,
        config: ServerConfig,
        db: DatabaseManager,
        indexer: Indexer,
        registry: Optional[ParserRegistry] = None,
        indentation_manager: Optional[IndentationManager] = None,
        suppression_registry: Optional[SuppressionRegistry] = None,
        history_manager: Optional[HistoryManager] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.indexer = indexer
        self.registry = registry or ParserRegistry()
        self.indent_mgr = indentation_manager or IndentationManager()
        self.suppression = suppression_registry
        self.history_manager = history_manager
        self._restoring_files = False
        #: A VALIDATION PASS: while it is set, every write is suppressed, so a batch can be checked
        #: against the file as the caller read it BEFORE its first byte moves, which is why the
        #: barrier sits at the one choke point every write goes through.
        self._preview_pass = False
        #: A DRY RUN: while this list is set, _atomic_write APPENDS the bytes instead of
        #: writing them, so a caller can ask what a write WOULD produce and run the guards
        #: that read the result before a single byte moves (see Patcher._title_route). It is
        #: checked BEFORE _preview_pass: a dry run inside a validation pass still captures.
        self._capture_writes: Optional[List[bytes]] = None
        #: THE EDIT LOCK (src/ast_graph_server/filelock.py). Per FILE, never global: the call
        #: that writes a file is ALONE in it while it runs, and a READER of that same file waits
        #: too -- not to avoid a torn read, which cannot happen (the write is a temp file then
        #: os.replace), but because its read would be stale BY CONSTRUCTION and cost it a turn.
        #: It covers the INDEX pass as well: the index of a file is written under the same lock,
        #: which is what makes "database is locked" impossible on our own path.
        self._edits_lock = FileLocks()
        #: THE STATE OF THE CALLER'S READ, per BLOCK, for the duration of ONE grouped call: the
        #: block as the CALLER read it (the version every entry is expressed against -- line
        #: numbers are block-relative, so a rebaser built on the file would be off by the
        #: preamble), the block as it now stands, the bytes of the whole file as this call last
        #: left it (`file_bytes`, what tells a THIRD PARTY's write from ours), the block's byte
        #: span, its rebaser, and the lines an entry already wrote. None outside a grouped call.
        self._call_state: Optional[Dict[str, Dict[str, Any]]] = None
        #: One rebaser per block, alive for the whole grouped call: it holds the identity of
        #: every line the caller read and where that line stands NOW (see LineTranslation).
        self._call_translation: Dict[str, LineTranslation] = {}
        #: THE BYTES a refused grouped call had already written, and the state this call last
        #: left each file in. `originals` is what `_restore_files` puts back when a syntax
        #: barrier is discovered only in the real pass; `_batch_after` is the per-file record
        #: of our own write. Both None outside a grouped call, and the single-call path never
        #: reads them.
        self._batch_originals: Optional[Dict[str, bytes]] = None
        self._batch_after: Optional[Dict[str, Tuple[bytes, int]]] = None
        #: The content_hash each edited BLOCK had before this call wrote anything: the
        #: version a caller stamp must open. Keyed by the path the caller WROTE, because
        #: that is the address every entry carries -- several blocks of one file can be
        #: edited in the same call, and each has its own stamp. It is captured the first
        #: time the block is resolved, which is the last moment before a write can move
        #: it, and it is never overwritten afterwards.
        self._call_block_hashes: Dict[str, str] = {}
        #: The translation the entry currently running was given: the write path reads it to
        #: record what that entry wrote, in the caller's own numbering.
        self._last_translation: Optional[Any] = None
        #: The caller-numbered lines the entry currently running WRITES: read by _record_write
        #: to build the conflict table. Never derived from the translated operations, which
        #: carry CURRENT numbers (that mistake invented a conflict, measured).
        self._last_written_lines: List[int] = []
        #: True while a grouped call's VALIDATION pass resolves every block: that pass is the
        #: only place a caller stamp is recorded (see Patcher._remember_stamps), and the flag
        #: keeps a single call from ever writing to the table it reads.
        self._capturing_stamps = False
        self._batch_entry_index = 0
        #: Short cache for markdown_layout results: (abs_path_str, content_hash) -> List[dict]
        self._markdown_layout_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _newline_bytes(file_bytes: bytes) -> bytes:
        """Return the dominant newline sequence used by a file."""
        return b"\r\n" if b"\r\n" in file_bytes else b"\n"

    @classmethod
    def _match_newline(cls, text: str, file_bytes: bytes) -> str:
        """Rewrite a snippet so its newlines match the target file (LF vs CRLF)."""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if cls._newline_bytes(file_bytes) == b"\r\n":
            return normalized.replace("\n", "\r\n")
        return normalized

    def _syntax_error_payload(
        self,
        identifier_key: str,
        identifier: str,
        err_objs: List[Any],
        message: str,
        region: Optional[Tuple[int, int]] = None,
        touches_last_line: bool = False,
        block_lines: int = 0,
    ) -> Dict[str, Any]:
        err_strings = [str(e) for e in err_objs]
        err_dicts = [e.to_dict() if hasattr(e, "to_dict") else {"message": str(e)} for e in err_objs]
        hint = err_objs[0].actionable_hint if err_objs and hasattr(err_objs[0], "actionable_hint") else ""
        validation: Dict[str, Any] = {
            "syntax_ok": False,
            "errors": err_strings,
            "error_details": err_dicts,
            "actionable_hint": hint,
        }
        if region is not None:
            # The parser reports *file* lines of the spliced document, but the
            # caller reasoned on *block* lines. Measured on a real run: the agent
            # replaced block lines 90-101, was told "Syntax error at line 184",
            # and spent four turns retrying shifted ranges because nothing tied
            # the two coordinate systems together.
            first, last = region
            validation["edited_file_lines"] = [first, last]
            inside = False
            for detail in err_dicts:
                line = detail.get("line")
                if not isinstance(line, int):
                    continue
                detail["file_line"] = line
                if first <= line <= last:
                    inside = True
                    detail["edited_region_line"] = line - first + 1
                else:
                    detail["in_edited_region"] = False
            if not inside and err_dicts:
                validation["note"] = (
                    "Every reported syntax error sits outside the region your content replaced "
                    "(file lines " + str(first) + "-" + str(last) + "): the replacement unbalanced the "
                    "surrounding structure (brace, parenthesis, semicolon or string) rather than "
                    "containing the syntax error itself."
                )
        # The signature of the mistake the agents make most here: the replacement
        # itself ends with the closing delimiter, so the parse error lands on its LAST
        # line (measured on mapab2 a r1 of the base 9016f1d: region [10, 107], error on
        # region line 98). Saying it beats letting the model guess from a far-away line.
        if region is not None:
            length = last - first + 1
            if any(detail.get("edited_region_line") == length for detail in err_dicts):
                end_note = (
                    "The syntax error sits on the LAST line of the lines your content replaced: "
                    "your replacement most likely closes a delimiter (brace, parenthesis, bracket) "
                    "that the block already closes right after it. Drop the trailing delimiter, or "
                    "extend the range over the line that follows."
                )
                validation["note"] = (
                    validation["note"] + " " + end_note if validation.get("note") else end_note
                )
        if touches_last_line and block_lines:
            # The agent asked for exactly this: "the failure was caused by my replacement
            # range including the class closing brace, which the tool did not flag as a
            # range problem" (declared twice, base 9016f1d, and paid two turns each time).
            structural = (
                "Your edit reaches the LAST line of the block (line " + str(block_lines)
                + "), which carries its closing delimiter. A replacement that closes the block a "
                "second time leaves a stray '}': that is the usual cause of a syntax error "
                "reported far below the lines you edited."
            )
            validation["note"] = (validation["note"] + " " + structural) if validation.get("note") else structural
        # An error message that does not carry its own diagnostic is a defect: an agent
        # refused with the bare "Syntax validation failed. Operation rejected and file remains
        # untouched." saw nothing to repair, while the line, the column and the offending
        # snippet sat in a NEIGHBOURING field it never read (measured on
        # scalpel_16k_round006_ordre1 t15: an edit_block batch of 7, 6 refusals carrying
        # 'validation.errors' and not one of them naming a line). The first entry of
        # 'validation.errors' is the parser's own sentence (line:col + snippet), so the
        # 'error' field repeats it verbatim rather than paraphrasing it.
        error = message
        if err_strings:
            error = error + " " + err_strings[0]
        return {
            "status": "error",
            identifier_key: identifier,
            "error": error,
            "validation": validation,
        }

    @staticmethod
    def _line_region(data: bytes, start: int, length: int) -> Tuple[int, int]:
        """1-based line span of data[start:start+length], for error localisation."""
        first = data.count(b"\n", 0, start) + 1
        last = data.count(b"\n", 0, start + max(length, 0)) + 1
        return (first, max(first, last))

    # ------------------------------------------------------------------
    # Safety guards (an agent must not be able to corrupt a file)
    # ------------------------------------------------------------------
    @staticmethod
    def _starts_with_doc_comment(code: str) -> bool:
        """True when the first non-blank submitted line is a /// comment."""
        for line in code.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            return stripped.startswith("///")
        return False

    @staticmethod
    def _leading_doc_start(file_bytes: bytes, start_byte: int) -> int:
        """Byte offset of the contiguous /// block sitting directly above start_byte.

        A symbol's recorded range starts at its declaration, so its XML
        documentation comment lives *outside* that range. Rewriting the
        declaration with a payload that repeats the comment therefore prepended
        a second copy, and deleting the declaration left the comment orphaned.
        Extending the range over the doc block makes both operations whole.
        """
        best = start_byte
        idx = start_byte
        while idx > 0:
            prev_newline = idx - 1
            if file_bytes[prev_newline : prev_newline + 1] != b"\n":
                break
            line_start = file_bytes.rfind(b"\n", 0, prev_newline) + 1
            line = file_bytes[line_start:prev_newline]
            if line.lstrip(b" \t\r").startswith(b"///"):
                best = line_start
                idx = line_start
            else:
                break
        return best

    @staticmethod
    def _base_symbol_id(symbol_id: str) -> str:
        return symbol_id.split("#", 1)[0]

    #: Kinds that must not appear twice with the same qualified name.
    DUPLICATED_DECLARATION_KINDS = frozenset(
        {"class", "struct", "interface", "record", "enum", "namespace"}
    )

    def _duplicate_declaration(
        self,
        parser: Any,
        file_path: str,
        before_bytes: bytes,
        after_bytes: bytes,
    ) -> Optional[str]:
        """Return the declaration name a mutation would duplicate, if any.

        Syntax validation cannot catch a duplicated using directive or a
        duplicated top-level type: both are valid C#. Re-parsing before and
        after, then comparing how many symbols share the same base identity,
        does catch it. Method/property names are excluded because overloads are
        legitimate.
        """
        before = parser.parse_file(file_path, before_bytes)
        after = parser.parse_file(file_path, after_bytes)
        kinds = self.DUPLICATED_DECLARATION_KINDS
        before_counts = Counter(self._base_symbol_id(s.id) for s in before.symbols if s.kind in kinds)
        after_counts = Counter(self._base_symbol_id(s.id) for s in after.symbols if s.kind in kinds)
        for base, count in after_counts.items():
            if count > 1 and count > before_counts.get(base, 0):
                return base
        return None

    def _reject_duplicate(self, key: str, identifier: str, base: str) -> Dict[str, Any]:
        return {
            "status": "error",
            key: identifier,
            "error": (
                "Mutation refused: it would duplicate the declaration '" + base + "'. "
                "The file is left untouched."
            ),
            "validation": {
                "syntax_ok": True,
                "errors": ["duplicate declaration: " + base],
                "actionable_hint": (
                    "Never submit a whole-file or whole-container payload into a single symbol. "
                    "Submit only the symbol's own declaration, or use a text tool when the change "
                    "spans several declarations."
                ),
            },
        }

    def _reject_namespace_mutation(self, operation: str, sym: Any) -> Dict[str, Any]:
        return {
            "status": "error",
            "symbol_id": sym.id,
            "error": (
                "Mutation refused: '" + sym.id + "' is a namespace declaration, and " + operation
                + " on a namespace rewrites every declaration it contains. The file is left untouched."
            ),
            "validation": {
                "syntax_ok": True,
                "errors": ["namespace mutation refused"],
                "actionable_hint": (
                    "Edit the namespace MEMBERS individually -- a class, a struct, a method, a "
                    "property: edit_block(path='file.cs::Ns::Classe', ops=[...]) rewrites one of "
                    "them, and insert_block(parent='" + sym.id + "', text=...) adds a new member. "
                    "This surface has no text tool: no operation here rewrites a namespace "
                    "declaration, and none is needed to change what it contains."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def _ast_depth(self, symbol: Any) -> int:
        """Nesting depth of a symbol, derived from the persisted parent chain.

        Indentation is a function of this depth and the file's indent unit, never
        of the bytes currently on disk. A block inserted at the wrong column can
        therefore be repaired by the next edit instead of being locked in: the
        previous code re-based every edit onto the symbol's *existing* first-line
        column, so a mis-indented member stayed mis-indented and the agent looped
        (update answered noop, delete+insert reproduced the wrong column).
        """
        depth = 0
        parent_id = getattr(symbol, "parent_id", None)
        seen = set()
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = self.db.get_symbol(parent_id)
            if not parent:
                break
            depth += 1
            parent_id = getattr(parent, "parent_id", None)
        return depth

    def _live_section(self, block_id: str):
        """(document_id, abs_path, text, layout item) for a section id, or None.

        The identifier is a name: `stable_section_ids` is recomputed on the text read
        from the FILE, and the name is matched against those. The stored table is not
        consulted, so it cannot point at a neighbour -- at worst it is stale and the
        name simply is not found.
        """
        document_id, separator, slug = block_id.partition("#")
        if not separator or not slug or not self._is_markdown(document_id):
            return None
        abs_path, text, _newline, error = self._resolve_markdown(document_id)
        if error:
            return None
        layout = markdown_layout(text)
        if not layout:
            return None
        for identifier, item in zip(self._layout_ids(document_id, layout), layout):
            if identifier == block_id:
                return document_id, abs_path, text, item
        return None

    def _layout_ids(self, document_id: str, layout: List[dict]) -> List[str]:
        """The identifiers this layout produces, aligned index by index with it.

        The names are recomputed from the headings the FILE holds, so an identifier
        never depends on a stored position: `stable_section_ids` is the only authority.
        """
        sections = [
            DocumentSection(
                heading=item["heading"],
                level=int(item["level"]),
                content="",
                order_index=int(item["index"]),
            )
            for item in layout
        ]
        return list(stable_section_ids(document_id, sections))

    @staticmethod
    def _hashes_match(expected: Optional[str], actual: Optional[str]) -> bool:
        """Does expected_hash match actual hash (exact match or prefix)?"""
        if not expected or not actual:
            return False
        exp = str(expected).strip()
        act = str(actual).strip()
        if exp == act:
            return True
        if len(exp) >= 6 and (act.startswith(exp) or exp.startswith(act)):
            return True
        return False

    def _child_carrying_hash(self, target_id: str, kind: str, expected_hash: str) -> Optional[str]:
        """The printed address of the child block whose content_hash is `expected_hash`, or None.

        A hash opens the block it was read from and nothing else. When the caller sends a CHILD's
        hash to its parent -- the frequent shape is a document addressed with one paragraph's
        hash -- the guard answered "the block changed since you read it": a cause that is FALSE
        (nothing had changed, these are two different blocks), followed by a hint that sent the
        caller to edit the empty root, which can only answer "replace range must be inside 1..0".
        Measured on run 1 of base ea25ac6 (mapab2/a): 11 refusals from t21 to t52, the first
        accepted edit_block at t63, the run cut at the 80-turn ceiling with a broken build.
        The lookup is deterministic -- one layout pass, the same one the read path uses -- so the
        section named here carries exactly the hash the caller sent.
        """
        if kind != "document" or not expected_hash:
            return None
        _abs_path, text, _newline, error = self._resolve_markdown(target_id)
        if error:
            return None
        layout = markdown_layout(text)
        if not layout:
            return None
        identifiers = self._layout_ids(target_id, layout)
        for index, item in enumerate(layout):
            if int(item["level"]) <= 0:
                continue
            start, end = int(item["section_start"]), int(item["section_end"])
            candidate = compute_content_hash(text[start:end].encode("utf-8"))
            if self._hashes_match(expected_hash, candidate) and index < len(identifiers):
                return self.displayed_section_id(identifiers[index])
        return None

    def _block_and_kind(self, block_id: str):
        """Resolve a NAME to (kind, meta, content, file_path, raw_bytes, span) or None.

        `span` is a BYTE span into `raw_bytes`, in ONE unit for every branch, and it is
        EXACT for every file: the symbol branch takes the parser's start_byte/end_byte, and
        the Markdown branches lay the text out on a ROUND-TRIPPING decode (`_byte_span`).
        Consumers slice bytes with it (`_begin_block_state`); measuring it on the text the
        surface PRINTS selected another window the moment a byte the UTF-8 decoder cannot
        read sat before the block (`errors="replace"` renders one such byte as U+FFFD, which
        re-encodes to three bytes).

        The name may be the one the surface PRINTS -- `file.cs::Classe::Methode`,
        `file.md::Section mere::Paragraphe` -- and not only the stored identifier: the
        stored id of an overload carries a SHA-1 the caller can neither read nor retype,
        so resolution by name is the door that makes the printed path usable.
        """
        from . import tracing

        tracing.note("resolve_start", block_id=block_id)
        symbol = self.db.get_symbol(block_id)
        if not symbol and "::" in block_id:
            resolved = self._resolve_displayed_path(block_id)
            tracing.note("resolve_named", block_id=block_id, resolved=resolved)
            if resolved:
                block_id = resolved
                symbol = self.db.get_symbol(block_id)
        if symbol:
            try:
                abs_path = self.config.to_absolute_path(symbol.file_path)
            except ValueError:
                return None
            if not abs_path.exists():
                return None
            raw = abs_path.read_bytes()
            slice_bytes = raw[symbol.start_byte : symbol.end_byte]
            if compute_content_hash(slice_bytes) != symbol.content_hash:
                # The file changed outside the tools (or a second database wrote it), so the
                # stored offsets no longer describe this symbol. A shifted slice returned with
                # status success is FALSE CONTENT -- worse than any refusal, because the agent
                # acts on it. Re-index once and retry; server._symbol_view does the same, and
                # the two paths must not disagree about the same symbol.
                # A READ must not crash on a WRITE it merely wanted to do. This re-index runs
                # inside `read_block` (and inside every tool that resolves a block), so a
                # database locked by another connection -- or a file being written at that
                # instant -- used to escape as an exception, and the caller got the SDK's
                # generic "Error executing tool read_block" with no cause at all. Failing to
                # refresh is NOT a reason to refuse an answer we can still check: the hash test
                # below decides, and it refuses when the content cannot be trusted.
                tracing.note("reindex_in_read", file=str(abs_path), symbol=block_id)
                try:
                    self._reindex(abs_path)
                except Exception as exc:
                    tracing.note(
                        "reindex_in_read_failed",
                        file=str(abs_path),
                        error=type(exc).__name__ + ": " + str(exc)[:300],
                    )
                refreshed = self.db.get_symbol(block_id)
                if refreshed is not None:
                    symbol = refreshed
                    raw = abs_path.read_bytes()
                    slice_bytes = raw[symbol.start_byte : symbol.end_byte]
                if compute_content_hash(slice_bytes) != symbol.content_hash:
                    return None
            content = slice_bytes.decode("utf-8", errors="replace")
            tracing.note("resolved", block_id=block_id, kind="symbol", bytes=len(slice_bytes))
            return (
                "symbol",
                {"file_path": symbol.file_path, "symbol_kind": symbol.kind},
                content,
                abs_path,
                raw,
                (symbol.start_byte, symbol.end_byte),
            )
        # A section identifier is a NAME: it is matched against the names the live
        # FILE produces, never against a stored position. The table used to decide
        # (name -> its order_index -> the layout entry at that index), so a table one
        # step behind the file answered with the name of one paragraph and the text of
        # its neighbour -- measured on mapab/a r1, where an edit then rewrote the
        # wrong section. Resolving by name removes the possibility: either the name
        # exists in the file, or it does not.
        live = self._live_section(block_id)
        if live is not None:
            document_id, abs_path, _text, target = live
            raw = self._markdown_bytes(abs_path)
            if raw is None:
                return None
            lossless = self._round_tripping_text(raw)
            layout = self._get_markdown_layout(abs_path, raw, lossless)
            item = self._layout_item_at(layout, target["index"])
            if item is None:
                return None
            content, span = self._block_view(
                raw, lossless, int(item["section_start"]), int(item["section_end"])
            )
            return (
                "section",
                {"file_path": document_id, "heading": target["heading"], "level": int(target["level"])},
                content,
                abs_path,
                raw,
                span,
            )
        section = self.db.get_document_section(block_id)
        if section and self._is_markdown(section["document_id"]):
            # Legacy positional identifiers (`file#2`) are not names: the table is the
            # only thing that can resolve them, and it is what they mean.
            abs_path, _text, _newline, error = self._resolve_markdown(section["document_id"])
            if error:
                return None
            raw = self._markdown_bytes(abs_path)
            if raw is None:
                return None
            lossless = self._round_tripping_text(raw)
            layout = self._get_markdown_layout(abs_path, raw, lossless)
            target = self._layout_item_at(layout, section["order_index"])
            if target is None:
                # A document with no heading at all is indexed as a single section
                # covering the whole file: that section IS the document root.
                if not layout:
                    return self._document_block(section["document_id"], abs_path, raw)
                return None
            content, span = self._block_view(
                raw, lossless, int(target["section_start"]), int(target["section_end"])
            )
            return (
                "section",
                {"file_path": section["document_id"], "heading": section["heading"], "level": section["level"]},
                content,
                abs_path,
                raw,
                span,
            )
        # The document itself: a file is a block too, and its id is its path. Its
        # own content is the text no heading owns -- the preamble, or the whole
        # file when it has no heading at all. Without this the parent node of every
        # section was unreachable: its preamble could not be read or edited, and a
        # file without any heading could not be edited at all.
        if self.db.get_document(block_id) is not None and self._is_markdown(block_id):
            abs_path, _text, _newline, error = self._resolve_markdown(block_id)
            if error:
                return None
            raw = self._markdown_bytes(abs_path)
            if raw is None:
                return None
            return self._document_block(block_id, abs_path, raw)
        return None

    @staticmethod
    def _round_tripping_text(raw: bytes) -> str:
        """`raw` decoded with a codec that re-encodes to `raw`, byte for byte.

        This is the text the SPAN is measured on, and it is used for NOTHING else.
        `errors="surrogateescape"` turns every byte the UTF-8 decoder cannot read into one
        character of U+DC80..U+DCFF, and `text.encode("utf-8", "surrogateescape")` gives that
        byte back -- so a layout offset in this text IS a byte offset in the file.
        `errors="replace"` cannot do that: it renders each such byte as U+FFFD, which
        re-encodes to THREE bytes (measured: 60 bytes 0xFF took a 91-byte file span from
        (71, 91) to (191, 211) -- an empty window). This text NEVER leaves the patcher:
        `errors="replace"` remains the codec of every string the agent reads, because
        `pydantic_core.to_json` -- the codec the MCP surface serializes tool results with --
        raises PydanticSerializationError on a lone surrogate.
        """
        return raw.decode("utf-8", "surrogateescape")

    @staticmethod
    def _markdown_bytes(abs_path: Any) -> Optional[bytes]:
        """The bytes on disk behind a resolved Markdown block, or None (a file that vanished)."""
        try:
            return Path(str(abs_path)).read_bytes()
        except OSError:
            return None

    def _get_markdown_layout(self, abs_path: Path, raw_bytes: bytes, lossless_text: str) -> List[Dict[str, Any]]:
        """Cached parsing of Markdown layout by (file_path, content_hash)."""
        chash = compute_content_hash(raw_bytes)
        key = (str(abs_path), chash)
        if hasattr(self, "_markdown_layout_cache") and key in self._markdown_layout_cache:
            return [dict(item) for item in self._markdown_layout_cache[key]]
        layout = markdown_layout(lossless_text)
        if hasattr(self, "_markdown_layout_cache"):
            if len(self._markdown_layout_cache) > 200:
                self._markdown_layout_cache.clear()
            self._markdown_layout_cache[key] = layout
        return [dict(item) for item in layout]

    @staticmethod
    def _layout_item_at(layout: List[dict], index: Any):
        """The layout entry whose `index` is `index`, or None.

        `index` is the paragraph ordinal, and it is the identity the two decodes AGREE on: a
        byte the decoder cannot read changes how a heading READS, never which LINE is a
        heading. The entry `_live_section` matched by name is therefore the same entry here;
        only the offsets differ, and the lossless ones are the offsets that speak bytes.
        """
        wanted = int(index)
        for item in layout:
            if int(item["index"]) == wanted:
                return item
        return None

    @classmethod
    def _block_view(cls, raw: bytes, lossless: str, start: int, end: int):
        """(what the agent reads, the exact BYTE span) for a range of the lossless text.

        `start`/`end` are offsets `markdown_layout` returned on the round-tripping text, so
        they address `raw` exactly. `content` is that byte range rendered with
        `errors="replace"` -- the codec the surface printed before, so the text the agent
        receives does not change.
        """
        span = cls._byte_span(lossless, start, end)
        return raw[span[0]:span[1]].decode("utf-8", "replace"), span

    @staticmethod
    def _byte_span(text: str, start: int, end: int):
        """A CHARACTER span of a ROUND-TRIPPING text, expressed in its BYTES.

        `text` must be a decode that re-encodes to the bytes it came from
        (`_round_tripping_text`): with that codec the conversion is EXACT for every file,
        including one the UTF-8 decoder cannot read. The span is consumed by slicing FILE
        BYTES (`_begin_block_state`, then `_record_write`), so this is the unit the tuple must
        carry for every branch -- the symbol branch already gives bytes.
        """
        return (len(text[:start].encode("utf-8", "surrogateescape")),
                len(text[:end].encode("utf-8", "surrogateescape")))

    def _document_root_end(self, text: str) -> int:
        """Offset where the document's own text stops and its first section starts."""
        for item in markdown_layout(text):
            if int(item["level"]) > 0:
                return int(item["section_start"])
        return len(text)

    def _document_block(self, document_id: str, abs_path: Any, raw: bytes):
        """The root block of a Markdown document: the text no heading owns."""
        lossless = self._round_tripping_text(raw)
        end = self._document_root_end(lossless)
        content, span = self._block_view(raw, lossless, 0, end)
        return (
            "document",
            {"file_path": document_id},
            content,
            abs_path,
            raw,
            span,
        )

    def block_metrics(self, block_id: str) -> Optional[Dict[str, Any]]:
        """Numbered text, content hash and line count of a block, or None.

        The numbers and the hash are exactly what read_block reports, because the
        discovery tools (inspect_symbols, search_symbols, get_document_section[s])
        call this too: code already shown to the agent is therefore directly
        editable -- the numbers are the ones edit_block expects and the hash is the
        one to pass as expected_hash. That removes the read_block that used to be
        required before every edit.
        """
        resolved = self._block_and_kind(block_id)
        if resolved is None:
            return None
        kind, meta, content, _abs_path, _raw, _span = resolved
        return {
            "kind": kind,
            "meta": meta,
            "content": content,
            "content_hash": compute_content_hash(content.encode("utf-8")),
            "line_count": len(content.splitlines()),
            "numbered_text": numbered(content, 1),
        }

    # --- structure on demand -------------------------------------------------
    # read_block answers STRUCTURE questions and never returns raw file text: a
    # directory is listed, a code file is outlined, a project/config file is
    # summarised. Returning the text would send the model back to the flat
    # file-by-file reading it was trained on -- the exact behaviour the index
    # exists to replace.

    #: Above this, a directory child is sized but NOT read for its line count: a
    #: listing stays a listing, and a multi-megabyte blob is not a block.
    LINE_COUNT_MAX_BYTES = 2_000_000

    def _existing_path(self, block_id: str) -> Optional[Path]:
        try:
            absolute = self.config.to_absolute_path(block_id)
        except ValueError:
            return None
        return absolute if absolute.exists() else None

    def _relative_path(self, absolute: Path) -> str:
        try:
            return Path(os.path.relpath(str(absolute), str(self.config.workspace_root))).as_posix()
        except ValueError:
            return absolute.name

    @classmethod
    def _sized_file(cls, path: str) -> Tuple[Optional[int], Optional[int]]:
        """(line count, bytes) of a file a listing sizes, without reading a blob.

        A listing sizes every child it names, so it reads each DIRECT child once to
        count its lines. Above LINE_COUNT_MAX_BYTES the read is skipped and the line
        count is None: the byte size still says what the caller is looking at.
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, None
        if size > cls.LINE_COUNT_MAX_BYTES:
            return None, size
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None, size
        if not data:
            return 0, size
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1), size

    def _directory_listing(self, block_id: str, absolute: Path) -> Dict[str, Any]:
        """The COMPLETE list of a directory's children: short name, lines, bytes.

        A child of a directory is a FILE or another directory, never a path: the
        directory's own path is written once, above. There is no window and no page:
        a listing that stops early is a listing the caller cannot act on, and the
        harness budget is 15x the worst listing of this repository.
        """
        indexer = getattr(self, "indexer", None)
        # ONE pass over the directory entries, and at most ONE stat each. The old form
        # asked is_dir, is_file and stat for EVERY entry to build a page of 200 -- 3 to 4
        # syscalls per entry -- which is what made a 10 000-entry directory cost 5.6 s and
        # a 100 000-entry one a minute. os.scandir also gives is_dir() from the directory
        # record itself, with no extra call at all.
        scanned: List[Tuple[bool, str, str, str]] = []
        ignored = 0
        ignored_by: List[str] = []
        try:
            with os.scandir(absolute) as scanner:
                for dirent in scanner:
                    try:
                        is_dir = dirent.is_dir()
                    except OSError:
                        is_dir = False
                    child_path = self._relative_path(Path(dirent.path))
                    pattern = (
                        indexer.ignore_pattern_for(child_path) if indexer is not None else None
                    )
                    if pattern is not None:
                        # An ignored directory used to read as EMPTY, with only a count: the
                        # caller could not tell "nothing here" from "hidden from me". The
                        # pattern names the reason; the hidden names stay private.
                        ignored += 1
                        if pattern not in ignored_by:
                            ignored_by.append(pattern)
                        continue
                    scanned.append((is_dir, dirent.name, child_path, str(dirent.path)))
        except OSError:
            pass
        scanned.sort(key=lambda item: (not item[0], item[1].lower()))
        children: List[Dict[str, Any]] = []
        lines_held = 0
        bytes_held = 0
        for is_dir, name, _child_path, absolute_str in scanned:
            if is_dir:
                # The trailing slash IS the type, and a directory has no line span and
                # no size of its own: what it holds is one call away.
                children.append({"name": name + "/"})
                continue
            line_count, size = self._sized_file(absolute_str)
            children.append({
                "name": name,
                "lines": [1, line_count] if line_count else None,
                "bytes": size,
            })
            lines_held += line_count or 0
            bytes_held += size or 0
        payload: Dict[str, Any] = {
            "status": "success",
            "id": block_id,
            # The block's own size: what the files it HOLDS directly weigh. A
            # subdirectory is not descended into, so it contributes nothing here.
            "line_count": lines_held,
            "bytes": bytes_held,
            "children": children,
        }
        if ignored:
            # An ignored directory used to read as EMPTY, with only a count: the caller
            # could not tell "nothing here" from "hidden from me". The pattern names the
            # reason; the hidden names stay private.
            payload["ignored_entries"] = ignored
            payload["ignored_by"] = ignored_by[:20]
        return payload

    def _symbol_rows(self, file_part: str) -> List[Dict[str, Any]]:
        """Every symbol of a file, under either spelling of the path it was given."""
        relative = file_part
        absolute = self._existing_path(file_part)
        if absolute is not None:
            relative = self._relative_path(absolute)
        with self.db.get_connection() as conn:
            for candidate in (relative, file_part.replace("\\", "/")):
                rows = conn.execute(
                    "SELECT id, name, parent_id, signature, kind, start_line, end_line, "
                    "start_byte, end_byte FROM symbols WHERE file_path = ? ORDER BY start_byte ASC",
                    (candidate,),
                ).fetchall()
                if rows:
                    return [dict(row) for row in rows]
        return []

    def _symbol_labels(self, rows: List[Dict[str, Any]]) -> Dict[str, str]:
        """The PRINTED name of every symbol of one file: short, plus `(params)` when the name
        is shared, plus a rank ` (2)` when even that is shared.

        Two declarations of one file may carry the same name: overloads, and the two halves of
        a partial class. What a developer writes, and what resolution accepts, is the parameter
        list; when two declarations agree on BOTH -- the real repository declares
        AggregateResultMapper twice, each holding `GetColumnName(string)` -- the parameter list
        discriminates nothing, and the refusal named two candidates whose `path` was identical
        to the character, advising the caller to "call read_block with the one you want". A
        rank makes the address real, exactly like the `(2)` the section printer already puts on
        two sibling headings of one title. A declaration with no parameter list at all (a
        class, a namespace) never gets parentheses.
        """
        labels: Dict[str, str] = {}
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row.get("parent_id"), []).append(row)
        for group in groups.values():
            counts: Dict[str, int] = {}
            for row in group:
                folded = self._fold_name(str(row.get("name") or ""))
                counts[folded] = counts.get(folded, 0) + 1
            seen: Dict[str, int] = {}
            for row in group:
                name = str(row.get("name") or "")
                if counts[self._fold_name(name)] > 1:
                    parameters = self._parameter_types(row.get("signature") or "")
                    if parameters is not None:
                        name += "(" + parameters + ")"
                key = self._fold_name(name)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] > 1:
                    name += " (" + str(seen[key]) + ")"
                labels[row["id"]] = name
        return labels

    @staticmethod
    def _symbol_id_chains(rows: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """The ancestry of every symbol as IDS, outermost first (itself last)."""
        by_id = {row["id"]: row for row in rows}
        order = {row["id"]: index for index, row in enumerate(rows)}
        chains: Dict[str, List[str]] = {}
        for row in rows:
            identifiers = [row["id"]]
            seen = {row["id"]}
            parent_id = row.get("parent_id")
            while (parent_id and parent_id not in seen
                   and order.get(parent_id, len(rows)) < order[row["id"]]):
                seen.add(parent_id)
                identifiers.append(parent_id)
                parent_id = by_id[parent_id].get("parent_id")
            identifiers.reverse()
            chains[row["id"]] = identifiers
        return chains

    @staticmethod
    def _symbol_chains(rows: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """The printed ancestry of every symbol: SHORT names, outermost first.
        
        A child is never named by its full address, and the prefix is written once,
        above: `SparkConnectOptions` holds `ApplyTo`, it is not named
        `SparkConnectOptions.ApplyTo` a second time. The chain is what the caller
        retypes to address a member, so it is also what resolution matches on.
        """
        order = {row["id"]: index for index, row in enumerate(rows)}
        chains: Dict[str, List[str]] = {}
        for row in rows:
            names = [str(row.get("name") or "")]
            seen = {row["id"]}
            parent_id = row.get("parent_id")
            # Parsers emit a container BEFORE the members it holds, so a parent always
            # has a lower index than its children: the order is the cycle guard.
            while (parent_id and parent_id not in seen
                   and order.get(parent_id, len(rows)) < order[row["id"]]):
                seen.add(parent_id)
                parent = rows[order[parent_id]]
                names.append(str(parent.get("name") or ""))
                parent_id = parent.get("parent_id")
            names.reverse()
            chains[row["id"]] = names
        return chains

    def _displayed_symbol_children(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The symbol tree of a file, named the way the caller will retype it.

        Two members of one parent may carry the same name -- C# overloads are exactly
        that. The stored discriminator is a SHA-1 of the declaration, which the caller
        can neither read nor judge; what a developer writes, and what the surface
        therefore PRINTS, is the parameter list. It is added only where a name is not
        enough, so the other 98% of names stay short.
        """
        nodes: Dict[str, Dict[str, Any]] = {}
        children_ids: Dict[str, List[str]] = {row["id"]: [] for row in rows}
        roots: List[str] = []
        for row in rows:
            nodes[row["id"]] = {
                "name": str(row.get("name") or ""),
                "lines": [int(row.get("start_line") or 0), int(row.get("end_line") or 0)],
                "bytes": max(0, int(row.get("end_byte") or 0) - int(row.get("start_byte") or 0)),
            }
        order = {row["id"]: index for index, row in enumerate(rows)}
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            parent_id = row.get("parent_id")
            if (parent_id in nodes and parent_id != row["id"]
                    and order.get(parent_id, len(rows)) < order[row["id"]]):
                children_ids[parent_id].append(row["id"])
            else:
                roots.append(row["id"])

        # ONE printer for the whole surface: the same labels `read_block` accepts and
        # `search_symbols` displays (parameter list when a name is shared, rank when even
        # that is shared). Two printers meant two vocabularies.
        labels = self._symbol_labels(rows)
        for identifier, node in nodes.items():
            node["name"] = labels.get(identifier, node["name"])

        def build(identifier: str) -> Dict[str, Any]:
            node = nodes[identifier]
            held = [build(child) for child in children_ids[identifier]]
            if held:
                node["children"] = held
            return node

        return [build(identifier) for identifier in roots]

    #: Symbol kinds whose kind and name ARE the whole declaration: printing the stored
    #: signature again would repeat the line the reader has just read.
    CONTAINER_SYMBOL_KINDS = frozenset({
        "namespace", "class", "interface", "struct", "record", "enum", "module", "trait",
    })
    #: Symbol kinds that take a parameter list, so their declaration prints as
    #: `Name(params) -> return`.
    CALLABLE_SYMBOL_KINDS = frozenset({
        "method", "function", "constructor", "destructor", "operator", "delegate",
    })
    #: Declaration words that say nothing about WHAT a block is -- the kind already says it.
    DECLARATION_MODIFIERS = frozenset({
        "public", "private", "protected", "internal", "static", "sealed", "abstract",
        "virtual", "override", "readonly", "const", "async", "partial", "unsafe", "extern",
        "new", "volatile", "required", "event", "file", "in", "out", "ref", "this",
        "params", "export", "default", "final", "declare",
    })
    #: Payload keys that describe the ANSWER of a listing, not the block it lists: the
    #: header line already carries the id and the sizes.
    LISTING_PAYLOAD_KEYS = ("status", "id", "format", "line_count", "bytes", "children")

    @staticmethod
    def _listing_sizes(line_count: Any, size: Any, unit: str = "B") -> str:
        """`120 lines, 3400 B` or `120 lines, 3400 chars`, with either half omitted when unknown."""
        parts: List[str] = []
        if line_count:
            parts.append(str(int(line_count)) + " lines")
        if size:
            parts.append(str(int(size)) + " " + unit)
        return ", ".join(parts)

    @staticmethod
    def _listing_count(count: int, noun: str, plural: str = "") -> str:
        """`1 symbol`, `6 symbols`, `4 entries`: a count that reads as English."""
        if count == 1:
            return str(count) + " " + noun
        return str(count) + " " + (plural or noun + "s")

    def _listing_headline(self, block_id: str, payload: Dict[str, Any], counted: str) -> str:
        """The FIRST line of a text listing: the block's own address and its size.

        The address is the one string that must not be repeated below -- it is what a child
        is appended to -- so it is written here, once, in the spelling read_block accepts.
        """
        sizes = self._listing_sizes(payload.get("line_count"), payload.get("bytes"))
        sizes = ", ".join(part for part in (counted, sizes) if part)
        return block_id + (("   " + sizes) if sizes else "")

    def _declaration_tail(self, label: str, kind: str, signature: str) -> str:
        """What the printed NAME does not already say: `(params) -> return`, or the type.

        The tail is READ from the stored signature -- the declaration line the parser kept --
        never rebuilt from the name, and only when the name is found in it: a label indexed
        under another spelling prints no tail rather than a wrong one. A container is its kind
        and its name (`class Spark`), and `using` is a keyword, so neither takes a tail.
        """
        text = " ".join(str(signature or "").split())
        if not text or kind in self.CONTAINER_SYMBOL_KINDS or kind == "using":
            return ""
        bare = label.split("(")[0]
        match = re.search(r"\b" + re.escape(bare) + r"\b", text)
        if match is None:
            return ""
        head = " ".join(
            word for word in text[: match.start()].split(" ")
            if word and word not in self.DECLARATION_MODIFIERS
        ).strip()
        rest = text[match.end():].strip()
        if rest.startswith("<"):
            # A generic declaration: `DataFrame<T>(...)`. The type arguments are part of the
            # name, and the parameter list is what follows them.
            close = rest.find(">")
            if close != -1:
                rest = rest[close + 1:].lstrip()
        parameters = ""
        returned = head
        if rest.startswith("("):
            depth = 0
            for position, character in enumerate(rest):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        # A declaration written across several lines collapses into one: the
                        # stored signature keeps the author's line breaks, the listing cannot.
                        parameters = "(" + " ".join(rest[1:position].split()) + ")"
                        after = rest[position + 1:].strip()
                        if after.startswith("->"):
                            returned = after[2:].split("{")[0].strip()
                        break
        if parameters or kind in self.CALLABLE_SYMBOL_KINDS:
            arrow = (" -> " + returned) if returned else ""
            # An overload prints its discriminator IN the label (`ApplyTo(Builder)`), which is
            # the address the caller retypes: repeating the parameter list would print two
            # spellings of one block.
            return arrow if "(" in label else parameters + arrow
        return (" -> " + head) if head else ""

    def _listing_symbol_line(self, node: Dict[str, Any], row: Optional[Dict[str, Any]], depth: int) -> str:
        """One line of a symbol tree: `<kind> <name>(<params>) -> <type>  (N lines)`."""
        label = str(node.get("name") or "")
        kind = str((row or {}).get("kind") or "")
        tail = self._declaration_tail(label, kind, str((row or {}).get("signature") or ""))
        span = node.get("lines") or [0, 0]
        first, last = int(span[0]), int(span[-1])
        count = last - first + 1 if (first > 0 and last >= first) else 0
        where = f"({count} line)" if count == 1 else f"({count} lines)"
        # A `using` block is indexed under the name the keyword already prints: one of the two
        # would be noise, so the line reads `using  (N lines)`.
        head = (kind + " ") if kind and kind != label else ""
        return "  " * (depth + 1) + head + label + tail + "  " + where

    def _listing_entry_line(self, child: Dict[str, Any]) -> str:
        """One line of a directory listing: a name, then its size when it has one."""
        name = str(child.get("name") or "")
        lines = child.get("lines")
        count = int(lines[-1]) if isinstance(lines, list) and lines else None
        sizes = self._listing_sizes(count, child.get("bytes"))
        return "  " + name + (("   " + sizes) if sizes else "")

    def _listing_property_lines(self, payload: Dict[str, Any]) -> List[str]:
        """The named properties of a project/config file, one line each."""
        lines: List[str] = []
        for key, value in payload.items():
            if key in self.LISTING_PAYLOAD_KEYS:
                continue
            if isinstance(value, list):
                if value and all(isinstance(item, dict) for item in value):
                    lines.append("  " + str(key) + ":")
                    for item in value:
                        parts = [str(part) for part in item.values() if part not in (None, "")]
                        lines.append("    " + " ".join(parts))
                else:
                    lines.append("  " + str(key) + ": " + ", ".join(str(item) for item in value))
            elif isinstance(value, dict):
                lines.append("  " + str(key) + ":")
                for name, inner in value.items():
                    if isinstance(inner, list):
                        lines.append("    " + str(name) + ": " + ", ".join(str(part) for part in inner))
                    else:
                        lines.append("    " + str(name) + ": " + str(inner))
            else:
                lines.append("  " + str(key) + ": " + str(value))
        return lines

    def render_listing_text(self, block_id: str, payload: Dict[str, Any]) -> str:
        """A listing as ONE text: the block's own address, then its children indented.

        The address is written ONCE, on the first line, and the tree under it is indentation:
        a child is addressed by the header plus '::' plus the name printed here, so repeating
        the path on every line would print the same string as many times as there are blocks.
        Each symbol line carries the KIND and the DECLARATION (`method Connect(string master,
        string appName) -> SparkContext  [53-129]`), which the JSON envelope never printed:
        measured on the benchmark repository, a listing that cost 2 203 characters of indented
        JSON costs 688 as text and says MORE (Read.Spark.cs: 885 -> 369, the tests file:
        2 156 -> 1 014).
        """
        children = payload.get("children")
        rows = self._symbol_rows(block_id) if isinstance(children, list) else []
        is_document = self._is_markdown(block_id) or (
            isinstance(children, list) and bool(children) and isinstance(children[0], dict) and "chars" in children[0]
        )
        if rows:
            headline = self._listing_headline(block_id, payload, self._listing_count(len(rows), "symbol"))
        elif is_document and isinstance(children, list):
            def count_nodes(nodes: List[Dict[str, Any]]) -> int:
                return sum(1 + count_nodes(n.get("children") or []) for n in nodes)
            total = count_nodes(children)
            sizes = self._listing_sizes(payload.get("line_count"), payload.get("chars"), unit="chars")
            counted = self._listing_count(total, "section", "sections")
            headline = block_id + (("   " + ", ".join(p for p in (counted, sizes) if p)) if (counted or sizes) else "")
        elif isinstance(children, list):
            headline = self._listing_headline(
                block_id, payload, self._listing_count(len(children), "entry", "entries")
            )
        else:
            headline = self._listing_headline(block_id, payload, "")
        lines = [headline]
        if rows:
            # The tree is answered from the SAME rows the JSON envelope was built from, keyed
            # by (first line, printed label): the kind and the signature are the only thing the
            # envelope left out, and they are read here, never guessed.
            labels = self._symbol_labels(rows)
            index = {
                (int(row.get("start_line") or 0), labels.get(row["id"], str(row.get("name") or ""))): row
                for row in rows
            }
            def walk(node: Dict[str, Any], depth: int) -> None:
                label = str(node.get("name") or "")
                span = node.get("lines") or [0, 0]
                row = index.get((int(span[0]), label))
                lines.append(self._listing_symbol_line(node, row, depth))
                for child in node.get("children") or []:
                    walk(child, depth + 1)
            for node in children:
                walk(node, 0)
        elif is_document and isinstance(children, list):
            def walk_doc(node: Dict[str, Any], depth: int) -> None:
                name = str(node.get("name") or "")
                span = node.get("lines") or [0, 0]
                first, last = int(span[0]), int(span[-1])
                count = last - first + 1 if (first > 0 and last >= first) else 0
                where = f"({count} line)" if count == 1 else f"({count} lines)"
                lines.append("  " * (depth + 1) + "#" * (depth + 1) + " " + name + "  " + where)
                for child in node.get("children") or []:
                    walk_doc(child, depth + 1)

            for node in children:
                walk_doc(node, 0)
            if payload.get("numbered_text"):
                lines.append("\n  Preamble:")
                for pline in str(payload["numbered_text"]).splitlines():
                    lines.append("    " + pline)
            elif payload.get("root_text_empty"):
                lines.append("\n  (Preamble is empty: 0 lines before first heading. Read sections below with their addresses)")
        elif isinstance(children, list):
            for child in children:
                lines.append(self._listing_entry_line(child))
        else:
            lines.extend(self._listing_property_lines(payload))
        return "\n".join(lines)

    def _listing_answer(
        self, block_id: str, payload: Dict[str, Any], answer_format: Optional[str] = None
    ) -> Any:
        """A listing in the format this server answers with: the object tree, or its text.

        The format covers LISTINGS ONLY. The TEXT of a block (numbered lines plus content
        hash), the refusals with their candidates and the write replies answer the same way
        in both modes, so a campaign that compares them varies the listing render and nothing
        else. The stored `id` of the JSON form is left untouched: only the text form prints
        the canonical address, on its first line.
        """
        mode = str(answer_format or getattr(self.config, "answer_format", "json") or "json").lower()
        # A REFUSAL is not a listing: a project file that does not parse, an ignored tree, a
        # path that is not there -- each answers with its own cause and hint, in both modes.
        # Wrapping one in a text envelope reported a success about a file that was not read
        # (caught by tests/test_structure_on_demand.py).
        if mode != "text" or str(payload.get("status")) != "success":
            return payload
        ans = {
            "status": "success",
            "id": block_id,
            "format": "text",
            "text": self.render_listing_text(block_id, payload),
        }
        if "content_hash" in payload:
            ans["content_hash"] = payload["content_hash"]
        return ans

    def _file_outline(self, block_id: str, absolute: Path) -> Optional[Dict[str, Any]]:
        """The COMPLETE symbol list of a code file: short names, spans, sizes.

        Structure only, never the source: the listing is the door the caller chooses a
        block through, and handing the text back would send the model down the flat
        file-by-file reading the index exists to replace.
        """
        rows = self._symbol_rows(block_id)
        if not rows:
            return None
        try:
            file_bytes = absolute.read_bytes()
            line_count = len(file_bytes.decode("utf-8", errors="replace").splitlines())
            size = len(file_bytes)
        except OSError:
            line_count, size = None, None
        return {
            "status": "success",
            "id": block_id,
            "line_count": line_count,
            "bytes": size,
            "content_hash": compute_content_hash(file_bytes) if file_bytes is not None else None,
            "children": self._displayed_symbol_children(rows),
            "actionable_hint": "Call read_block(paths=['" + block_id + "::sym1', ...]) to read multiple blocks in one turn.",
        }

    def _structured_summary(self, block_id: str, absolute: Path) -> Optional[Dict[str, Any]]:
        """Same envelope, NAMED properties: a project/config file has no block tree.

        Its children are not blocks with a span and a size -- they are the named
        things the file declares (packages, target frameworks, top-level keys), so
        they keep their names instead of being flattened into a uniform list.
        """
        suffix = absolute.suffix.lower()
        file_bytes: Optional[bytes] = None
        try:
            file_bytes = absolute.read_bytes()
            line_count: Optional[int] = len(file_bytes.decode("utf-8", errors="replace").splitlines())
            size: Optional[int] = len(file_bytes)
        except OSError:
            line_count, size = None, None
        payload: Dict[str, Any] = {"status": "success", "id": block_id,
                                   "line_count": line_count, "bytes": size}
        if suffix in (".csproj", ".props", ".targets", ".xml"):
            try:
                root = ET.parse(str(absolute)).getroot()
            except Exception as error:
                return self._summary_parse_refusal(block_id, suffix, str(error))
            packages, frameworks, projects = [], [], []
            for element in root.iter():
                tag = str(element.tag).split("}")[-1]
                if tag == "PackageReference":
                    version = element.get("Version")
                    if version is None:
                        for child in element:
                            if str(child.tag).split("}")[-1] == "Version" and child.text:
                                version = child.text.strip()
                    packages.append({"package": element.get("Include") or element.get("Update"),
                                     "version": version})
                elif tag in ("TargetFramework", "TargetFrameworks", "TargetFrameworkVersion"):
                    if element.text and element.text.strip():
                        # TargetFrameworks holds a LIST: "net8.0;net472" is two targets, and
                        # reporting it as one string made the summary say something the file
                        # does not (measured).
                        for part in re.split(r"[;,]", element.text):
                            if part.strip():
                                frameworks.append(part.strip())
                elif tag == "ProjectReference":
                    projects.append(element.get("Include"))
            payload["package_references"] = packages[:100]
            payload["target_frameworks"] = sorted(set(frameworks))[:20]
            payload["project_references"] = projects[:50]
            return payload
        if suffix == ".json":
            try:
                data = json.loads(absolute.read_text(encoding="utf-8", errors="replace"))
            except Exception as error:
                return self._summary_parse_refusal(block_id, suffix, str(error))
            rows = self._symbol_rows(block_id)
            if not isinstance(data, dict):
                if isinstance(data, list) and any(isinstance(item, dict) for item in data) and rows:
                    payload["top_level_keys"] = [
                        str(r["name"]) for r in rows if not r.get("parent_id")
                    ][:100]
                    payload["children"] = self._displayed_symbol_children(rows)
                    if file_bytes is not None:
                        payload["content_hash"] = compute_content_hash(file_bytes)
                    return payload
                return {
                    "status": "error", "id": block_id,
                    "error": (
                        "'" + block_id + "' is valid JSON, but its top level is a "
                        + type(data).__name__ + ", which has no keys to list: Scalpel "
                        "summarises the structure of an object only."
                    ),
                    "actionable_hint": "search_documents finds the paragraphs describing it.",
                }
            payload["top_level_keys"] = list(data.keys())[:100]
            payload["nested_keys"] = {
                key: (list(value.keys())[:20] if isinstance(value, dict) else None)
                for key, value in list(data.items())[:20]
            }
            if rows:
                payload["children"] = self._displayed_symbol_children(rows)
                if file_bytes is not None:
                    payload["content_hash"] = compute_content_hash(file_bytes)
            return payload
        if suffix in (".yaml", ".yml"):
            keys = []
            for line in absolute.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"^([A-Za-z_][A-Za-z0-9_.\-]*):", line)
                if match:
                    keys.append(match.group(1))
            rows = self._symbol_rows(block_id)
            if not keys and rows:
                keys = [str(r["name"]) for r in rows if not r.get("parent_id")]
            payload["top_level_keys"] = keys[:100]
            payload["validated"] = False
            payload["note"] = (
                "Keys are matched with a pattern, not read by a YAML parser: this summary "
                "says what the file LOOKS like and does not certify that it is valid YAML."
            )
            if rows:
                payload["children"] = self._displayed_symbol_children(rows)
                if file_bytes is not None:
                    payload["content_hash"] = compute_content_hash(file_bytes)
            return payload
        return None

    @staticmethod
    def _line_starts(text: str) -> List[int]:
        """Byte offset of every line start, built ONCE.

        Counting the newlines before EVERY section was O(sections x file size): a
        32 000-heading document took 31.8 s in read_block. bisect makes it O(log lines).
        """
        starts = [0]
        for index, character in enumerate(text):
            if character == "\n":
                starts.append(index + 1)
        return starts

    @staticmethod
    def _line_of(starts: List[int], position: int) -> int:
        return bisect.bisect_right(starts, max(0, position))

    @classmethod
    def _section_lines(cls, item: Dict[str, Any], starts: List[int]) -> List[int]:
        """The INCLUSIVE line span of a layout entry: the last line holds byte end-1."""
        start = int(item["section_start"])
        end = int(item["section_end"])
        last = end - 1
        return [cls._line_of(starts, start),
                cls._line_of(starts, last) if last >= start else cls._line_of(starts, start)]

    @classmethod
    def _section_labels(cls, layout: List[dict]) -> List[str]:
        """The PRINTED name of every entry: its title, plus its rank among its siblings.

        Two headings of one parent may carry the same title. The stored slug suffixes
        the second one ('new-features-2'), and the printed name has to be just as
        distinguishable, or two different blocks of one file would print one path.
        """
        parents: List[int] = []
        stack: List[Tuple[int, int]] = []
        for index, item in enumerate(layout):
            level = int(item["level"])
            if level <= 0:
                parents.append(-1)
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            parents.append(stack[-1][1] if stack else -1)
            stack.append((level, index))
        labels: List[str] = []
        seen: Dict[Tuple[int, str], int] = {}
        for index, item in enumerate(layout):
            if int(item["level"]) <= 0:
                labels.append("")
                continue
            title = section_title(str(item["heading"]))
            key = (parents[index], cls._fold_name(title))
            seen[key] = seen.get(key, 0) + 1
            labels.append(title if seen[key] == 1 else title + " (" + str(seen[key]) + ")")
        return labels

    @staticmethod
    def _bare_title(label: str) -> str:
        """A printed label without the '(2)' rank _section_labels adds to a repeated title.

        The rank tells two SIBLING paragraphs of one title apart, so it IS part of the
        printed name. It is not part of the title: a caller that types the title alone
        names EVERY paragraph that carries it, and a refusal is then the only honest
        answer. Matching the rank as if it were part of the title made the bare name miss
        the ranked sibling -- so a title held twice silently resolved to the first one,
        which is how the wrong paragraph gets read, and then edited.
        """
        return re.sub(r"\s*\(\d+\)$", "", label)

    @classmethod
    def _section_chains(
        cls, layout: List[dict], labels: Optional[List[str]] = None
    ) -> List[List[str]]:
        """The heading chain of every layout entry, outermost heading first.

        Aligned index by index with the layout; an empty list for the preamble, which
        is not a child of the document but its own text.
        """
        if labels is None:
            labels = cls._section_labels(layout)
        chains: List[List[str]] = []
        stack: List[Tuple[int, int]] = []
        for index, item in enumerate(layout):
            level = int(item["level"])
            if level <= 0:
                chains.append([])
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            held = list(chains[stack[-1][1]]) if stack else []
            chains.append(held + [labels[index]])
            stack.append((level, index))
        return chains

    @classmethod
    def _document_children(cls, text: str) -> List[Dict[str, Any]]:
        """The COMPLETE paragraph tree of a document: short titles, spans, sizes.

        A child is the heading TITLE it was printed under, never its slug: the stored
        identifier of a paragraph is a fold of its whole heading path
        ('#1-3-2-dev-unreleased-new-features'), which no caller can guess, and the
        hierarchy is carried by the nesting rather than by repeating the path.
        """
        layout = markdown_layout(text)
        starts = cls._line_starts(text)
        labels = cls._section_labels(layout)
        roots: List[Dict[str, Any]] = []
        stack: List[Tuple[int, Dict[str, Any]]] = []
        for index, item in enumerate(layout):
            level = int(item["level"])
            if level <= 0:
                # The preamble is the document's OWN text, not a child of it: it has no
                # heading to be named by, and nothing can be inserted after a leaf.
                continue
            node = {
                "name": labels[index],
                "lines": cls._section_lines(item, starts),
                "chars": int(item["section_end"]) - int(item["section_start"]),
            }
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1].setdefault("children", []).append(node)
            else:
                roots.append(node)
            stack.append((level, node))
        return roots

    # --- resolution by name ---------------------------------------------------
    # The surface PRINTS a name; the caller types that name back. The stored id of a
    # paragraph is a slug of its whole heading path, and the stored id of an overload
    # carries a SHA-1 of its declaration: neither is guessable, and a caller that tried
    # the natural spelling was told the symbol "no longer exists" -- a false cause, since
    # the file declared it twice. Resolution by name is what makes the printed path an
    # address, and a name that several blocks share is refused BY NAME, never guessed.

    @staticmethod
    def _fold_name(text: str) -> str:
        """A printed name folded to what a caller can retype.

        Case, spaces, accents and punctuation are folded away, so the name that was
        copied by hand resolves: 'New Features', 'new-features' and 'New  Features'
        are one paragraph, and the slug the index stores is not the printed name.
        """
        decomposed = unicodedata.normalize("NFKD", text or "")
        ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
        return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()

    @classmethod
    def _folded_chain(cls, parts: List[str]) -> str:
        return " ".join(cls._fold_name(part) for part in parts).strip()

    @staticmethod
    def _split_parameter_list(name: str) -> Tuple[str, Optional[str]]:
        """('Read.AsCsvSync', 'CsvReadOptions, CancellationToken') for a printed name."""
        text = (name or "").strip()
        if not text.endswith(")"):
            return text, None
        depth = 0
        for index in range(len(text) - 1, -1, -1):
            character = text[index]
            if character == ")":
                depth += 1
            elif character == "(":
                depth -= 1
                if depth == 0:
                    return text[:index].strip(), text[index + 1 : -1].strip()
        return text, None

    @staticmethod
    def _cut_parameter_default(item: str) -> str:
        depth = 0
        for index, character in enumerate(item):
            if character in "(<[{":
                depth += 1
            elif character in ")>]}":
                depth -= 1
            elif character == "=" and depth == 0:
                return item[:index].strip()
        return item.strip()

    @classmethod
    def _parameter_types(cls, signature: str) -> Optional[str]:
        """The parameter TYPES of a declaration: the discriminator a caller can retype.

        The stored discriminator of an overload is 8 hex characters of SHA-1, which the
        caller can neither read nor judge. A C# developer names a signature by its
        parameters, so that is what the surface prints and what resolution accepts.
        """
        text = " ".join((signature or "").split())
        start = text.find("(")
        if start < 0:
            return None
        depth = 0
        end = -1
        for index in range(start, len(text)):
            character = text[index]
            if character in "(<[{":
                depth += 1
            elif character in ")>]}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end < 0:
            return None
        parts: List[str] = []
        current = ""
        depth = 0
        for character in text[start + 1 : end]:
            if character in "(<[{":
                depth += 1
            elif character in ")>]}":
                depth -= 1
            if character == "," and depth == 0:
                parts.append(current)
                current = ""
            else:
                current += character
        parts.append(current)
        types: List[str] = []
        for part in parts:
            item = cls._cut_parameter_default(part)
            if not item:
                continue
            if ":" in item:
                # Python annotates AFTER the name (`x: int`); C# types come first.
                head, _, tail = item.partition(":")
                if head.strip() and " " not in head.strip() and tail.strip():
                    item = tail.strip()
            item = re.sub(r"^(ref|out|in|params|this|scoped)\s+", "", item)
            named = re.search(r"[A-Za-z_@][A-Za-z0-9_@]*\s*$", item)
            if named and named.start() > 0:
                without_name = item[: named.start()].strip()
                if without_name:
                    item = without_name
            types.append(item)
        return ", ".join(types)

    def _resolve_displayed_path(self, path: str) -> Optional[str]:
        """The stored id a printed path names, or None when it names none or several.

        A stored id is tried FIRST and left alone: resolution by name is the door for
        the paths the surface itself prints, not a filter in front of every lookup.
        """
        if self.db.get_symbol(path) is not None:
            return None
        resolution = self._resolution_of(path)
        return resolution.get("canonical") if resolution else None

    def _resolve_workspace_member(self, segments: List[str]) -> Optional[Dict[str, Any]]:
        """Resolve a member address starting with '::' across all workspace files."""
        matches: List[Dict[str, Any]] = []
        target_name = segments[-1].split("(")[0].strip()
        candidate_rows = self.db.search_symbols(query=target_name, limit=20)
        seen_files = set()
        for cand in candidate_rows:
            fp = cand.get("file_path")
            if not fp or fp in seen_files:
                continue
            seen_files.add(fp)
            res = self._resolve_symbol_names(fp, segments)
            if res and res.get("canonical"):
                matches.append(res)
        documents = self.db.list_documents(limit=50)
        for doc in documents:
            fp = doc.get("file_path") or str(doc.get("id"))
            if not fp or not self._is_markdown(fp) or fp in seen_files:
                continue
            seen_files.add(fp)
            res = self._resolve_section_names(fp, segments)
            if res and res.get("canonical"):
                matches.append(res)
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolution_of(self, path: str) -> Optional[Dict[str, Any]]:
        """What a printed path designates: one block, several candidates, or nothing.

        None when the path is not a member path at all (no '::'), or when its file part
        is not a file this index parses. Otherwise: the file part, every candidate the
        name matches, and `canonical` when exactly one of them is meant.
        """
        if "::" not in path:
            return None
        file_part, _, rest = path.partition("::")
        segments = [segment.strip() for segment in rest.split("::") if segment.strip()]
        if not segments:
            return None
        if not file_part:
            return self._resolve_workspace_member(segments)
        absolute = self._existing_path(file_part)
        if absolute is None or not absolute.is_file():
            return None
        if self._is_markdown(file_part):
            return self._resolve_section_names(file_part, segments)
        return self._resolve_symbol_names(file_part, segments)

    def _resolve_section_names(self, document_id: str, segments: List[str]) -> Dict[str, Any]:
        """A paragraph named by its heading chain, as the surface prints it."""
        _abs_path, text, _newline, error = self._resolve_markdown(document_id)
        if error:
            return {"file_part": document_id, "candidates": []}
        layout = markdown_layout(text)
        identifiers = self._layout_ids(document_id, layout)
        labels = self._section_labels(layout)
        chains = self._section_chains(layout, labels)
        starts = self._line_starts(text)
        # The printed name of a paragraph never carries its NUMBER: the tools assign it, so it is
        # structure, not part of the name -- "1. Alpha" prints as "Alpha". A caller that copies the
        # heading off the "## " line it just read is naming the same paragraph, so the number is
        # stripped from BOTH sides before matching. The reply then says which spelling is canonical.
        wanted = self._folded_chain([section_title(segment) for segment in segments])
        # The chain may be written with or without the outermost heading: the stored
        # slug path starts BELOW the top-level heading, and a caller that copies the
        # printed tree writes it from the top. Both are the same paragraph.
        matches = [
            index for index, chain in enumerate(chains)
            if chain and (self._folded_chain(chain) == wanted
                          or (len(chain) > 1 and self._folded_chain(chain[1:]) == wanted))
        ]
        if not matches and len(segments) == 1:
            # A short name alone is accepted when the FILE holds it once. When the file
            # holds it five times (CHANGELOG.md declares five 'New Features'), the
            # refusal NAMES them instead of picking one of them. A name written WITH its
            # printed rank ('New Features (2)') designates that one; a bare title
            # designates every paragraph that carries it, ranked or not.
            ranked = bool(re.search(r"\(\d+\)\s*$", segments[0]))
            matches = [
                index for index, chain in enumerate(chains)
                if chain
                and self._fold_name(chain[-1] if ranked else self._bare_title(chain[-1]))
                == wanted
            ]
        candidates = [
            self._section_candidate(document_id, layout, index, chains[index], starts, labels)
            for index in matches
        ]
        resolution: Dict[str, Any] = {"file_part": document_id, "candidates": candidates}
        if len(candidates) == 1:
            resolution["canonical"] = identifiers[matches[0]]
        elif not candidates:
            # The name is not in the file. Name the paragraphs that ARE close, so a caller that got
            # one word wrong is not sent to re-list a document to find the one it meant. Bounded: a
            # full listing is the read_block this refusal exists to avoid.
            resolution["near"] = self._near_sections(
                document_id, layout, chains, starts, labels, section_title(segments[-1])
            ) or self._nearest_paragraphs(document_id, layout, chains, starts, labels, segments)
        return resolution

    def _near_sections(
        self, document_id: str, layout: List[dict], chains: List[List[str]],
        starts: List[int], labels: List[str], asked: str, limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """The paragraphs of a file whose title looks like the one asked for, or none."""
        wanted = self._fold_name(asked)
        if not wanted:
            return []
        near: List[Dict[str, Any]] = []
        for index, chain in enumerate(chains):
            if not chain:
                continue
            title = self._fold_name(self._bare_title(chain[-1]))
            if not title:
                continue
            if title.startswith(wanted) or wanted.startswith(title):
                near.append(self._section_candidate(document_id, layout, index, chain, starts, labels))
            if len(near) >= limit:
                break
        return near

    def _nearest_paragraphs(
        self, document_id: str, layout: List[dict], chains: List[List[str]],
        starts: List[int], labels: List[str], segments: List[str], limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """The paragraphs nearest the line the asked name matched, when no title looks like it.

        `_near_sections` matches by PREFIX, so a title that CONTAINS the asked name rather than
        starting with it matches nothing and the refusal falls back to the flat "read the file".
        Measured on scalpel_16k_round004_ordre0 t11, on the duplicated leaf segment
        "Documentation Must Never Lead Implementation (+1)": the ONE candidate the prefix rule
        found was luck, and the same shape without the rank suffix reaches none. Position, not
        name, is the fallback: what surrounds the paragraph the caller aimed at is what it can
        retype.
        """
        wanted = self._fold_name(section_title(segments[-1]))
        matched = [
            index for index, chain in enumerate(chains)
            if chain and self._fold_name(self._bare_title(chain[-1])) == wanted
        ]
        anchor = (
            self._line_of(starts, int(layout[matched[0]]["section_start"]))
            if matched and matched[0] < len(layout) else None
        )
        ranked: List[Tuple[int, int, int]] = []
        for index, item in enumerate(layout):
            if int(item["level"]) <= 0 or index >= len(chains) or not chains[index]:
                continue
            line = self._line_of(starts, int(item["section_start"]))
            ranked.append((0 if anchor is None else abs(line - anchor), line, index))
        ranked.sort()
        return [
            self._section_candidate(document_id, layout, index, chains[index], starts, labels)
            for _distance, _line, index in ranked[:limit]
        ]

    def _resolve_symbol_names(self, file_part: str, segments: List[str]) -> Dict[str, Any]:
        """A symbol named by its printed ancestry: `Classe::Methode`, or its short name."""
        rows = self._symbol_rows(file_part)
        names = [self._split_parameter_list(segment)[0] for segment in segments]
        parameters = self._split_parameter_list(segments[-1])[1]
        chains = self._symbol_chains(rows)
        labels = self._symbol_labels(rows)
        id_chains = self._symbol_id_chains(rows)
        ranked: Dict[str, List[str]] = {
            row["id"]: [labels[identifier] for identifier in id_chains[row["id"]]] for row in rows
        }
        # The PRINTED path first, verbatim: it carries the parameter list and, when two
        # declarations agree on that too, a rank. A path the surface prints must resolve.
        printed = self._folded_chain(segments)
        matches = [
            row for row in rows
            if len(ranked[row["id"]]) >= len(segments)
            and self._folded_chain(ranked[row["id"]][-len(segments):]) == printed
        ]
        if not matches:
            # Then the name alone, which is what a caller writes from memory: unique in the
            # file it resolves, shared it is refused with the ranked candidates named.
            wanted = self._folded_chain(names)
            matches = [
                row for row in rows
                if len(chains[row["id"]]) >= len(names)
                and self._folded_chain(chains[row["id"]][-len(names):]) == wanted
            ]
        if parameters is not None:
            # The printed parameter list is the overload discriminator, and `()` is a list:
            # `First()` written by the caller used to be treated as "no list given" (an empty
            # string is falsy), so the zero-parameter overload stayed ambiguous with its
            # sibling even though the surface prints `First()` for it. When the list names
            # none of the matches, the name alone decided and the refusal says so.
            typed = [
                row for row in matches
                if self._fold_name(self._parameter_types(row.get("signature") or "") or "")
                == self._fold_name(parameters)
            ]
            matches = typed or matches
        candidates = [
            self._symbol_candidate(file_part, row, ranked[row["id"]]) for row in matches
        ]
        resolution: Dict[str, Any] = {"file_part": file_part, "candidates": candidates}
        if len(candidates) == 1:
            resolution["canonical"] = matches[0]["id"]
        return resolution

    @classmethod
    def _symbol_candidate(cls, file_part: str, row: Dict[str, Any], chain: List[str]) -> Dict[str, Any]:
        """One candidate of an ambiguous name: printed name, complete path, line span.

        `chain` is the RANKED ancestry built by `_symbol_labels`: it already carries the
        parameter list, and the rank when two declarations agree on that too. The candidate
        named here is therefore an address a caller can actually pass back, which the old
        version was not: it printed two identical paths and advised picking one of them.
        """
        label = str(chain[-1]) if chain else str(row.get("name") or "")
        return {
            "name": label,
            "path": file_part + "::" + "::".join(chain),
            "lines": [int(row.get("start_line") or 0), int(row.get("end_line") or 0)],
        }

    @classmethod
    def _section_candidate(
        cls, document_id: str, layout: List[dict], index: int, chain: List[str],
        starts: List[int], labels: List[str],
    ) -> Dict[str, Any]:
        """One candidate of an ambiguous paragraph name: printed name, path, line span."""
        item = layout[index]
        return {
            "name": labels[index],
            "path": document_id + "::" + "::".join(chain),
            "lines": cls._section_lines(item, starts),
        }

    # Device names Windows answers for even though no such file exists.
    RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
        prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10)
    }

    def _degenerate_id_refusal(self, block_id: str) -> Optional[str]:
        """Why an id that names no block is refused BEFORE any path is touched.

        "", "." and "..." all resolve to a real directory, so the tools answered for the
        workspace root under a name that designates nothing, and 'src/a.py.' answered for
        'src/a.py' because the file system strips a trailing dot. An ALIAS is worse than a
        refusal: the caller believes it read the block it named.
        """
        if block_id == "":
            return "it is empty"
        if block_id != block_id.strip():
            return "white space around a block id is part of no name"
        # '.' is deliberately NOT refused: the workspace root is a block, and a canonical
        # id for it is what gives a caller a route to the top-level layout. Refusing every
        # spelling of the root left it unreachable (found by the happy-path verifier).
        leaf = block_id.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if (leaf.endswith(".") or leaf.endswith(" ")) and leaf not in (".", ".."):
            # '..' is a parent segment, not a trailing dot: refusing it for the wrong
            # reason was itself a false cause (found by the adversarial verifier).
            return "a trailing dot or space is stripped by the file system, so it names no block"
        if leaf.split(".")[0].upper() in self.RESERVED_NAMES:
            return "it is a device name, not a file"
        if Path(block_id).drive and not Path(block_id).is_absolute():
            return "a drive-relative path designates no block inside the workspace"
        return None

    def _outside_the_workspace(self, block_id: str) -> bool:
        try:
            self.config.to_absolute_path(block_id)
            return False
        except ValueError:
            return True

    def _write_needs_a_block(self, block_id: str, absolute: Path) -> Dict[str, Any]:
        """A WRITE that names a whole file or directory: refuse it, and name the blocks it holds.

        `_unresolved_block` is the shared refusal of the READ path, and for a plain path it answers
        with STRUCTURE -- a listing, a summary, an outline -- carrying `status: success`. A write
        landing there was therefore told it had succeeded while its operations were dropped. The
        agent declared it as a "silent no-op" on SparkConnectOptions.cs in run 1 of base 30d62b4
        (mapab2/a), after attempting the same edit three times and reading the file back four times
        to be sure: ~11 turns, 16% of that run cost. Structure answers a read; a write has to name
        a block, so say which ones exist -- the caller then repairs in the next call, as it did for
        the sibling file it edited by symbol.
        """
        names: List[str] = []
        what = "a whole file"
        if absolute.is_dir():
            what = "a DIRECTORY -- a block that HOLDS files"
            listing = self._directory_listing(block_id, absolute)
            names = [str(item.get("name", "")) for item in (listing.get("children") or [])]
        else:
            outline = self._file_outline(block_id, absolute)
            if outline is not None:
                what = "a whole code file"
                names = [str(item.get("name", "")) for item in (outline.get("children") or [])]
            elif self._structured_summary(block_id, absolute) is not None:
                what = "a project/config file, summarised rather than parsed"
        shown = ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")
        return {
            "status": "error",
            "id": block_id,
            "error": (
                "'" + block_id + "' is " + what + ", not a block inside one, so NOTHING was written: "
                "edit_block rewrites a symbol (T.cs::Classe::Methode) or a Markdown paragraph."
            ),
            "holds": shown,
            "actionable_hint": (
                "Name one of its blocks: " + (shown if shown else "(none listed)")
                + ". read_block('" + block_id + "') prints their addresses; insert_block(parent='...') "
                "adds a new one."
            ),
        }

    def _unresolved_block(
        self, block_id: str, for_write: bool = False, answer_format: Optional[str] = None
    ) -> Dict[str, Any]:
        """Refuse an id that designates no block, and say what the path DOES hold.

        The refusal must never name a cause the code did not check: "check the spelling"
        was returned for '..', for C:\\Windows, and for a path that leaves the workspace --
        none of which is a typo -- and a deleted file was still asserted to exist from a
        stale index row. Each case now says what it actually is.
        """
        payload: Dict[str, Any] = {
            "status": "error",
            "id": block_id,
            "error": "No symbol or Markdown section has this identifier.",
        }
        # Containment comes FIRST. It used to run after the degenerate-id check, so '..'
        # and '../..' were described as a trailing-dot problem -- a cause the code never
        # tested -- instead of as a path that leaves the workspace.
        if self._outside_the_workspace(block_id):
            payload["error"] = (
                "'" + block_id + "' resolves outside the workspace root and is refused: this "
                "index only ever answers for paths inside it."
            )
            payload["actionable_hint"] = (
                "The workspace root is '" + str(self.config.workspace_root) + "'; name a path "
                "inside it."
            )
            return payload
        degenerate = self._degenerate_id_refusal(block_id)
        if degenerate is not None:
            payload["error"] = "'" + block_id + "' designates no block: " + degenerate + "."
            return payload
        # A bare PATH is answered by STRUCTURE, never by raw content: a directory is
        # listed, a project/config file is summarised, a code file is outlined. Handing
        # the text back would send the model down the flat file-by-file reading it was
        # trained on -- the behaviour the index exists to replace. It is tried BEFORE the
        # '#'/'::' guard, because a real file may carry those in its name and nothing else
        # can name it.
        absolute = self._existing_path(block_id)
        if absolute is not None:
            canonical = self._relative_path(absolute)
            if for_write:
                # The structure below answers a READ. Returning it to a write reported
                # `status: success` and dropped the operations on the floor.
                return self._write_needs_a_block(canonical, absolute)
            # An ALIAS is answered under its CANONICAL id -- one rule, everywhere. The root
            # is no exception: '.' is the id read_block gives the workspace root, so an alias
            # of it ('docs/..') is answered as '.' rather than echoed under a name of its own.
            if canonical != block_id and canonical != block_id.replace("\\", "/"):
                # An ALIAS of a real path ('src/../docs/guide.md', a doubled separator, a
                # trailing slash): answer for the block it DESIGNATES instead of denying
                # that it exists.
                return self.read_block(canonical, answer_format=answer_format)
            # The three LISTINGS. They are the only answers the answer format touches:
            # the object tree, or the block's own address followed by its children.
            if absolute.is_dir():
                return self._listing_answer(
                    canonical, self._directory_listing(block_id, absolute), answer_format
                )
            summary = self._structured_summary(block_id, absolute)
            if summary is not None:
                return self._listing_answer(canonical, summary, answer_format)
            outline = self._file_outline(block_id, absolute)
            if outline is not None:
                return self._listing_answer(canonical, outline, answer_format)
            return self._unindexed_path_refusal(payload, block_id)
        if "#" in block_id or "::" in block_id:
            symbol = self.db.get_symbol(block_id) if "::" in block_id else None
            resolution = self._resolution_of(block_id) if symbol is None else None
            candidates = (resolution or {}).get("candidates") or []
            file_part = (resolution or {}).get("file_part") or block_id.split("::", 1)[0]
            if len(candidates) > 1:
                # The block EXISTS, several times: "it no longer declares a symbol with that
                # id" was a FALSE cause -- measured on Read.Csv.cs, where ::Read.AsCsvSync
                # names two live overloads of the same file. The refusal names them, with the
                # only discriminator the caller can retype (its parameters, and its lines).
                payload["error"] = (
                    "'" + block_id + "' names " + str(len(candidates)) + " blocks of '"
                    + file_part + "', so it designates none of them: two blocks of one file "
                    "may carry the same name, and the name alone is then not an address. "
                    "Nothing was read."
                )
                payload["candidates"] = candidates
                payload["actionable_hint"] = (
                    "Every path below is complete: call read_block with the one you want. "
                    "What tells them apart is in the printed name -- the parameter list "
                    "of an overload, or the (2), (3) rank of a repeated heading."
                )
            elif symbol is None and self._existing_path(file_part) is not None:
                if self._is_markdown(file_part):
                    payload["error"] = (
                        "'" + block_id + "' is not in the index: the file exists, but it does "
                        "not hold a paragraph with that name."
                    )
                    near = (resolution or {}).get("near") or []
                    if near:
                        # Give the RIGHT one instead of sending the caller back to list the file.
                        payload["candidates"] = near
                        payload["actionable_hint"] = (
                            "The paragraphs whose name is closest are in 'candidates' (path, name, "
                            "lines): call read_block with the path of the one you meant, or "
                            "read_block('" + file_part + "') for the complete list."
                        )
                    else:
                        payload["actionable_hint"] = (
                            "read_block('" + file_part + "') lists the paragraphs the file holds now."
                        )
                else:
                    payload["error"] = (
                        "'" + block_id + "' is not in the index: the file exists, but it no "
                        "longer declares a symbol with that id."
                    )
                    # A DEAD ID IS NOT A TYPO: the file exists, and what the caller needs is the
                    # address of the block that stands where the one it named used to. Name those
                    # addresses rather than prescribing a call that would only print them.
                    # Measured on scalpel_16k_round005_ordre0 t22 and scalpel_16k_round004_ordre0
                    # t37: 5 calls and 4 turns on the PROJECT-file shape alone, before the agent
                    # left the tool for search_documents and search_symbols.
                    absolute = self._existing_path(file_part)
                    summary = self._structured_summary(file_part, absolute) if absolute else None
                    if summary is not None and not self._symbol_rows(file_part):
                        # A structured file never declared a symbol: asserting one that
                        # "disappeared" would be a cause the code never checked.
                        return self._summary_key_refusal(file_part, summary)
                    near = self._near_addresses(file_part, block_id, absolute) if absolute else []
                    if near:
                        payload["candidates"] = near
                        payload["actionable_hint"] = (
                            "The blocks of '" + file_part + "' that are nearest to the name you "
                            "sent are in 'candidates' (path, name, lines): call read_block with the "
                            "path of the one you meant, or read_block('" + file_part + "') for the "
                            "complete list."
                        )
                    else:
                        payload["actionable_hint"] = (
                            "read_block('" + file_part + "') lists the symbols the file holds now."
                        )
            if symbol is not None:
                payload["error"] = (
                    "stale symbol: '" + block_id + "' is in the index, but the file changed "
                    "outside the tools and the stored offsets no longer match. The content is "
                    "NOT returned -- a shifted slice would be worse than a refusal."
                )
                payload["actionable_hint"] = (
                    "read_block('" + symbol.file_path + "') re-reads the file's current symbols."
                )
            return payload
        return self._unindexed_path_refusal(payload, block_id)

    def _near_symbol_addresses(self, file_part: str, asked: str, limit: int = 5) -> List[Dict[str, Any]]:
        """The nearest OTHER symbols of a code file: full paths, ranked by ancestry.

        A dead symbol id is not a typo and not a version drift: the file simply does not declare
        that name any more, and "read_block(file) lists the symbols the file holds now" is a
        whole extra call before the caller can even choose. Measured on
        scalpel_16k_round005_ordre0 t22: a :: address on a PROJECT file (no parser declares a
        symbol in it at all) cost five calls and four turns before the agent gave up on the tool
        and went to search_documents then search_symbols. The addresses are printed with
        `symbol_addresses` -- the printer search_symbols and read_block share -- so what is named
        here is what resolution accepts.
        """
        rows = self._symbol_rows(file_part)
        if not rows:
            return []
        chains = self._symbol_chains(rows)
        labels = self._symbol_labels(rows)
        printed = self.symbol_addresses(file_part)
        wanted = self._fold_name(asked)
        similar: List[Dict[str, Any]] = []
        for row in rows:
            identifier = str(row["id"])
            address = printed.get(identifier)
            if not address:
                continue
            names = [self._fold_name(str(row.get("name") or ""))]
            names += [self._fold_name(str(step)) for step in chains.get(identifier, [])]
            if any(name and wanted and (name.startswith(wanted) or wanted.startswith(name))
                   for name in names):
                similar.append({
                    "name": labels.get(identifier, str(row.get("name") or "")),
                    "path": address,
                    "lines": [int(row.get("start_line") or 0), int(row.get("end_line") or 0)],
                })
            if len(similar) >= limit:
                break
        if similar:
            return similar[:limit]
        containers = {
            "namespace", "class", "interface", "struct", "record", "enum", "module", "trait",
            "mapping", "sequence", "object", "array", "property",
        }
        roots = [
            row for row in rows
            if str(row.get("kind") or "") in containers
            and not row.get("parent_id")
            and str(row["id"]) in printed
        ]
        return [
            {
                "name": labels.get(str(row["id"]), str(row.get("name") or "")),
                "path": printed[str(row["id"])],
                "lines": [int(row.get("start_line") or 0), int(row.get("end_line") or 0)],
            }
            for row in roots[:limit]
        ]
    #: The declaration names of a structured file, in the order a reader wants them.
    SUMMARY_KEY_ORDER = (
        "target_frameworks", "package_references", "project_references", "top_level_keys",
        "nested_keys", "declarations",
    )

    def _near_addresses(self, file_part: str, block_id: str, absolute: Optional[Path]) -> List[Dict[str, Any]]:
        """The nearest addresses the SAME file offers, or [] when it offers none.

        It chooses the FIRST of the file kinds that can name an address at all: a parsed file
        names symbols, a document names paragraphs, a project/config file names the declarations
        its summary reads -- and a file of none of those kinds answers [] rather than inventing
        addresses (the refusal names its content instead).
        """
        if self._is_markdown(file_part):
            _abs_path, text, _newline, error = self._resolve_markdown(file_part)
            if error:
                return []
            layout = markdown_layout(text)
            if not layout:
                return []
            labels = self._section_labels(layout)
            chains = self._section_chains(layout, labels)
            starts = self._line_starts(text)
            candidates: List[Dict[str, Any]] = []
            for index, item in enumerate(layout):
                if int(item["level"]) <= 0 or index >= len(chains):
                    continue
                candidates.append({
                    "name": labels[index],
                    "path": file_part + "::" + "::".join(chains[index]),
                    "lines": self._section_lines(item, starts),
                })
            return self._nearest_by_lines(candidates, self._asked_line(block_id, candidates))
        if absolute is not None and absolute.suffix.lower() in (".csproj", ".props", ".targets", ".xml"):
            summary = self._structured_summary(block_id, absolute)
            if summary is None:
                return []
            return [
                {"name": key, "path": file_part + "::" + key, "lines": []}
                for key in self.SUMMARY_KEY_ORDER
                if key in summary
            ]
        return self._near_symbol_addresses(file_part, self._last_segment(block_id))

    @staticmethod
    def _last_segment(block_id: str) -> str:
        """The last :: segment of a printed path, without a rank suffix."""
        leaf = block_id.split("::")[-1].strip()
        if leaf.endswith(")") and "(" in leaf:
            head, _, rank = leaf.rpartition("(")
            if rank[:-1].isdigit():
                leaf = head.rstrip()
        return leaf

    @classmethod
    def _asked_line(cls, block_id: str, candidates: List[Dict[str, Any]]) -> Optional[int]:
        """The line of the candidate whose name IS the asked segment, or None.

        It is what makes "the nearest addresses" a PROXIMITY rule rather than a truncation: the
        paragraphs surrounding the one the caller aimed at are the ones it can retype.
        """
        wanted = cls._fold_name(cls._last_segment(block_id))
        for item in candidates:
            if cls._fold_name(str(item.get("name") or "")) == wanted:
                lines = item.get("lines") or []
                if lines:
                    return int(lines[0])
        return None

    @staticmethod
    def _nearest_by_lines(candidates: List[Dict[str, Any]], wanted: Optional[int],
                          limit: int = 5) -> List[Dict[str, Any]]:
        """`limit` candidates, nearest to `wanted` when it is known, in order otherwise."""
        if wanted is None:
            return candidates[:limit]
        def score(item: Dict[str, Any]) -> Tuple[int, int]:
            lines = item.get("lines") or []
            start = int(lines[0]) if lines else 0
            return (abs(start - wanted), start)
        return sorted(candidates, key=score)[:limit]

    @staticmethod
    def _summary_key_refusal(file_part: str, summary: Dict[str, Any]) -> Dict[str, Any]:
        """A STRUCTURED file (project/config) declares no symbols: name what it holds instead.

        "the file exists, but it no longer declares a symbol with that id" is FALSE about a
        .csproj: no symbol was ever declared there, and the route it prescribes (read the file
        again) cannot answer the question either -- measured on scalpel_16k_round005_ordre0 t22
        and scalpel_16k_round004_ordre0 t37, where the agent left the tool for search_documents
        and search_symbols. What the file DOES offer is its summary, and that IS readable.
        """
        keys: List[str] = []
        for key, value in summary.items():
            if key in ("status", "id"):
                continue
            if isinstance(value, dict):
                keys.append(str(key) + " (" + ", ".join(list(value)[:5]) + ")")
            else:
                keys.append(str(key))
        listed = ", ".join(keys[:8]) if keys else "(nothing it can name)"
        return {
            "status": "error",
            "id": file_part,
            "error": (
                "'" + file_part + "' is not a code file: no parser declares symbols in it and no "
                "heading names a paragraph, so nothing here is addressed with ::. What it holds "
                "is its STRUCTURE, not symbols."
            ),
            "holds": listed,
            "actionable_hint": (
                "No :: address exists in this file. read_block('" + file_part + "') answers with "
                "its structure -- " + listed + " -- which is the whole content a project or config "
                "file has on this surface."
            ),
        }
    #: Ceiling for the raw text of a file no parser claims: above it the caller is told the
    #: size instead of being handed a payload the harness would truncate anyway.
    RAW_FILE_MAX_BYTES = 512 * 1024

    def _raw_text_payload(
        self, normalized: str, block_id: str, allow_supported: bool = False
    ) -> Optional[Dict[str, Any]]:
        """The TEXT of a file no parser and no document reader claims, numbered, READ-ONLY.

        A file nobody claims used to be a dead end: the refusal said "this extension is neither"
        and pointed at search_documents, so an agent that needed the CONTENT of a .ps1 -- named
        in the task's own documents -- spent its next two turns hunting for a route that does
        not exist. The baseline arm has read_file; this arm had nothing (measured on three
        consecutive runs of the base dee367d, ~3 turns each).

        It is scoped to what the index has NOTHING to say about. A parsed file keeps its
        structure: reading a whole source file stays what it has always been here -- a listing,
        never a dump. READ-ONLY is stated, never implied: nothing inside such a file is a block,
        so the write tools refuse the very path this answers for.
        """
        indexer = getattr(self, "indexer", None)
        supported = getattr(indexer, "_is_supported", None)
        if not allow_supported and supported is not None and supported(normalized):
            return None
        try:
            absolute = self.config.to_absolute_path(normalized)
        except ValueError:
            return None
        if not absolute.is_file():
            return None
        try:
            raw = absolute.read_bytes()
        except OSError:
            return None
        if len(raw) > self.RAW_FILE_MAX_BYTES:
            return {
                "status": "error",
                "id": block_id,
                "error": (
                    "'" + block_id + "' weighs " + str(len(raw)) + " bytes and no parser claims it: "
                    "the raw text is returned below " + str(self.RAW_FILE_MAX_BYTES) + " bytes only, "
                    "and a truncated answer would be worse than this refusal."
                ),
                "actionable_hint": "search_documents finds the paragraphs that describe it.",
            }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "status": "error",
                "id": block_id,
                "error": (
                    "'" + block_id + "' is not UTF-8 text: no parser claims it and its bytes "
                    "are not readable as text, so the path designates no block."
                ),
                "actionable_hint": "search_documents finds the paragraphs that describe it.",
            }
        return {
            "status": "success",
            "id": block_id,
            "kind": "file_text",
            "file_path": normalized,
            "line_count": len(text.splitlines()),
            "bytes": len(raw),
            "content_hash": compute_content_hash(raw),
            "numbered_text": numbered(text, 1),
            "read_only": True,
            "actionable_hint": (
                "No parser claims this file, so its whole text is returned numbered from 1 -- and "
                "it is READ-ONLY: nothing inside it is a block, so edit_block refuses this path. "
                "Report a change here instead of attempting one."
            ),
        }

    @staticmethod
    def _is_using_directive(code: str) -> bool:
        """Is the inserted text a using DIRECTIVE (not a `using` statement)?

        A directive must sit above every other declaration of the file; a statement (`using var
        x = ...`, `using (...)`) belongs inside a body. Only the directive has a legal place
        computed here.
        """
        for line in code.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            if stripped.startswith("global "):
                stripped = stripped[len("global "):].lstrip()
            return (
                stripped.startswith("using ")
                and stripped.endswith(";")
                and not stripped.startswith("using var ")
                and not stripped.startswith("using (")
            )
        return False

    def _unindexed_path_refusal(self, payload: Dict[str, Any], block_id: str) -> Dict[str, Any]:
        """Why a bare path designates no block: absent from disk, or simply never parsed.

        An agent asked for `scripts/databricks/deploy_and_run_databricks.ps1`, got the bare
        "No symbol or Markdown section has this identifier", and reported that it "did not
        indicate that the file is simply unindexed (as opposed to non-existent)" -- working
        around it by reading the documents that describe the script (mapab2 b r2 of the base
        957f704). The two cases lead to different next steps: a typo is worth retrying, a file
        Scalpel never parses is worth another route entirely.
        """
        try:
            exists = self.config.to_absolute_path(block_id).exists()
        except ValueError:
            exists = False
        try:
            normalized = self.config.to_relative_posix(self.config.to_absolute_path(block_id))
        except ValueError:
            normalized = block_id.replace("\\", "/")
        indexed = {path.replace("\\", "/") for path in self.db.get_indexed_paths()}
        if not exists:
            if normalized in indexed:
                # The index still remembers a file that is gone: the old message asserted
                # it was there ("This one holds: class M"), which is a false cause.
                payload["error"] = (
                    "'" + block_id + "' is in the index but no longer on disk, so it "
                    "designates no block: the index is behind the file system."
                )
                payload["actionable_hint"] = (
                    "A re-index drops the entry; until then only files that exist can be read."
                )
                return payload
            if "#" not in block_id and "::" not in block_id and "/" not in block_id:
                # A bare NAME designates no block, but it may NAME one: answering "check the
                # spelling" about a block that exists one `::` away sends the caller hunting
                # for a typo that is not there.
                named = self._blocks_named(block_id)
                if named:
                    paths = sorted(set(named))
                    listed = ", ".join(paths[:4])
                    if len(paths) > 4:
                        listed += " (and " + str(len(paths) - 4) + " more)"
                    payload["error"] = (
                        "'" + block_id + "' is a NAME, not a path: " + str(len(named))
                        + " block(s) in this workspace carry it."
                    )
                    payload["actionable_hint"] = "Pass the whole path: " + listed + "."
                    return payload
            payload["error"] = (
                "No symbol or Markdown section has this identifier, and no file on disk has "
                "this path either: check the spelling."
            )
            return payload
        indexer = getattr(self, "indexer", None)
        pattern = indexer.ignore_pattern_for(normalized) if indexer is not None else None
        if pattern is not None:
            # A file inside an ignored tree is not unindexed because of its TYPE: it was
            # hidden on purpose. "This extension is neither" sent the caller to change the
            # path or the route instead of the ignore list.
            payload["error"] = (
                "'" + block_id + "' matches the workspace ignore pattern '" + pattern + "', so "
                "Scalpel never indexes it: the path designates no block."
            )
            payload["actionable_hint"] = (
                "That is the ignore list, not the file's type: remove the pattern to index it."
            )
            return payload
        if normalized in indexed:
            if not self._symbol_rows(normalized):
                raw = self._raw_text_payload(normalized, block_id, allow_supported=True)
                if raw is not None:
                    return raw
            payload["error"] = (
                "'" + block_id + "' is indexed, but the parser recognised no container "
                "(class, interface, namespace, struct, record or enum) in it, so there is no "
                "whole file to read."
            )
            payload["actionable_hint"] = (
                "search_symbols(query='<a name you expect>', path='" + block_id + "') lists what "
                "it DOES hold -- a file whose declarations are all wrapped (an 'export' "
                "statement, say) can hold none -- and search_documents finds the paragraphs "
                "describing it."
            )
            return payload
        # A file NOBODY claims -- no parser, no document reader -- answers with its TEXT:
        # the refusal that used to stand here named the cause and left the caller with no
        # route at all (cf. _raw_text_payload).
        raw = self._raw_text_payload(
            normalized, block_id, allow_supported=not self._symbol_rows(normalized)
        )
        if raw is not None:
            return raw
        payload["error"] = (
            "'" + block_id + "' exists on disk but is not indexed: Scalpel parses code files "
            "and documents, and this extension is neither, so the path designates no block."
        )
        payload["actionable_hint"] = (
            "search_documents finds the paragraphs that describe it; this tool cannot return "
            "its content."
        )
        return payload

    @staticmethod
    def _summary_parse_refusal(block_id: str, suffix: str, detail: str) -> Dict[str, Any]:
        """A recognised project/config type that does not parse: say THAT, not "unsupported".

        "this extension is neither [code nor document]" was flatly false for a malformed
        .csproj or .json -- measured five times -- and sent the caller to change its route
        instead of fixing the content.
        """
        return {
            "status": "error", "id": block_id,
            "error": (
                "'" + block_id + "' is a " + suffix + " file Scalpel recognises, but it does "
                "not parse: " + detail[:200]
            ),
            "actionable_hint": (
                "The path is right and the content is not valid " + suffix + "; fix it and read "
                "it again."
            ),
        }

    def read_block(self, block_id: str, answer_format: Optional[str] = None) -> Dict[str, Any]:
        """One block, in one of the TWO forms the surface has: a LISTING or its TEXT.

        A PATH -- a directory, a code file, a .md file, a project/config file -- is a
        LISTING: the COMPLETE list of its children, each with its SHORT name inside its
        parent, its line span and its size (bytes for code, characters for a document),
        nested by indentation. There is no window and no page: everything the block holds
        is in the answer, and the block's own size is line_count plus bytes (or chars).
        A .md file answers BOTH ways at once, because its root block IS the text no
        heading owns: the complete list of its paragraphs, AND that text numbered with
        its hash.

        A SYMBOL or a PARAGRAPH is TEXT: its lines numbered from 1, and the hash that
        pins them, under the exact names the editing path uses -- line_count,
        numbered_text, content_hash. Those numbers are the coordinate system edit_block
        uses, and the hash is the version they refer to, so numbers taken from a stale
        read are refused instead of applied to the wrong lines.

        The path is written ONCE. A child is addressed by this same path followed by
        '::' and the name printed here -- or by '/' and the name, inside a directory --
        and the server folds case, spaces, accents and punctuation, so the printed name
        resolves when it is copied by hand.
        """
        # THE READ TAKES THE SAME LOCK THE EDIT TAKES, and only on ITS OWN file. A reader of a
        # file being edited waits: its read would be stale by construction, and it would pay a
        # turn for it (docs/CHANTIER_VERROU_EDITION.md section 4.1). A reader of ANOTHER file is
        # not delayed -- the lock is per file, which is the whole point of it. The lock is taken
        # BEFORE the metrics are read, so nothing can slip between the read and a write that
        # follows it in the same call.
        _read_target = self._file_of(block_id)
        if _read_target is not None:
            try:
                self._edits_lock.acquire(_read_target)
            except LockTimeout as busy:
                return {"status": "error", "block_id": block_id, "error": str(busy)}
            try:
                return self._read_block_locked(block_id, answer_format)
            finally:
                self._edits_lock.release(_read_target)
        return self._read_block_locked(block_id, answer_format)

    def _read_block_locked(self, block_id: str, answer_format: Optional[str] = None) -> Dict[str, Any]:
        """read_block, with the caller already alone in the file (see read_block)."""
        metrics = self.block_metrics(block_id)
        if metrics is None:
            return self._unresolved_block(block_id, answer_format=answer_format)
        kind = metrics["kind"]
        meta = metrics["meta"]
        content = metrics["content"]
        if kind == "document":
            # The document root: the file IS a block, and its own text is the text no
            # heading owns -- the preamble, or the whole file when it has no heading at
            # all. Its size is the FILE's, and its children are every paragraph, nested
            # by heading level, each named by its title. The file path therefore answers
            # BOTH ways at once: the complete list of its paragraphs, and the editing
            # buffer of the text no heading owns -- which is what the root block IS.
            _abs_path, text, _newline, error = self._resolve_markdown(meta["file_path"])
            payload: Dict[str, Any] = {
                "status": "success",
                "id": block_id,
                "line_count": len(text.splitlines()) if not error else metrics["line_count"],
                "chars": len(text) if not error else len(content),
                "content_hash": metrics["content_hash"],
                "numbered_text": metrics["numbered_text"],
            }
            if not error:
                payload["children"] = self._document_children(text)
            if not payload["numbered_text"]:
                # An empty numbered_text printed NEXT TO a full children tree reads as "this
                # block has no text", and the agent asked again before editing (anomaly 3 of the
                # run on base f831a4e: "the empty numbered_text field alongside a populated
                # children array is ambiguous"). Say what the emptiness IS.
                payload["root_text_empty"] = True
                payload["preamble_line_count"] = 0
                payload["actionable_hint"] = (
                    "This file's own text -- the preamble no heading owns -- is EMPTY (0 lines before first heading): every line of it "
                    "sits under a heading, and those paragraphs are the children below. Read the one "
                    "you need by its address, or pass several in read_block(paths=[...])."
                )
            else:
                payload["preamble_line_count"] = len(str(payload["numbered_text"]).splitlines())
            if answer_format == "text":
                return self._listing_answer(block_id, payload, answer_format)
            return payload
        # A SYMBOL or a PARAGRAPH: the editing buffer, and the keys edit_block and the
        # discovery views consume. line_count, numbered_text and content_hash are the
        # contract of the content path and are returned under these exact names.
        payload = {
            "status": "success",
            "id": block_id,
            "kind": kind,
            "file_path": meta["file_path"],
            "content_hash": metrics["content_hash"],
            "line_count": metrics["line_count"],
            "numbered_text": metrics["numbered_text"],
        }
        if kind == "symbol":
            payload["symbol_kind"] = meta["symbol_kind"]
            payload["bytes"] = len(content.encode("utf-8"))
        elif kind == "section":
            payload["heading"] = meta["heading"]
            payload["level"] = int(meta["level"])
            payload["chars"] = len(content)
        else:
            payload["bytes"] = len(content.encode("utf-8"))
        return payload

    @staticmethod
    def _empty_root_refusal(
        block_id: str,
        content: str,
        current_hash: str,
        expected_hash: Optional[str],
    ) -> Dict[str, Any]:
        """The refusal for a stale hash on a DOCUMENT whose own text is EMPTY.

        A file is a block too, and its own text is the part no heading owns: it is EMPTY by one
        of TWO routes, and the refusal must not assert one of them alone. A file of 0 bytes has
        no text at all ('_document_root_end' returns len(text), which is 0); a file whose first
        line is a heading has text, but every line of it is owned ('_document_root_end' returns
        that heading's offset, 0). An independent verification caught the first wording asserting
        the second cause about a 0-byte file -- a cause that does not exist there.

        The generic stale refusal then returned that empty buffer as "the FRESH block ...
        returned below" and prescribed replaying the operations "using the line numbers in
        numbered_text above" -- there are none, and the replay answers "op 0: replace range
        must be inside 1..0". Reported on the corpus (30/09/2026): 10 refusals returning the
        empty hash, 3 followed by a second identical refusal, 2 by that out-of-range error.

        The vocabulary is the one the two neighbouring paths already use, not a new one: the
        READ path answers 'root_text_empty' for this same block, and the WRITE path with a
        CORRECT hash says "it is a document block with 0 lines" and names 'insert_after 0'.
        Only the stale branch was mute, and its remedy did not exist.
        """
        return {
            "status": "error",
            "block_id": block_id,
            "error": (
                "Stale read, and this identifier is the FILE itself: '" + str(block_id) + "' is a "
                "document block with 0 lines -- this file's own text, the part no heading owns, is "
                "EMPTY: either the file itself is empty, or every line of it sits under a heading. "
                "There is nothing here to replay operations against, and the hash you sent does not "
                "open this block."
            ),
            "content_hash": current_hash,
            "expected_hash": str(expected_hash),
            "hash_differs_at": Patcher._first_difference(str(expected_hash or ""), current_hash),
            "line_count": len(content.splitlines()),
            "numbered_text": numbered(content, 1),
            "root_text_empty": True,
            "actionable_hint": (
                "read_block('" + str(block_id) + "') prints this file's child blocks, if it has "
                "any -- the paragraphs that hold its text -- each with its address and its own "
                "content_hash: edit the one you read, with THAT hash. To give the file a line of "
                "its own instead, call edit_block(path='" + str(block_id) + "', expected_hash='"
                + current_hash + "', ops=[{\"insert_after\": 0, \"content\": \"...\"}]) -- "
                "insert_after 0 is the one operation a 0-line block accepts, and the hash above is "
                "the one that opens this block."
            ),
        }

    def edit_block(
        self,
        block_id: Optional[str] = None,
        ops: Optional[List[Dict[str, Any]]] = None,
        expected_hash: Optional[str] = None,
        *,
        path: Optional[str] = None,
        edits: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Apply line operations to one block, then re-validate and rewrite it.

        The caller sends only the new content: the operations name the lines to
        replace, delete or insert after, using the 1-based numbers read_block
        returned. `expected_hash` is the version those numbers refer to.
        """
        if edits is not None:
            return self._edit_batch(edits)
        if block_id is None:
            block_id = path or ""
        if not ops:
            # The agents PROBE with an empty op list to fetch a hash, then read: two of
            # them declared the bare "ops is empty." as an anomaly (base 86872df, both
            # arms). Naming the next call costs nothing and saves the turn.
            return {
                "status": "error",
                "block_id": block_id,
                "error": "ops is empty: there is nothing to apply.",
                "actionable_hint": (
                    "read_block('" + block_id + "') returns the numbered lines AND the "
                    "content_hash edit_block expects; send those operations in the next call."
                ),
            }
        target = self._file_of(block_id)
        if target is not None:
            try:
                self._edits_lock.acquire(target)
            except LockTimeout as busy:
                return {
                    "status": "error",
                    "block_id": block_id,
                    "error": (
                        "Refused: " + str(busy) + " NOTHING was written, and the version stamp "
                        "you sent still opens its block. Send the same call again once that one "
                        "has finished."
                    ),
                }
            try:
                return self._edit_block_locked(block_id, ops, expected_hash)
            finally:
                self._edits_lock.release(target)
        return self._edit_block_locked(block_id, ops, expected_hash)

    def _edit_block_locked(
        self,
        block_id: str,
        ops: Optional[List[Dict[str, Any]]] = None,
        expected_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """edit_block, with the caller already alone in the file (see edit_block)."""
        resolved = self._block_and_kind(block_id)
        if resolved is None:
            abs_file = self._existing_path(block_id)
            if abs_file is not None and abs_file.is_file() and "::" not in block_id and "#" not in block_id:
                indexer = getattr(self, "indexer", None)
                supported = getattr(indexer, "_is_supported", None)
                is_supported = (supported is not None and supported(str(abs_file)))
                if is_supported:
                    rows = self._symbol_rows(block_id)
                    has_containers = any(r.get("kind") in ("function", "class", "method", "interface", "struct", "record", "enum") for r in rows)
                    if not has_containers and not self._is_markdown(block_id) and self._structured_summary(block_id, abs_file) is None:
                        raw = abs_file.read_bytes()
                        content = raw.decode("utf-8", errors="replace")
                        resolved = (
                            "file_text",
                            {"file_path": self._relative_path(abs_file)},
                            content,
                            abs_file,
                            raw,
                            (0, len(raw)),
                        )
            if resolved is None:
                return self._unresolved_block(block_id, for_write=True)
        kind, meta, content, _abs_path, _raw, _span = resolved
        # ONE resolver for the read AND the write: a path built from the names read_block
        # prints designates the same block here, and the write path below receives the
        # stored id it maps to. Without this the caller had to translate the path it had
        # just read into another spelling -- the friction the '::' grammar exists to
        # remove -- and the write answered "not found in graph database" about a block
        # that had just been read.
        target_id = self._resolve_displayed_path(block_id) or block_id
        current_hash = compute_content_hash(content.encode("utf-8"))
        # THE VERSION THE CALLER READ, taken ONCE, at the first resolution of this block in
        # this call: that is the last instant before a write of the same call can move it.
        # In a grouped call an earlier entry has already written this file, so `content` is
        # the CURRENT state and it is NOT what the stamp opens. Reading the caller stamp
        # against the CURRENT hash refuses work the tool can serve -- the staleness it caused
        # itself, which is exactly what this chantier removes. Outside a grouped call the
        # table is empty, so this IS the ordinary guard, byte for byte.
        written_id = self._written_id(block_id)
        caller_hash = self._call_block_hashes.get(str(block_id))
        if caller_hash is None:
            caller_hash = self._call_block_hashes.get(str(written_id))
        if caller_hash is None:
            # OUTSIDE A GROUPED CALL THE TABLE IS EMPTY, so the stamp is read against the
            # CURRENT hash -- the ordinary single-call guard, byte for byte what it always did.
            caller_hash = current_hash
        # THE VERSION THIS BLOCK HOLDS AS THE CALL FOUND IT, remembered for the entries that
        # come AFTER this one -- and recorded at the END of the call, never here. Recording it
        # at this point was measured to poison the very next single call: the table outlives
        # the call that filled it, so a second edit_block on the same block read the hash its
        # own first write had produced and answered "Stale read" about a stamp that opened
        # the block exactly (17 tests). The pair is captured here, where the resolution and
        # the hash both exist; it is STORED by _remember_stamps, on the way out.
        stamps = (str(block_id), str(target_id), current_hash)
        # THE ONE STALENESS LEFT. `caller_hash` is the version the CALLER read -- the one the
        # validation pass recorded before the first byte moved. An entry of a grouped call is
        # REBASED before it reaches here (Patcher._batch_entry), so its stamp opens the version
        # this call started from and not the current one; comparing it with `current_hash` would
        # refuse exactly the work the tool displaced itself. What survives is the one case nobody
        # can translate: a writer OUTSIDE the tool, which the lock cannot reach and whose moves
        # we never saw. The batch repair -- three comparisons and a test on the position of the
        # tail, a bandage on a refusal the tool no longer emits -- is DELETED, and no local of
        # it is left behind to suggest otherwise.
        if expected_hash and not self._hashes_match(expected_hash, caller_hash):
            # Two different failures used to share this message, and it asserted the wrong one.
            # Run 1 of base ea25ac6 (mapab2/a): the caller read a SECTION, then addressed the FILE
            # with that section's hash. "The block changed since you read it" was false -- nothing
            # had changed, these are two blocks -- and the hint prescribed an edit on the empty
            # root, which answers "replace range must be inside 1..0". 11 refusals, t21 to t52,
            # the first accepted edit at t63, the run cut at the ceiling. Name the block the hash
            # BELONGS to before blaming the caller's read for going stale.
            owner = self._child_carrying_hash(target_id, kind, str(expected_hash)) or self._owner_of_hash(
                str(expected_hash), target_id
            )
            if owner:
                return {
                    "status": "error",
                    "block_id": block_id,
                    "error": (
                        "Wrong block: that hash is the content_hash of '" + owner + "', not of '"
                        + str(block_id) + "'. Nothing changed here -- a hash opens the block it was "
                        "read from, and only that one."
                    ),
                    "expected_hash": str(expected_hash),
                    "expected_hash_belongs_to": owner,
                    "content_hash": current_hash,
                    "line_count": len(content.splitlines()),
                    "numbered_text": numbered(content, 1),
                    # Both intents are served, because a refusal cannot know which one the caller had:
                    # re-target to the block the hash opens, or stay here with THIS block own hash.
                    "actionable_hint": (
                        "The hash you sent still opens its own block: edit_block(path='" + owner
                        + "', expected_hash='" + str(expected_hash) + "', ops=[...]) with the line numbers "
                        "read_block printed for '" + owner + "'. To edit '" + str(block_id) + "' instead, "
                        "use ITS current hash '" + current_hash + "'"
                        + (" (its own text is empty: insert_after 0 adds the first line)."
                           if not content else ".")
                    ),
                }
            if kind == "document" and not content:
                # A FILE id whose own text is empty: a 0-byte file, or one whose first line is a
                # heading -- either way _document_root_end leaves 0 characters of text. The generic
                # refusal below would hand this empty buffer back as "the FRESH block ... returned
                # below" and prescribe a replay "using the line numbers in numbered_text above":
                # there are none, and the replay answers "op 0: replace range must be inside 1..0".
                # Measured shape, remedy and vocabulary: see Patcher._empty_root_refusal.
                return Patcher._empty_root_refusal(block_id, content, current_hash, expected_hash)
            # The guard has ALREADY read the current block to detect staleness, so
            # the correction is returned inside the refusal: the agent repairs in
            # this same call instead of spending a turn on read_block. Measured on
            # the 25/09 corpus: 12 stale-read refusals, 0 of them carried a hint,
            # and every one cost at least 2 turns (subagent s3).
            return {
                "status": "error",
                "block_id": block_id,
                "error": (
                    "Stale read: the block changed since you read it, so its line numbers no longer "
                    "designate the same lines. The FRESH block is returned below -- its numbered_text "
                    "and content_hash are the ones edit_block expects now. Re-send your operations "
                    "against these numbers in this same call; no separate read_block is needed."
                ),
                "content_hash": current_hash,
                "expected_hash": str(expected_hash),
                "hash_differs_at": Patcher._first_difference(str(expected_hash or ""), current_hash),
                "line_count": len(content.splitlines()),
                "numbered_text": numbered(content, 1),
                "actionable_hint": (
                    "Your expected_hash and the current content_hash differ at character "
                    + str(Patcher._first_difference(str(expected_hash or ""), current_hash))
                    + " -- they are NOT identical even when they look alike (you sent '"
                    + str(expected_hash or "") + "', the current one is '" + current_hash
                    + "'). Call edit_block(path='" + block_id + "', expected_hash='"
                    + current_hash + "', ops=[...]) using the line numbers in numbered_text above."
                ),
            }

        applied = apply_ops(content, ops, lambda fragment, _column: fragment)
        if applied.error:
            error = dict(applied.error)
            error["block_id"] = block_id
            if not content:
                # The op failed on a block that has no lines of its own. "op 0: replace range must be
                # inside 1..0" is true and names no way out -- t24 and t38 of run 1 (base ea25ac6) each
                # spent a turn on it. Say what the block IS, and which call gives it a first line:
                # `insert_after 0` is the one op an empty block accepts, and a test pins that it works.
                children = ""
                if kind == "document":
                    sections = self.db.get_document_sections(target_id)
                    if sections:
                        children = " Its " + str(len(sections)) + " child block(s) hold the text."
                error["error"] = (
                    str(error.get("error", "")) + " '" + str(block_id) + "' carries no text of its own: it is a "
                    + kind + " block with 0 lines, so there is nothing here to replace or delete." + children
                )
                error["actionable_hint"] = (
                    "insert_after 0 gives this block its first line; read_block('" + str(block_id)
                    + "') prints the addresses of the child blocks that hold the rest."
                )
            return error
        insert_duplicate: Optional[str] = None
        repeated = self._newly_repeated_line(content, applied.text)
        duplicated = repeated[0] if repeated else None
        was_at = repeated[1] if repeated else 0
        # WHICH OPERATION RE-EMITTED IT, named by its index -- the other half the agent
        # asked for ("a message naming the offending op and the real source line (8)").
        # `content` is the rebuilt block: the operation whose content holds the line is
        # the one that produced the second copy, and an insertion counts like any other.
        from_op = 0
        if duplicated:
            for position, one in enumerate(ops or []):
                if isinstance(one, dict) and duplicated in str(one.get("content") or ""):
                    from_op = position
                    break
        if duplicated:
            # A replace range is INCLUSIVE: the line just outside it (a-1 or b+1) is NOT
            # consumed, so content that re-emits it leaves TWO copies. This used to be WRITTEN
            # and merely warned about -- the artifact stayed broken until a second call repaired
            # it, four times in one run (base f831a4e, t33 among them) -- and the agent's own
            # words were: "a warning that accompanies a successful write is easy to miss and
            # leaves the artifact broken in the meantime". The check runs BEFORE any write, so
            # refusing costs the same turn as repairing and the file is never wrong in between.
            #
            # REFUSED only when a RANGE op could have caused it. An INSERT has no range, so
            # the repair the refusal names ("extend the range over it") is not an answer, and
            # re-emitting a nearby line from NEW content is ordinary -- a second loop whose body
            # is the same statement as the first. Measured on a r1 of the base fba3450: three
            # turns lost (t11 -> t13) on exactly that, and the only way out the agent found was
            # to rename its own local variables (t14), which is the tool dictating the code.
            places = " and ".join(
                "line " + str(one)
                for one in self._duplicate_line_numbers(applied.text, duplicated)
            )
            if self._has_range_op(ops):
                return {
                    "status": "error",
                    "block_id": block_id,
                    "error": (
                        "NOTHING was written: op " + str(from_op) + " of your content re-emits the"
                        " line '" + duplicated.strip() + "', which the block you read held ONCE,"
                        " at line " + str(was_at) + ". The result would hold it twice (at "
                        + places + "). A replace range is INCLUSIVE: the line just outside it (a-1 "
                        "or b+1) is NOT consumed by the replacement."
                    ),
                    "offending_op": from_op,
                    "source_line_in_your_read": was_at,
                    "duplicated_line": duplicated.strip(),
                    "content_hash": compute_content_hash(content.encode("utf-8")),
                    "line_count": len(content.splitlines()),
                    "numbered_text": numbered(content, 1),
                    "actionable_hint": (
                        "Drop that line from your content, or extend the range over it, and re-send: "
                        "the block is untouched and the numbered_text above is current, so no "
                        "read_block is needed."
                    ),
                }
            insert_duplicate = (
                "your content re-emits the line '" + duplicated.strip() + "', which the block already "
                "carries: it now holds it twice (at " + places + "). An insert has no range, so nothing "
                "is refused -- if the copy is deliberate (two similar statements), ignore this warning; "
                "if not, drop the line from your content."
            )
        if kind == "symbol":
            # A NAMESPACE block that the ops only PREFIX with using directives is not a rewrite of
            # the namespace: it is the request to add those usings, which have ONE legal place and
            # a route that already knows it. Anything else on a namespace keeps its refusal below.
            routed = self._using_route(target_id, block_id, content, applied.text)
            if routed is not None:
                self._remember_stamps(stamps)
                return routed
            # The fragment is inserted LITERALLY. read_block shows the block with its
            # real indentation, and the agent sends its lines at that same
            # indentation: measured on a real run, 12 of 12 fragment edits matched the
            # expected column. Re-basing the fragment was worse than useless -- the
            # reference column came from a neighbouring line, so a blank neighbour put
            # the whole insert at column 0, and the agent's correction (re-based on the
            # already-broken line) was answered noop. The block's own base is still
            # anchored to its AST level by the whole-block call below.
            # Whether the operations reach the block's closing delimiter: it is the
            # one structural fact the caller cannot see from the error alone, and the
            # one it asked for ("the tool did not flag it as a range problem").
            block_lines = len(content.splitlines())
            touches_last = any(
                (op.get("replace") and op["replace"][0] <= block_lines <= op["replace"][1])
                or (op.get("delete") and op["delete"][0] <= block_lines <= op["delete"][1])
                or (op.get("insert_after") == block_lines)
                for op in ops
            )
            result = self.update_symbol_body(
                target_id,
                new_code=applied.text,
                touches_last_line=touches_last,
                block_lines=block_lines,
            )
        elif kind == "document":
            result = self.update_document_root(target_id, new_content=applied.text)
        elif kind == "file_text":
            result = self.update_file_text(target_id, new_content=applied.text)
        else:
            # The ops go down WITH the content: a call that only ADDS can carry a heading
            # at the block's own level, which is a paragraph the caller CREATED and does
            # not belong inside it (see _title_route).
            result = self.update_document_section(
                target_id,
                new_content=applied.text,
                additions=self._added_fragments(ops, content),
            )

        # The answer names the path the caller WROTE, never the stored id the resolver
        # produced: that id is an internal coordinate (an overload carries a SHA-1 in it)
        # and it never goes back out on the surface.
        for key in ("block_id", "symbol_id", "section_id", "document_id"):
            if key in result and result[key] == target_id:
                result[key] = block_id
        # A routed ADDITION (see _title_route) created a NEW section in the FILE and left
        # this block exactly as it found it -- which is precisely the shape the check below
        # reads as "the insert landed outside the block" and reports with the HOST's hash,
        # lines and numbered_text. Here those fields are the CREATED section's, and the
        # reply already says, in its own words, that the host did not move.
        if result.get("title_route"):
            self._remember_stamps(stamps)
            if not result.get("diff_snippet"):
                result["diff_snippet"] = render_edit_snippet(
                    "", self._without_line_numbers(result.get("numbered_text") or "")
                )
            return result
        if result.get("status") in ("success", "noop"):
            after = self.read_block(block_id)
            if after.get("status") == "success":
                result["content_hash"] = after.get("content_hash", result.get("content_hash"))
                result["line_count"] = after.get("line_count", result.get("line_count"))
                result["numbered_text"] = after.get("numbered_text", result.get("numbered_text"))
                result["diff_snippet"] = render_edit_snippet(content, applied.text)
                if (
                    result.get("status") == "success"
                    and not self._preview_pass
                    and after.get("content_hash") == compute_content_hash(content.encode("utf-8"))
                    and applied.text != content
                ):
                    # The write went through, but NOT into this block: an insert_after that lands
                    # after the LAST line changes the FILE (the new code becomes a sibling) and the
                    # block keeps its own text. The reply used to report a diff for the block anyway
                    # -- "status: success, - 3 lines, + 39 lines" with line_count 3 and an UNCHANGED
                    # hash -- and the agent of a r0 of the base 4f3ae58 read that as "the insert
                    # silently did not apply" (t14), redid the work with insert_block (t15) and would
                    # have written the code TWICE. The fields of the block stay truthful; the fact is
                    # named, and the file listing hands over the address of what now follows it.
                    result["written_outside_block"] = True
                    file_path = str(block_id).split("::")[0]
                    written = self._written_beyond_block(target_id, file_path, content, applied.text)
                    if written is not None:
                        # Both evidence fields described the state BEFORE the write until now: the
                        # block that did not move kept its own hash and its own lines, so the ONLY
                        # proof the new code existed was the file listing. Written down by the agent
                        # of a r1 of the base ec572f1e: "the reply's numbered_text field returned
                        # the *unchanged* 85-line block rather than the newly created sibling".
                        # What a caller checks a write with has to BE the write.
                        if written.get("block_id"):
                            result["written_block_id"] = written["block_id"]
                        if written.get("created_symbols"):
                            result["created_symbols"] = written["created_symbols"]
                        result["content_hash"] = written["content_hash"]
                        result["line_count"] = written["line_count"]
                        result["numbered_text"] = written["numbered_text"]
                        result["diff_snippet"] = render_edit_snippet(
                            "", self._without_line_numbers(written.get("numbered_text") or "")
                        )
                    warnings = list(result.get("warnings") or [])
                    warnings.append(
                        "the write went through, but the new lines are NOT inside " + str(block_id)
                        + ": they landed " + self._where_the_lines_landed(ops, content)
                        + ", so it changes the FILE and this block keeps the same hash and the same "
                        + str(after.get("line_count", result.get("line_count", 0)))
                        + " lines. Do NOT send it again."
                        + (
                            " The numbered_text, line_count and content_hash above are those of '"
                            + str(written["block_id"]) + "', the block this call created."
                            if written is not None and written.get("block_id") else ""
                        )
                    )
                    listing = self.read_block(file_path)
                    if listing.get("status") == "success":
                        text = listing.get("text") or listing.get("numbered_text") or ""
                        if text:
                            result["file_listing"] = text
                            warnings.append(
                                "read_block of " + file_path + " shows what now follows it, with "
                                "the address of the block that was created."
                            )
                    result["warnings"] = warnings
        result.setdefault("block_id", block_id)
        if result.get("status") == "success":
            if insert_duplicate:
                warnings = list(result.get("warnings") or [])
                warnings.append(insert_duplicate)
                result["warnings"] = warnings
            lost = self._blank_boundary_lost(ops, content)
            if lost:
                warnings = list(result.get("warnings") or [])
                warnings.append(
                    "the replaced range ended on line " + str(lost) + ", which was BLANK, and your "
                    "content does not end with a blank line: the blank line that separated what "
                    "follows is GONE (the range is inclusive). Add a trailing blank line to your "
                    "content if you wanted to keep it."
                )
                result["warnings"] = warnings
            dropped = self._heading_range_drops_a_bullet(ops, content)
            if dropped:
                number, text = dropped
                warnings = list(result.get("warnings") or [])
                warnings.append(
                    "the replaced range covered line " + str(number) + ", a bullet of the titled "
                    "item above it ('" + text[:80] + "'), and your content carries no bullet: the "
                    "bullet is GONE (the range is inclusive). Shrink the range to the title line "
                    "if you meant to keep it."
                )
                result["warnings"] = warnings
        self._remember_stamps(stamps)
        return result

    #: A using DIRECTIVE alone on its line: `using A.B;`, `global using static A.B;`, `using X = A.B;`.
    #: The `using var x = ...;` DECLARATION and `using (...)` statement do not match (no `=` after
    #: a plain name list, no parenthesis), and a comment line does not match either.
    _USING_DIRECTIVE_LINE = re.compile(
        r"^(?:global\s+)?using\s+(?:static\s+)?(?:[A-Za-z_]\w*\s*=\s*)?[\w.:<>,\[\]?]+(?:\s*,\s*[\w.:<>\[\]?]+)*\s*;$"
    )

    def _using_route(
        self, target_id: str, block_id: str, content: str, new_text: str
    ) -> Optional[Dict[str, Any]]:
        """Serve `an edit of a C# namespace that only adds usings above it`, or None (refusal kept).

        Measured on the campaign (03/10/2026): 8 calls on `SparkConnectOptions.cs::DataLinq.Spark`,
        every one an `insert_after 0` of `using ...;` or a `replace [1, 1]` that re-types the
        namespace line unchanged under new usings. The refusal sent the agent to qualify names
        instead, and the builds that followed failed.

        TRUE ONLY WHEN ALL OF THIS HOLDS, and any doubt is a None:
          * the target is a `namespace` symbol of a `.cs` file;
          * the new text is the old text UNCHANGED, preceded by nothing but blank lines and
            using directives (so no character of the namespace is rewritten);
          * at least one of those directives is not already in the file (a directive already
            there would be a duplicate declaration, and nothing is left to add).
        The write is the file route of `insert_symbol`, which seats a directive after the last
        existing one (or at the top) and runs the same syntax and duplicate guards as an
        `insert_block` of a using.
        """
        sym = self.db.get_symbol(target_id)
        if sym is None or sym.kind != "namespace" or not str(sym.file_path).lower().endswith(".cs"):
            return None
        old = content.replace("\r\n", "\n").rstrip("\n")
        new = new_text.replace("\r\n", "\n").rstrip("\n")
        if not old or len(new) <= len(old) or not new.endswith(old):
            return None
        prefix = new[: len(new) - len(old)]
        if not prefix.endswith("\n"):
            return None            # the old text does not start on a line boundary of the new one
        directives: List[str] = []
        for line in prefix.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if not self._USING_DIRECTIVE_LINE.match(stripped):
                return None
            directives.append(stripped)
        if not directives:
            return None
        try:
            file_text = self.config.to_absolute_path(sym.file_path).read_bytes().decode(
                "utf-8", "surrogateescape"
            )
        except (OSError, ValueError):
            return None
        present = {line.strip() for line in file_text.splitlines()}
        fresh: List[str] = []
        for directive in directives:
            if directive not in present and directive not in fresh:
                fresh.append(directive)
        if not fresh:
            return None
        result = self.insert_symbol(str(sym.file_path), "\n".join(fresh))
        if result.get("status") != "success":
            return result
        result["using_route"] = True
        result["block_id"] = block_id
        after = self.read_block(block_id)
        if isinstance(after, dict) and after.get("status") == "success":
            result["content_hash"] = after["content_hash"]
            result["line_count"] = after["line_count"]
            result["numbered_text"] = after["numbered_text"]
        warnings = list(result.get("warnings") or [])
        warnings.append(
            "'" + str(block_id) + "' is a namespace and was NOT rewritten: your operations only "
            "added using directive(s) above it, so " + ", ".join(fresh) + " went to the file's using "
            "region (after the last existing using). The namespace block keeps its own text and "
            "hash. To add a using, insert_block(parent='" + str(sym.file_path) + "', text='using X;') "
            "says it directly."
        )
        result["warnings"] = warnings
        return result

    @staticmethod
    def _lines_the_ops_touch(ops: Any) -> Optional[Set[int]]:
        """The 1-based lines of the block these operations ADDRESS, or None if unreadable.

        `replace [a, b]` and `delete [a, b]` address every line of the inclusive range.
        `insert_after n` addresses the boundary AFTER line n, so it stands or falls with line
        n itself; `insert_after 0` -- which puts lines before the block's first line --
        addresses no existing line, and is therefore always safe. An operation this reading
        does not understand answers None, and the caller then keeps its refusal.
        """
        touched: Set[int] = set()
        for op in ops or []:
            if not isinstance(op, dict):
                return None
            span = op.get("replace") if "replace" in op else op.get("delete")
            if span is not None:
                if not isinstance(span, (list, tuple)) or len(span) != 2:
                    return None
                try:
                    first, last = int(span[0]), int(span[1])
                except (TypeError, ValueError):
                    return None
                if first < 1 or last < first:
                    return None
                touched.update(range(first, last + 1))
                continue
            if "insert_after" in op:
                try:
                    anchor = int(op["insert_after"])
                except (TypeError, ValueError):
                    return None
                if anchor < 0:
                    return None
                if anchor >= 1:
                    touched.add(anchor)
                continue
            return None
        return touched

    def _batch_syntax_refusal(
        self, entries: List[Dict[str, Any]], results: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Refuse the WHOLE batch when an entry hit the syntax barrier, or None.

        See BATCH_ATOMICITY_RULE for the measured reason: the entries of one call were read
        against ONE version of the file, so applying the others after a syntax refusal leaves a
        file the caller cannot reason about -- its line numbers moved, and the expected_hash of
        the entries that were refused no longer opens their block. The refused entry itself
        wrote nothing, so refusing them all leaves every hash the caller sent valid and the
        repair is exactly the entry named here.
        """
        refused = [
            index for index, answer in enumerate(results)
            if Patcher._is_syntax_barrier(answer)
        ]
        if not refused:
            return None
        first = results[refused[0]]
        validation = first.get("validation") or {}
        detail = str((validation.get("errors") or [""])[0]) or "syntax validation failed"
        error = (
            "Batch refused: entry " + str(refused[0]) + " of " + str(len(entries))
            + " fails syntax validation, so NOTHING was written -- not that entry, and not the "
            "entries beside it either. " + detail
        )
        hint = (
            "Repair entry " + str(refused[0]) + " and send the batch again: every entry still "
            "matches the file, because nothing was written. " + BATCH_ATOMICITY_RULE
        )
        updated: List[Dict[str, Any]] = []
        for index, answer in enumerate(results):
            bid = str((entries[index].get("path") if index < len(entries) and isinstance(entries[index], dict) else "") or f"entry {index}")
            if index not in refused:
                updated.append({
                    "status": "error",
                    "entry_index": index,
                    "block_id": bid,
                    "error": (
                        "Refused with the batch: entry " + str(refused[0]) + " fails syntax "
                        "validation, and a batch is all-or-nothing. Nothing was written for this "
                        "entry either, so re-send it unchanged once entry " + str(refused[0])
                        + " is repaired."
                    ),
                    "batch_refused_with_entry": refused[0],
                    "actionable_hint": hint,
                })
                continue
            one = dict(answer)
            one["entry_index"] = index
            if not one.get("block_id") and not one.get("id"):
                one["block_id"] = bid
            one["error"] = error
            one["batch_atomic"] = True
            one["actionable_hint"] = hint
            updated.append(one)
        return {
            "status": "error",
            "applied": 0,
            "refused": len(results),
            "results": updated,
            "batch_atomic": True,
            "atomicity": BATCH_ATOMICITY_RULE,
            "actionable_hint": hint,
        }
    def _edit_batch(self, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Edit SEVERAL blocks in ONE call: ONE lock, and every entry REBASED.

        THREE THINGS HAPPEN, in this order, and each one is there because its absence was
        measured:

          1. THE FILES ARE LOCKED, all of them, in the alphabetical order FileLocks
             imposes -- the fixed order is what forbids a deadlock between two batches
             that cross. The lock is held across the writes AND the index pass of each
             file, so no other call can read a version this one is about to move, and our
             own edit-then-index sequence cannot collide with another one.
          2. A SYNTAX VALIDATION pass with the writes suppressed. It resolves every entry
             BEFORE the first byte moves, which is the last moment at which every block
             still stands as the caller read it: that is where the stamp table is built.
          3. THE REAL PASS. Every entry is REBASED against the version the caller READ
             before a single operation of it is read (Patcher._translate_entry), and each
             write advances the rebaser: the tool made those moves, so the tool undoes
             them for the entries that follow. Only two refusals survive, and they are the
             brief two: an aimed line another entry already wrote or deleted (a real
             conflict), and a stamp that is not the version this call started from (a
             writer OUTSIDE the tool).

        Each entry answers exactly like a single edit_block, in the order sent.
        """
        wanted = self._batch_files(entries)
        try:
            self._edits_lock.acquires(wanted)
        except LockTimeout as busy:
            message = (
                "Refused: " + str(busy) + " Nothing was written for ANY entry of this "
                "call, so every expected_hash you sent still opens its block. Send the same "
                "batch again once that call has finished."
            )
            return {
                "status": "error",
                "applied": 0,
                "refused": len(entries),
                "results": [
                    {"status": "error", "error": message, "actionable_hint": message,
                     "entry_index": index,
                     "block_id": str((entries[index].get("path") if index < len(entries) and isinstance(entries[index], dict) else "") or f"entry {index}")}
                    for index in range(len(entries))
                ],
                "actionable_hint": message,
            }
        originals: Dict[str, bytes] = {}
        results: List[Dict[str, Any]] = []
        self._batch_originals = originals
        self._batch_after = {}
        self._call_block_hashes = {}
        # THE STATE OF THE CALL, from here to the end of it: one entry per block the caller
        # addresses, holding the text it READ (frozen), the text as it now stands, the file
        # bytes we last left, and the rebaser of that block. It is what turns "another entry
        # moved my line" from a guess into a measurement.
        self._call_state = {}
        try:
            self._capturing_stamps = True
            previews: List[Dict[str, Any]] = []
            self._preview_pass = True
            try:
                previews = [
                    self._batch_entry(entry, index) for index, entry in enumerate(entries)
                ]
            finally:
                self._preview_pass = False
                self._capturing_stamps = False
            refusal = self._batch_syntax_refusal(entries, previews)
            if refusal is not None:
                return refusal
            for index, entry in enumerate(entries):
                self._remember_file(entry, originals)
                self._batch_entry_index = index
                entry["_file_key"] = self._file_of(entry.get("path"), entry)
                # ONE RESOLUTION OF THE BLOCK, here, and it becomes the state the entry is
                # rebased against: the text the caller READ (which the first resolution of
                # this call is the last to still see), its span in the file, and its rebaser.
                # Resolving it lazily inside the translator would read the POST-WRITE file
                # for every entry after the first, and hand the rebaser a version that is
                # already one entry ahead.
                path_used = str(entry.get("path"))
                if path_used and path_used not in self._call_state:
                    self._begin_block_state(
                        path_used, self._block_and_kind(path_used) if path_used else None
                    )
                results.append(self._batch_entry(entry, index))
            saved_batch_after = dict(self._batch_after) if self._batch_after else {}
        finally:
            self._batch_originals = None
            self._batch_after = None
            self._call_state = None
            self._call_block_hashes = {}
            self._last_translation = None
            self._batch_entry_index = 0
            self._edits_lock.releases(wanted)
        refusal = self._batch_syntax_refusal(entries, results)
        if refusal is not None:
            self._restore_files(originals)
            return refusal
        if self.history_manager is not None and saved_batch_after:
            commit_id = str(uuid.uuid4())
            parent_id = self.history_manager.get_head_commit_id()
            batch_files = []
            for file_str, (after_bytes, _) in saved_batch_after.items():
                p = Path(file_str)
                before_bytes = originals.get(file_str)
                before_sha = self.history_manager.save_blob(before_bytes) if before_bytes is not None else None
                after_sha = self.history_manager.save_blob(after_bytes)
                rel_path = self._relative_path(p)
                batch_files.append({
                    "file_path": rel_path,
                    "before_sha": before_sha,
                    "after_sha": after_sha,
                })
            self.history_manager.record_pending_commit(
                commit_id=commit_id,
                parent_id=parent_id,
                kind="scalpel_edit",
                tool_name="edit_block",
                files=batch_files,
            )
            self.history_manager.commit_transaction(commit_id)
        used = [
            str(entry.get("path")) for entry in entries
            if isinstance(entry, dict) and entry.get("path")
        ]
        answers = [self.unify_addresses(answer, used=used) for answer in results]
        applied = sum(1 for answer in answers if answer.get("status") == "success")
        noops = sum(1 for answer in answers if answer.get("status") == "noop")
        refused = len(answers) - applied - noops
        return {
            "status": "success" if (applied > 0 or (noops > 0 and refused == 0)) else "error",
            "applied": applied,
            "refused": refused,
            "results": answers,
        }
    def _batch_entry(self, entry: Any, index: int) -> Dict[str, Any]:
        """One entry of an `edits` batch, answered exactly like a single edit_block call.

        THE ENTRY IS REBASED FIRST, and that is the whole change. The tool made the moves
        the earlier entries caused, so the 1-based numbers this entry carries are
        translated onto the file as it now stands before a single operation is read
        (Patcher._translate_entry). What is left of the two refusals the brief allows is
        answered here:
          * an aimed line another entry of THIS call already wrote, or deleted -- a real
            conflict, and the refusal NAMES that entry;
          * a stamp that is not the version this call started from -- a writer OUTSIDE
            the tool, the one case the lock cannot reach.
        Everything else is served, and an entry that was rebased SAYS SO: `rebase` hands
        back the line numbers the caller wrote, so nobody has to re-read to find out where
        a line went.
        """
        if not isinstance(entry, dict) or not entry.get("path"):
            return {
                "status": "error",
                "entry_index": index,
                "error": (
                    "entry " + str(index) + " of edits has no path: nothing was written for it."
                ),
                "actionable_hint": (
                    "Give entry " + str(index) + " its path: edits=[{path: '<the block>', ops: "
                    "[...], expected_hash: '...'}, ...]. Every entry names the block it edits."
                ),
            }
        trouble = self._translate_entry(entry, index)
        if trouble is not None:
            fresh = self._fresh_block(entry)
            if "conflict" in trouble:
                # DECIDED BY THE PATCHER, not guessed from a message: the entry aims at a
                # line an earlier entry of the same call already wrote (Patcher._touched
                # -identities), and BOTH the line and the entry that got there first are
                # named in the caller own numbering.
                impossible = (
                    "line " + ", ".join(str(one) for one in trouble["conflict"])
                    + " -- entry " + str(trouble["with_entry"]) + " of this call wrote on it"
                )
                who = int(trouble["with_entry"])
                # THE RESPONSIBLE ENTRY IS ALWAYS NAMED HERE, and never as "None": this branch
                # is the PATCHER's own decision (two entries writing the same read line), so
                # the writer is known. Printing `entry None` was measured on a real refusal,
                # and a reply that names nothing while claiming a conflict reads as an
                # accusation with no author.
                blamed = " and entry " + str(who) + " writes that line too"
                return {
                    "status": "error",
                    "entry_index": index,
                    "block_id": str(entry.get("path")),
                    "error": (
                        "Refused: " + str(impossible) + " Entry " + str(index) + " of this"
                        " call" + blamed + ", and the tool cannot know which one you meant to"
                        " win: serving both would let the second overwrite a line the first"
                        " already rewrote. The refused entry wrote nothing."
                    ),
                    "rebased_by": who,
                    "conflict": True,
                    "actionable_hint": (
                        "Nothing was written for entry " + str(index) + ". Send this one as its"
                        " OWN call once the other has landed, or merge the two writes into ONE"
                        " operation."
                    ),
                    **fresh,
                }
            if "translation" in trouble:
                # AN OPERATION THE TOOL CANNOT READ IS NOT A CONFLICT, AND NOT A STALE READ.
                # The rebaser answers LineTranslationError for two families: an aimed line it
                # can no longer map (the real conflict, decided above), and a payload it cannot
                # PARSE at all -- not an object, none of replace/delete/insert_after, a
                # malformed or out-of-range span. Both used to leave through the ONE return
                # above, so a batch of a SINGLE entry answered "the tool cannot know which one
                # you meant to win: serving both would let the second overwrite a line the
                # first already rewrote" about a second entry that did not exist, with
                # `conflict: true` and `rebased_by: null` -- an accusation with no author,
                # measured on six distinct payloads. The cause is neither of the brief's two:
                # nothing about the caller stamp is wrong, and no other entry wrote the line.
                # The operation itself could not be read, and NO line number the tool could
                # return would repair it -- which is why the stale-read wording (and its
                # "re-send against these numbers") is not used here either.
                return {
                    "status": "error",
                    "entry_index": index,
                    "block_id": str(entry.get("path")),
                    "error": (
                        "Refused: " + str(trouble["translation"]) + " Entry " + str(index)
                        + " of this call carries an operation the tool cannot read, so it"
                        " cannot tell which lines that operation addresses. No other entry of this call wrote the"
                        " line: the operation itself is unreadable. The refused entry wrote"
                        " nothing."
                    ),
                    "unreadable_operation": True,
                    "actionable_hint": (
                        "Nothing was written for entry " + str(index) + ". Re-send it with"
                        " operations shaped as replace [a, b], delete [a, b] or insert_after n,"
                        " each span inside the line numbers read_block printed."
                    ),
                    **fresh,
                }
            return {
                "status": "error",
                "entry_index": index,
                "block_id": str(entry.get("path")),
                "error": (
                    "Stale read: the block changed since you read it, so its line numbers"
                    " no longer designate the same lines -- a writer outside these tools"
                    " moved it between your read and this call. The FRESH block is returned"
                    " below -- its numbered_text and content_hash are the ones edit_block"
                    " expects now. Re-send your operations against these numbers in this"
                    " same call; no separate read_block is needed."
                ),
                "expected_hash": str(entry.get("expected_hash") or ""),
                "actionable_hint": (
                    "Nothing was written for entry " + str(index) + ". Re-send it against"
                    " the numbered_text below."
                ),
                **fresh,
            }
        translated = entry.pop("_translated", False)
        rebased_lines = entry.pop("_rebased_lines", None)
        rebased_on = entry.pop("_rebased_on", None)
        rebased_ops = entry.pop("_rebased_ops", None)
        rebased_by = (rebased_on or [None])[0] if translated and rebased_on else None
        # Was the stamp this entry sent BEHIND its block when its turn came? Only a sibling's
        # write can have made it so here (a rebase has its own warning): compare the stamp with
        # the block's hash NOW, before this entry writes.
        stamp_was_behind = False
        if entry.get("_sibling_wrote") and entry.get("expected_hash"):
            try:
                head = self.read_block(str(entry.get("path")))
                stamp_was_behind = head.get("content_hash") != entry.get("expected_hash")
            except Exception:
                stamp_was_behind = False
        answer = self.edit_block(
            block_id=str(entry.get("path")),
            ops=rebased_ops if translated and rebased_ops is not None else (entry.get("ops") or []),
            expected_hash=entry.get("expected_hash"),
        )
        if isinstance(answer, dict):
            answer["entry_index"] = index
            if not answer.get("block_id") and not answer.get("id"):
                answer["block_id"] = str(entry.get("path"))
        moved_lines = bool(rebased_lines)
        sibling = bool(entry.pop("_sibling_wrote", False))
        if (moved_lines or sibling) and answer.get("status") in ("success", "noop"):
            # WHAT WAS RECALED, AND BY WHOM. The caller wrote its operations against the
            # version it read; the tool moved those lines ITSELF, so it names the entry
            # that moved them and what the numbers became. Nothing was refused -- and the
            # stamp is not reusable, which is why the reply carries the current one.
            answer["expected_hash_rebased"] = True
            if rebased_by is not None:
                answer["rebased_by"] = rebased_by
            answer["rebase"] = {
                "your_lines": sorted(int(one) for one in (rebased_lines or [])),
                "moved_by_entry": rebased_by,
            }
            warnings = list(answer.get("warnings") or [])
            if moved_lines:
                warnings.append(
                    "entry " + str(rebased_by) + " of THIS call wrote this block after you"
                    " read it, so the lines you aimed at had MOVED. The tool rebased them:"
                    " the operations you sent were applied to the lines you read, nothing"
                    " was refused, and your 1-based numbers still designate them. The stamp"
                    " you sent is NOT reusable -- this reply carries the current one."
                )
            elif stamp_was_behind:
                # The tool changed THIS block's own text (a renumbered heading): the stamp sent
                # really was one version behind, and the model cannot see that elsewhere.
                warnings.append(
                    "another entry of THIS call wrote the same FILE after you read it, so the"
                    " stamp you sent was one version behind -- but every line this entry aims"
                    " at still holds exactly what you read, so nothing had to be rebased. The"
                    " operations were applied and nothing was refused. The stamp you sent is"
                    " NOT reusable: this reply carries the current one."
                )
            # A sibling that wrote the same FILE WITHOUT touching this block taught nothing: the
            # hash of a block is the hash of its own text, so the stamp was NOT behind (measured:
            # it still opens the block after the sibling's write), and 'not reusable' holds for
            # every write -- the reply's own content_hash carries the current one. The structured
            # fields (expected_hash_rebased, rebase) still say so for a caller that reads them.
            if warnings:
                answer["warnings"] = warnings
        return answer

    def _fresh_block(self, entry: Any) -> Dict[str, Any]:
        """The current numbered text and hash of the block an entry names, for a refusal.

        The rebaser has ALREADY read the file -- that is what detected the staleness -- so
        the correction travels inside the refusal and the caller repairs in the same call
        instead of spending a turn on read_block.
        """
        path = str(entry.get("path") or "")
        try:
            read = self.read_block(path)
        except Exception:
            return {}
        if not isinstance(read, dict) or read.get("status") != "success":
            return {}
        return {
            "content_hash": read.get("content_hash"),
            "line_count": read.get("line_count"),
            "numbered_text": read.get("numbered_text"),
        }
    # ------------------------------------------------------------------
    # The lock, and the state of the caller's read
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # The lock, and the state of the caller's read
    # ------------------------------------------------------------------
    def _file_of(self, block_id: Any, entry: Any = None) -> Optional[str]:
        """The absolute path of the FILE behind a block address, or None when it names none.

        An address carries its file part FIRST: docs/spec.md#part/alpha and
        src/app.py::Classe::Methode both name a file before any block segment. The FILE is what
        the lock is taken on, so this answers WITHOUT resolving the block: an address whose
        block no longer resolves must still be locked, or it would be the one write that runs
        unprotected. The answer is absolute, so the lock, the snapshot and the rebaser all key
        on ONE spelling of the file -- and FileLocks normalises that spelling again itself.
        """
        raw = str(block_id or "")
        target = raw.split("::")[0]
        if "#" in target:
            target = target.split("#", 1)[0]
        target = target.strip()
        if not target:
            return None
        absolute = self._existing_path(target)
        if absolute is not None:
            return str(absolute)
        if entry is not None:
            resolved = self._block_and_kind(raw) if raw else None
            if resolved is not None and resolved[3] is not None:
                return str(resolved[3])
        try:
            return str(self.config.to_absolute_path(target))
        except (ValueError, AttributeError):
            return None

    def _batch_files(self, entries: List[Dict[str, Any]]) -> List[str]:
        """Every DISTINCT file this batch may write, in the order the entries demand them.

        Distinct, in first-appearance order: FileLocks.acquires sorts them itself into the
        FIXED alphabetical order that forbids a deadlock between two batches that cross, so
        this list only has to be complete.
        """
        files: List[str] = []
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not path:
                continue
            target = self._file_of(path, entry)
            if target and target not in files:
                files.append(target)
        return files

    def _stamp_is_behind(self, path: Any) -> bool:
        """Is the block of that address no longer the version this call started from?

        True exactly when another entry of THIS call has written the file since -- which is
        the fact the warning is about. It is MEASURED, not inferred from the absence of a
        line rebase: an entry may be served with no line moved (its lines were never
        displaced) while the file itself is already one version ahead of the caller stamp.
        """
        if self._call_state is None or not path:
            return False
        state = self._call_state.get(str(path))
        if state is None:
            return False
        # HAS THIS CALL WRITTEN THAT FILE YET? `_batch_after` is filled by _atomic_write with
        # the bytes and the entry index of every write this call makes, so membership there is
        # the fact itself. Comparing the block text against the stamp it started from was
        # measured to miss the case the warning exists for: entry 0 writes ANOTHER block of the
        # same file, this entry block is byte for byte what the caller read, and its stamp is
        # still one version behind the FILE.
        written = self._batch_after or {}
        for target in written:
            if str(target) == str(state.get("abs")):
                return True
        return False

    def _the_file_is_still_ours(self, path: str) -> bool:
        """Is that file on disk still what this call last left there?

        A FILE comparison, not a block one: an outside writer ANYWHERE in that file refuses the
        entry, because the moves it made below the aimed lines are moves we never made and
        cannot translate.
        """
        if self._call_state is None:
            return True
        for state in self._call_state.values():
            if str(state.get("abs")) != str(path):
                continue
            try:
                return Path(path).read_bytes() == state.get("file_bytes")
            except OSError:
                return False
        return True

    def _remember_stamps(self, stamps: Any) -> None:
        """Record what each block of this call held BEFORE the call wrote anything.

        Called on the way OUT, once every entry has run: the table is emptied before the real
        pass (see Patcher._edit_batch) and this is what refills it for a later entry. A
        single call never reads it, so filling it here cannot influence the call that fills
        it -- and `setdefault`, because the FIRST resolution of a block is the version the
        caller read.
        """
        if not self._capturing_stamps or not stamps:
            return
        for key in (str(stamps[0]), str(stamps[1])):
            self._call_block_hashes.setdefault(key, str(stamps[2]))

    def _written_id(self, block_id: Any) -> str:
        """The address the ENTRY carried for the block being written.

        edit_block resolves its target first and then works on target_id, so by the time
        the stamp is read the address the caller wrote is out of sight. The state of the
        call records which address opened each block, and this reads that back: the stamp
        table is keyed by what the CALLER wrote, never by an internal id.
        """
        target = str(block_id)
        if self._call_state is None or target in self._call_state:
            return target
        for used in self._call_state:
            if str(self._resolve_displayed_path(used) or used) == target:
                return used
        return target

    def _begin_block_state(self, used: str, resolved: Any) -> Optional[Dict[str, Any]]:
        """Register ONE block of this call: the version the caller read, and its rebaser.

        Keyed by the address the CALLER WROTE, because that is what every entry carries.
        The rebaser is built on the BLOCK own text -- read_block numbers a block from 1, so
        a rebaser fed the whole file would put every identity off by the preamble and
        refuse every entry. `block_bytes` is the slice that block occupies in the file,
        which is what lets a later write re-select the block from the bytes just written.
        """
        if str(used) in self._call_state:
            # ALREADY REGISTERED by an earlier entry of this call. Rebuilding it here would
            # re-read the file AFTER that entry wrote, so the version the caller read would be
            # replaced by the version the call has just produced -- and every identity of the
            # rebaser would be off by that move.
            return self._call_state[str(used)]
        if resolved is None or resolved[3] is None:
            return None
        absolute = Path(str(resolved[3]))
        try:
            raw = absolute.read_bytes()
        except OSError:
            return None
        # THE FILE IS RE-READ HERE, and `span` is an EXACT byte span into it. `resolved[4]`
        # carries the same bytes, but this read is what makes the window and the text ONE
        # observation, taken under the lock: `file_bytes`/`start_bytes` are compared BYTE FOR
        # BYTE against the disk (`_the_file_is_still_ours`).
        # The block is cut with `errors="surrogateescape"` for the same reason the span is
        # measured on a round-tripping decode: a block the UTF-8 decoder cannot read still has
        # to re-encode to its OWN bytes when this call writes the file back.
        span = resolved[5] or (0, len(raw))
        block_bytes = bytes(raw[span[0]:span[1]])
        block_text = block_bytes.decode("utf-8", "surrogateescape")
        state = {
            "used": str(used),
            "abs": str(absolute),
            "read": block_text,
            "current": block_text,
            "block_bytes": (int(span[0]), int(span[1])),
            "file_bytes": raw,
            #: What the file held when this call STARTED. `file_bytes` advances with every
            #: write, so comparing the two is what tells "another entry of this call wrote
            #: this file" from "nothing has happened yet".
            "start_bytes": raw,
            "rebaser": LineTranslation(block_text, block_text),
        }
        self._call_state[str(used)] = state
        return state
    def _translate_entry(self, entry: Any, index: int) -> Any:
        """Rebase ONE entry's operations onto the block as it now stands.

        Returns a marker dict when the entry may not be served, None when it may. TWO causes
        only, and they are the brief's two legitimate refusals:
          * a stamp that is not the version this call FOUND the block in -- a writer outside
            the tool (a human in an editor, a git checkout); the lock cannot reach it, and
            nobody can translate a move we never made;
          * an aimed line that ANOTHER ENTRY OF THIS CALL already wrote, or deleted. The
            rebaser raises and names that entry: it is a real conflict, the tool cannot know
            which of the two writes was meant to win.
        Anything else is translated. There is no third refusal, and no refusal "to be safe".

        THE NUMBERS ARE BLOCK-RELATIVE. read_block numbers the lines of ONE BLOCK from 1, so
        the rebaser is built on the BLOCK's own text and never on the file: a rebaser fed the
        whole file would put every identity off by the preamble, and would refuse every entry
        of every batch. The file is compared byte for byte (it is what detects a third party),
        and the block is what is translated.
        """
        if self._preview_pass or self._call_state is None or not isinstance(entry, dict):
            # The VALIDATION pass reads every payload and writes nothing, so there is no move
            # to rebase: rebasing there would also consume the stamp table the real pass needs.
            return None
        path = entry.get("path")
        state = self._call_state.get(str(path)) if path else None
        if state is None:
            return None            # nothing to lock or rebase: the resolver answers for it
        expected = entry.get("expected_hash")
        # AN ENTRY THIS CALL NEVER RESOLVED IS NOT EVIDENCE OF A THIRD PARTY. The table holds
        # what the call FOUND, and it is filled on the way OUT of a successful resolution
        # (Patcher._remember_stamps): an entry whose own payload the tool cannot read returns
        # before that point, so its block stays ABSENT here. Reading that absence as "not the
        # version the call started from" refused the entry with "a writer outside these tools
        # moved it" on a file nobody had touched -- measured, and the refusal printed its own
        # expected_hash equal to its own content_hash, contradicting itself two lines below its
        # own sentence. An absent stamp means UNKNOWN, never "stale": the ordinary guard below
        # (Patcher._edit_block_locked) still refuses a real stale read, reading the caller stamp
        # against the version this call found.
        known = self._call_block_hashes.get(str(path))
        if expected and known is not None and not self._hashes_match(expected, known):
            return {"stale": True}
        if not self._the_file_is_still_ours(str(state.get("abs"))):
            return {"stale": True}
        # A REAL CONFLICT, DECIDED HERE. Two entries of one call that write the SAME line of
        # the version the caller read cannot both be served: the second would overwrite what
        # the first wrote, and the tool cannot know which one was meant to win. The line is
        # named in the caller own numbering, and so is the entry that got there first.
        touched = self._touched_identities(entry.get("ops"))
        already = state.get("written") or {}
        clash = sorted(one for one in touched if one in already)
        if clash:
            return {"conflict": clash, "with_entry": int(already[clash[0]])}
        rebaser = state.get("rebaser")
        if rebaser is None:
            return None
        try:
            translated = rebaser.translate(entry.get("ops") or [])
        except LineTranslationError as impossible:
            return {"translation": impossible}
        self._last_translation = translated
        # THE MARK IS SET ON SUCCESS, and never inferred from a missing one: a translator that
        # returns "nothing to complain about" is indistinguishable from one that never ran, and
        # reading a field nobody wrote sent the RAW operations to the write -- measured, and it
        # answered "replace range must be inside 1..5" on every rebased entry.
        entry["_translated"] = True
        entry["_rebased_ops"] = translated.ops
        #: The lines this entry WRITES, in the CALLER numbering: its replace and delete
        #: spans, and never the anchor of an insert_after -- an insertion retires no line and
        #: names no writer (the module says so itself), and recording it refused the reverse
        #: order of the same pair of operations, measured. `ops` here is the caller own list,
        #: so the identities need no translation.
        self._last_written_lines = sorted(self._touched_identities(entry.get("ops")))
        entry["_rebased_on"] = translated.rebased
        # THE LINES, not the entries: `rebased` names WHO moved them and `by_entry` names WHICH
        # ones. The reply reports the second (the caller own 1-based numbers) and the first
        # separately; conflating them printed an entry index where a line number belongs.
        #: The lines this entry MOVED, in the CALLER numbering -- the only numbering the
        #: conflict table and the refusal may speak. `translated.ops` is expressed in the
        #: CURRENT numbers, so re-deriving identities from it invented a conflict.
        entry["_touched"] = sorted(int(one) for one in translated.touched)
        entry["_rebased_lines"] = sorted(
            {int(line) for lines in translated.by_entry.values() for line in lines}
        )
        # A SIBLING IS A MOVE TOO. Entry 0 may rewrite ANOTHER block of the same file, which
        # leaves THIS block byte for byte what the caller read and still makes the stamp one
        # version behind. Nothing had to be translated here, and that is exactly what must be
        # said: the warning may only ever claim the moves it really made.
        # HAS ANYONE ELSE WRITTEN THIS FILE DURING THE CALL? That is a MEASUREMENT, never the
        # absence of a rebase: `translated.rebased` lists the EARLIER ENTRIES that moved an
        # aimed line, so it is empty both when nothing had to move AND on the FIRST entry of
        # the call -- and reading it as "a sibling wrote" made entry 0 announce a sibling that
        # did not exist yet, on every grouped call (measured: entry 0 of a two-entry batch, and
        # entry 0 of A5 where entry 1 is refused and nobody else touches its file). The file
        # bytes this call last left are the fact; the rebase list is not.
        entry["_sibling_wrote"] = bool(
            self._call_block_hashes.get(str(path)) not in (None, None)
            and self._stamp_is_behind(str(path))
        )
        entry["_rebaser"] = rebaser
        entry["_rebaser_target"] = state.get("abs")
        return None

    @staticmethod
    def _touched_identities(ops: Any) -> Set[int]:
        """The 1-based lines of the CALLER version these operations move, or an empty set.

        `replace [a, b]` and `delete [a, b]` move every line of the inclusive range;
        `insert_after n` moves no line at all -- an insertion retires nothing and names no
        writer, so two entries may insert at the same boundary and both are served.
        `insert_after 0` addresses nothing, and an operation this reading cannot parse is left
        to the rebaser, which answers for it with its own wording.
        """
        touched: Set[int] = set()
        for op in ops or []:
            if not isinstance(op, dict):
                continue
            span = op.get("replace") if "replace" in op else op.get("delete")
            if span is None:
                continue
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                continue
            try:
                first, last = int(span[0]), int(span[1])
            except (TypeError, ValueError):
                continue
            if first < 1 or last < first:
                continue
            touched.update(range(first, last + 1))
        return touched

    def _record_write(self, target: str, written: bytes) -> None:
        """What THIS call just left in that file: the baseline the next entry is rebased on.

        The rebaser is told WHICH ENTRY wrote, and over which extent, in the caller's own
        numbering -- that is what turns "entry 0 moved my line" from a guess into a fact.
        Every block of that file is then re-read FROM THE BYTES JUST WRITTEN, through the
        slice it occupied before the write: the block is re-selected as the caller addresses
        it, so a file with several edited blocks never hands one block's text to another's
        rebaser.
        """
        if self._call_state is None:
            return
        for state in self._call_state.values():
            if str(state.get("abs")) != str(target):
                continue
            translated = self._last_translation
            written_identities = self._last_written_lines or []
            raw = state.get("file_bytes") or b""
            before = raw.decode("utf-8", "surrogateescape")
            after = written.decode("utf-8", "surrogateescape")
            span_first, span_last = state.get("block_bytes") or (0, 0)
            if span_last is None or span_last > len(raw) or span_first > span_last:
                state["file_bytes"] = written
                continue
            # THE BLOCK, NOT THE FILE. The rebaser speaks the caller's numbering, which is
            # BLOCK-relative, so it must be handed the block's lines and never the file's:
            # fed the whole file it placed line 2 at slot 7 and wrote the operation past the
            # end of the block, which is what "replace range must be inside 1..5" was.
            #
            # The span is a BYTE range, so the two texts are cut as bytes and decoded -- a
            # character split against byte offsets turns "### 8. Alpha\r\n- a ..." into the
            # whole file plus a copy of its last line, eight identities instead of four.
            #
            # And the span MOVES: an entry that inserts lines into this block makes it longer,
            # so the block starts where it did and now ends further down. Re-slicing with the
            # OLD end read the block one line short, and the rebaser -- handed a state its own
            # plan did not describe -- attributed the entry OWN move to NOBODY: the `rebased`
            # list came back empty and the reply had no entry to name. Measured on the seven
            # cases of benchmarks/.work/CAS_ONE_SHOT.md, where every insertion silently
            # stopped counting as a rebase.
            #
            # THE WRITE IS STILL IN MEMORY HERE. `_atomic_write` calls this BEFORE os.replace,
            # so re-resolving the block would read the OLD file -- measured: the block came back
            # as its pre-write text and the rebaser, handed a state its own plan did not
            # describe, attributed the entry own move to NOBODY (`rebased` empty, no entry to
            # name). The block text and its new span are therefore rebuilt FROM THE BYTES THIS
            # CALL IS ABOUT TO WRITE.
            previous_block = raw[span_first:span_last].decode("utf-8", "surrogateescape")
            # THE BLOCK OWN NEW TEXT, exactly: the entry translated operations applied to the
            # text it moved FROM, by the SAME function that produced the write (`apply_ops`).
            # Nothing is inferred from byte arithmetic -- a span grows by whole lines and the
            # last fragment of an insertion carries no trailing newline, so counting bytes came
            # out one line short. The span itself only has to be RIGHT ENOUGH to hold the new
            # text: the next write re-derives its own text the same way.
            applied_here = apply_ops(
                previous_block, translated.ops if translated else [],
                lambda fragment, _column: fragment,
            )
            if applied_here.error is None:
                current_block = applied_here.text
            else:
                current_block = written[span_first:span_last].decode("utf-8", "surrogateescape")
            if len(current_block.encode("utf-8", "surrogateescape")) != (span_last - span_first):
                state["block_bytes"] = (
                    span_first,
                    span_first + len(current_block.encode("utf-8", "surrogateescape")),
                )
            state["current"] = current_block
            state["file_bytes"] = written
            # WHICH ENTRY WROTE WHICH LINE -- IN THE CALLER OWN NUMBERING, which is the only
            # numbering the entries and the conflict check speak. `translated.ops` is expressed
            # in the CURRENT numbers: keying this table on them was measured to INVENT a
            # conflict (three entries on one block, entry 2 refused for a line entry 1 never
            # touched, `line 4 -- entry 1 of this call wrote on it`, while line 4 was intact).
            # `translated.touched` is the same set of lines as the caller read them.
            #
            # AND AN INSERTION NAMES NO WRITER. `touched` includes the ANCHOR line of an
            # insert_after (it stands or falls with that line), but no insertion writes it:
            # recording it made a later `insert_after` on the same line -- the reverse order
            # being served -- a refusal, measured. Only a replace and a delete write.
            written_lines = state.setdefault("written", {})
            for identity in written_identities:
                written_lines.setdefault(int(identity), self._batch_entry_index)
            rebaser = state.get("rebaser")
            if rebaser is None:
                continue
            if translated is None:
                # No plan stands behind this write, so the alignment is all the rebaser
                # has -- and the alignment cannot tell two identical lines apart. It is the
                # exact fallback the engine documents, kept for a caller that never
                # translated; inside a grouped call this branch is unreachable.
                rebaser.observe_lines(previous_block.splitlines(), current_block.splitlines())
                continue
            # RECORDED FROM THE PLAN, NEVER RE-DIFFED. The entry translated its operations
            # BEFORE writing, so the tool holds the exact regions it produced, and
            # `note_written` applies those. Re-aligning the two texts instead was measured to
            # process a PREVIOUS entry write a SECOND time -- the diff sees the earlier
            # insertion again -- and to collapse the identity table: line 4 of a five-line
            # block came back as slot 3, then "line 4 is outside the version you read" refused
            # an entry whose block was intact. The plan is also what places a copy among
            # IDENTICAL lines (case H of benchmarks/.work/CAS_ONE_SHOT.md): alignment alone
            # puts the inserted copy one line too high and the next entry rewrites the wrong
            # "- dup". The alignment is kept only for a write this call never translated.
            # THE TWO TEXTS ARE COMPARED IN THE SAME NORMAL FORM. `observe_lines` re-aligns
            # them line by line, and a block read WITHOUT a trailing newline against the same
            # block written WITH one differs by that alone: the alignment then sees a change
            # on the last line as well as the entry real one, and the identity table collapses
            # (measured: line 4 of a five-line block came back as slot 3, then "line 4 is
            # outside the version you read" refused an entry whose block was intact). Ending
            # both lists at a real line removes the artefact without inventing one.
            rebaser.observe_lines(
                _block_lines(previous_block),
                _block_lines(current_block),
            )
    def _remember_file(self, entry: Any, originals: Dict[str, bytes]) -> None:
        """Snapshot the file an entry targets, once, before this batch first writes to it."""
        path = str(entry.get("path")) if isinstance(entry, dict) and entry.get("path") else ""
        # A Markdown entry names a PARAGRAPH (`docs/spec.md#part/x`), not the file: without
        # the '#' the resolver looked for a file literally called `...#part/x`, found none,
        # and snapshotted NOTHING -- so a refused batch never put a Markdown file back. The
        # same snapshot is what the batch repair needs: it is the state the caller read from.
        target = path.split("::")[0]
        if "#" in target:
            target = target.split("#", 1)[0]
        absolute = self._existing_path(target) if target else None
        if absolute is None or not absolute.is_file():
            return
        key = str(absolute)
        if key not in originals:
            originals[key] = absolute.read_bytes()

    def _restore_files(self, originals: Dict[str, bytes]) -> None:
        """Put back, byte for byte, the files a refused batch had already written."""
        self._restoring_files = True
        try:
            for path, raw in originals.items():
                absolute = Path(path)
                self._atomic_write(absolute, raw)
                self._reindex(absolute)
        finally:
            self._restoring_files = False

    @staticmethod
    def _is_syntax_barrier(answer: Any) -> bool:
        """True when an answer is the syntax barrier refusal, and not another cause.

        Read from `validation.syntax_ok`, never from the message text: the cause is a FIELD, and
        a check that greps its own prose is one wording change away from refusing the wrong
        batch.
        """
        if not isinstance(answer, dict) or answer.get("status") != "error":
            return False
        validation = answer.get("validation")
        return isinstance(validation, dict) and validation.get("syntax_ok") is False
    @staticmethod
    def _has_range_op(ops: Any) -> bool:
        """True when at least one op replaces or DELETES a range.

        The duplicate refusal names "extend the range over it" as the repair, so it only holds for a
        range op. An insert has no range: refusing there tells the caller to do something impossible,
        which cost three turns on a r1 of the base fba3450.
        """
        for op in ops or []:
            if isinstance(op, dict) and (op.get("replace") or op.get("delete")):
                return True
        return False

    @staticmethod
    def _duplicate_line_numbers(text: str, line: str) -> List[int]:
        """The 1-based lines of `text` that hold EXACTLY `line` (blank lines never count).

        The refusal used to say the copy "already sits within a few lines of the range", which an
        agent read as a proximity rule and mis-corrected twice (r0 of the base fba3450, t15: "the
        line is at b+1"). Naming the two lines is the same fact, checkable on the spot.
        """
        return [
            number
            for number, one in enumerate(text.splitlines(), 1)
            if one.strip() and one == line
        ]

    @staticmethod
    def _blank_boundary_lost(ops: Any, before_text: str) -> Optional[int]:
        """The line a `replace` range ended on was BLANK and the content does not end blank.

        The range is INCLUSIVE, so `replace [17,18]` where line 18 is blank DELETES that blank
        line unless the content ends with one. The file still compiles and the reply says
        success: the defect is only visible in the returned text. An agent had to repair it with
        a second call -- "the boundary blank line was consumed", base 0489adc rep 2 -- so the
        fact is named in the same reply. NOT a refusal: removing a blank line is a legitimate
        edit, unlike re-emitting a declaration.
        """
        before_lines = before_text.splitlines()
        for op in ops or []:
            span = op.get("replace") if isinstance(op, dict) else None
            if not span:
                continue
            last = int(span[1])
            if last < 1 or last > len(before_lines) or before_lines[last - 1].strip():
                continue
            content = str(op.get("content") or "")
            trailing = content.rsplit(chr(10), 1)[-1] if chr(10) in content else content
            if content.strip() and trailing.strip():
                return last
        return None

    @staticmethod
    def _looks_like_a_title(line: str) -> bool:
        """A heading (`#`) or a numbered item (`3. **...**`, the shape the campaign used)."""
        stripped = line.strip()
        if stripped.startswith("#"):
            return True
        return bool(re.match(r"^\d+[.)]\s+\S", stripped))

    @staticmethod
    def _looks_like_a_bullet(line: str) -> bool:
        """A markdown LIST MARKER -- never a numbered item, which is a title in this reading."""
        return bool(re.match(r"^\s*[-*+]\s+\S", line))

    @staticmethod
    def _heading_range_drops_a_bullet(ops: Any, before_text: str) -> Optional[Tuple[int, str]]:
        """A `replace` range covered a TITLED item AND the bullet under it; the content re-emits
        the title only -- the bullet is GONE, and the reply used to say nothing.

        Measured twice in the campaign of the 02/10/2026 (`p1_r16` t41, `p1_r17` t27): the agent
        renumbered an item, wrote `replace [7, 8]` with the bare title, and line 8 held the item's
        sub-bullet. The answer was `success` with `warnings: []`, and the agent paid a repair call
        -- the same class as the boundary blank line, one turn further.

        DELIBERATELY NARROW, because a warning that fires on every ordinary replace is the false
        positive this depot has already removed detectors for. Three conditions, and all three are
        the defect's shape: the range STARTS on a title, its content carries NO bullet at all, and
        the dropped line IS a bullet the content does not re-emit. A rewrite that keeps or replaces
        a bullet never matches.

        NOT a refusal: dropping a bullet on purpose is a legitimate edit. The fact is named so the
        caller can check it against the text it holds.
        """
        before_lines = before_text.splitlines()
        for op in ops or []:
            span = op.get("replace") if isinstance(op, dict) else None
            if not span:
                continue
            try:
                first, last = int(span[0]), int(span[1])
            except (TypeError, ValueError):
                continue
            if first < 1 or last > len(before_lines) or last <= first:
                continue
            if not Patcher._looks_like_a_title(before_lines[first - 1]):
                continue
            content_lines = str(op.get("content") or "").splitlines()
            if any(Patcher._looks_like_a_bullet(one) for one in content_lines):
                continue
            content_text = {one.strip() for one in content_lines if one.strip()}
            for number in range(first + 1, last + 1):
                line = before_lines[number - 1]
                if not Patcher._looks_like_a_bullet(line):
                    continue
                if line.strip() in content_text:
                    continue
                return number, line.strip()
        return None

    COMMON_SYNTAX_LINES = {
        "if self.suppression:",
        "if self.suppression is not None:",
        "return None",
        "return True",
        "return False",
        "return",
        "pass",
        "break",
        "continue",
    }

    @classmethod
    def _newly_repeated_line(cls, before: str, after: str) -> Optional[Tuple[str, int]]:
        """A line the edit made appear twice, NEXT TO its original copy, and where it WAS.

        COUNTED and BOUNDED, which the two earlier versions each got half of:
        - the line must have occurred EXACTLY ONCE before the edit. A block full of
          '/// </summary>' (eleven of them in one C# property group) legitimately gains
          one more when a doc comment is added; reporting that was the recurring false
          positive (a r1 and a r3 of the base 9e506c5, runs 34/38 of b2c82d5);
        - common syntax guard lines ('if self.suppression:', 'return None', etc.) are ignored
          as they legitimately repeat within short error handling blocks;
        - the two copies must sit within 4 lines. The two real
          'builder = builder.Config(...)' statements are 16 lines apart and are not a copy
          (run 38, base b2c82d5); the accidental copy a replace range makes sits
          immediately next to the line it re-emitted.
        A replace range is INCLUSIVE, so the copy is usually the line just outside it
        (a-1 or b+1); the count rule also catches it when the range was simply too short.
        """
        counts_before: Dict[str, int] = {}
        for line in before.splitlines():
            if line.strip():
                counts_before[line] = counts_before.get(line, 0) + 1

        seen_at: Dict[str, List[int]] = {}
        for index, line in enumerate(after.splitlines()):
            if line.strip():
                seen_at.setdefault(line, []).append(index)

        for line, places in seen_at.items():
            stripped = line.strip()
            if len(stripped) < 8 or len(places) < 2 or stripped in cls.COMMON_SYNTAX_LINES:
                continue
            if counts_before.get(line, 0) != 1:
                continue
            gap = min(second - first for first, second in zip(places, places[1:]))
            if gap <= 4:
                # THE CALLER COORDINATE, RETURNED WITH THE LINE. `places` are positions in the
                # AFTER text -- the file the caller has never seen. Measured twice (fix1 t27,
                # s10 t27): the refusal announced "at line 12 and line 16" for a 12-line block,
                # and the agent wrote back that it had to "re-derive the cause myself". The line
                # occurred EXACTLY ONCE before (the guard above), so its position in what the
                # caller read is the one position that means something to it.
                was_at = next(
                    (i + 1 for i, one in enumerate(before.splitlines()) if one == line),
                    0,
                )
                return line, was_at
        return None

    def _written_beyond_block(
        self, target_id: str, file_path: str, before_text: str, after_text: str
    ) -> Optional[Dict[str, Any]]:
        """What a splice wrote OUTSIDE the block it was aimed at, addressed and hashed.

        When the write lands beyond the block's last line the block keeps its own text -- so the
        text the caller SUBMITTED is the block's own lines followed by the lines the file gained.
        Those added lines are the evidence of the write, and the block that did not move can prove
        nothing about them: its hash is the same one the caller sent.

        The lines are read back from the FILE, not rebuilt from the payload: an edit of what is
        named here expects the bytes on disk (CRLF included), and the hash below is that text's.
        The block the write CREATED is named when one starts inside them, because the lines are
        what the caller has to edit next. None when the payload re-indents the block itself: the
        block is then not a prefix of the submission, and the remainder would be a guess.
        """
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        if len(after_lines) <= len(before_lines) or after_lines[: len(before_lines)] != before_lines:
            return None
        added = len(after_lines) - len(before_lines)
        row = self.db.get_symbol(target_id)
        last_line = int(getattr(row, "end_line", 0) or 0)
        absolute = self._existing_path(file_path)
        if last_line <= 0 or absolute is None or not absolute.is_file():
            return None
        lines = absolute.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if last_line >= len(lines):
            return None
        written_text = "".join(lines[last_line : last_line + added]).rstrip("\r\n")
        if not written_text.strip():
            return None
        written: Dict[str, Any] = {
            "block_id": None,
            "content_hash": compute_content_hash(written_text.encode("utf-8")),
            "line_count": len(written_text.splitlines()),
            "numbered_text": numbered(written_text, 1),
        }
        # The address of the block the write CREATED: the first symbol that now starts inside
        # those lines. The span is one line wider than the text, because the added text may
        # open with a blank line.
        span = range(last_line + 1, last_line + 2 + added)
        created = [
            one for one in self.db.get_symbols_for_file(file_path)
            if int(one.get("start_line") or 0) in span
        ]
        if not created:
            return written
        created_sorted = sorted(created, key=lambda one: int(one.get("start_line") or 0))
        written["created_symbols"] = [
            {
                "id": self.displayed_id(str(item["id"])) or str(item["id"]),
                "kind": item.get("kind"),
                "name": item.get("name"),
                "line": int(item.get("start_line") or 0),
            }
            for item in created_sorted
        ]
        identifier = str(created_sorted[0]["id"])
        metrics = self.block_metrics(identifier)
        if metrics is None:
            return written
        written["block_id"] = self.displayed_id(identifier) or identifier
        written["content_hash"] = metrics["content_hash"]
        written["line_count"] = metrics["line_count"]
        written["numbered_text"] = metrics["numbered_text"]
        return written

    def _owner_of_hash(self, expected_hash: str, target_id: str) -> Optional[str]:
        """The printed address of ANY block whose text hashes to `expected_hash`, or None.

        The local lookup (`_child_carrying_hash`) only walks the children of the target DOCUMENT,
        so a stamp carried from another file fell through to "the block changed since you read it"
        -- a cause that is FALSE, the block never moved. Measured on the corpus (30/09/2026): 53
        stale-read refusals, 26 with hashes differing at the FIRST character (another block's
        stamp), and ZERO of them named the owner; the field `expected_hash_belongs_to` existed and
        had never been emitted once. Observed in a r2 of the base fdb4b18, t13 and t22: the agent
        carried the hash the Dossier had attached to SparkConnectOptions.cs into the test class and
        into guidance.md, and the reply blamed a change that had not happened.
        """
        if not expected_hash:
            return None
        for row in self.db.find_blocks_by_hash(expected_hash):
            printed = self.displayed_id(row["id"])
            if not printed:
                continue
            if str(printed) == str(target_id) or str(row["id"]) == str(target_id):
                # This IS the block being edited, spelled another way: not an owner.
                continue
            return printed
        return None

    #: How many index rows `_unique_owner_of_hash` asks for. Far above any honest count of
    #: blocks sharing one text: reaching it means "too many to be sure", which is a refusal.
    OWNER_LOOKUP_LIMIT = 50
    #: Most Markdown documents `_unique_owner_of_hash` is willing to lay out for one lookup.
    OWNER_DOCUMENT_CAP = 2000

    def _unique_owner_of_hash(self, expected_hash: Any) -> Optional[str]:
        """The printed address of THE ONE block whose CURRENT text hashes to `expected_hash`.

        Used to serve a batch entry that carries a stamp and no path. The stamp names a block,
        but only when it names exactly one, so every doubt is a None (and the caller keeps the
        refusal): a hash shared by two blocks (the same method copied into two files, two
        identical paragraphs) is ambiguous; a row of the index the disk no longer backs is
        dropped, because the index can lag the file -- each candidate is RE-RESOLVED and its
        text re-hashed, never trusted from the table; and a result list as long as the lookup
        limit is treated as ambiguous, since the rows beyond it are unseen.
        """
        stamp = str(expected_hash or "").strip()
        if not stamp:
            return None
        candidates: Dict[str, str] = {}      # stored identity -> printed address
        rows = self.db.find_blocks_by_hash(stamp, limit=self.OWNER_LOOKUP_LIMIT)
        if len(rows) >= self.OWNER_LOOKUP_LIMIT:
            return None
        for row in rows:
            printed = self.displayed_id(row["id"])
            if printed:
                candidates[str(row["id"])] = str(printed)
        # THE INDEX DOES NOT HOLD THE STAMP read_block PRINTS FOR A PARAGRAPH (it stores the hash
        # of the body alone), so the paragraphs are found by laying out each Markdown file, the
        # way `_child_carrying_hash` does for one document. Rare path (a refused entry), and it
        # bounds itself: a workspace with more documents than the cap is "too many to be sure".
        documents = self.db.list_documents(limit=self.OWNER_DOCUMENT_CAP)
        if len(documents) >= self.OWNER_DOCUMENT_CAP:
            return None
        for document in documents:
            document_id = str(document["id"])
            if not self._is_markdown(document_id):
                continue
            _abs_path, text, _newline, error = self._resolve_markdown(document_id)
            if error:
                continue
            layout = markdown_layout(text)
            if not layout:
                continue
            identifiers = self._layout_ids(document_id, layout)
            for index, item in enumerate(layout):
                if int(item["level"]) <= 0 or index >= len(identifiers):
                    continue
                start, end = int(item["section_start"]), int(item["section_end"])
                cand_hash = compute_content_hash(text[start:end].encode("utf-8"))
                if self._hashes_match(stamp, cand_hash):
                    # KEYED BY THE BLOCK, never by its printed address: two identical sibling
                    # paragraphs can print the SAME address, and counting addresses would
                    # take the pair for one block.
                    candidates[str(identifiers[index])] = str(
                        self.displayed_section_id(identifiers[index])
                    )
        live: Dict[str, str] = {}
        for key, printed in candidates.items():
            metrics = self.block_metrics(printed)
            if metrics is None or not self._hashes_match(stamp, metrics["content_hash"]):
                continue
            live[key] = printed
        return next(iter(live.values())) if len(live) == 1 else None

    @staticmethod
    def _first_difference(left: str, right: str) -> int:
        """1-based index of the first character that differs, or 0 when equal."""
        for index, (a, b) in enumerate(zip(left, right)):
            if a != b:
                return index + 1
        return 0 if len(left) == len(right) else min(len(left), len(right)) + 1

    def update_symbol_body(
        self,
        symbol_id: str,
        new_code: str = "",
        touches_last_line: bool = False,
        block_lines: int = 0,
        expected_hash: Optional[str] = None,
        edits: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Surgically replace a symbol's code on disk with AST auto-indentation and syntax barrier."""
        sym = self.db.get_symbol(symbol_id)
        if not sym:
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": f"Symbol '{symbol_id}' not found in graph database.",
            }

        if sym.kind == "namespace":
            return self._reject_namespace_mutation("an edit", sym)

        if expected_hash and not self._hashes_match(expected_hash, sym.content_hash):
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": f"Optimistic lock failure: symbol has hash '{sym.content_hash}', but expected '{expected_hash}'.",
            }

        try:
            abs_path = self.config.to_absolute_path(sym.file_path)
        except ValueError as exc:
            return {"status": "error", "symbol_id": symbol_id, "error": str(exc)}

        if not abs_path.exists():
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": f"Source file '{sym.file_path}' does not exist on disk.",
            }

        file_bytes = abs_path.read_bytes()
        parser = self.registry.get_parser_for_file(sym.file_path)
        if not parser:
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": f"No AST parser available for file '{sym.file_path}'.",
            }

        # Fragment mode: the caller quoted a few lines instead of resending the
        # whole block (measured: ~1 900 characters per edit, 99.3% identical on a
        # one-line change). Matching ignores indentation; the replacement is
        # re-based to where the match actually starts.
        if edits:
            block = file_bytes[sym.start_byte : sym.end_byte].decode("utf-8", errors="replace")
            applied = apply_edits(
                block,
                edits,
                lambda text, column: self.indent_mgr.rebase_indentation(
                    textwrap.dedent(text), column, file_path=sym.file_path
                ),
            )
            if applied.error:
                error = dict(applied.error)
                error["symbol_id"] = symbol_id
                return error
            new_code = applied.text
        elif not new_code.strip():
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": "Nothing to write: pass new_code (whole block) or edits (fragments).",
                "actionable_hint": (
                    "Send new_code with the whole replacement block, or edits=[{replace:[a,b],"
                    "content:'...'}] for a fragment. An empty call changes nothing."
                ),
            }

        # 1. The indentation column is a function of the AST depth, not of the
        #    bytes on disk: deriving it from the existing first line is what
        #    locked a wrongly indented block in place.
        indent_step = self.indent_mgr.detect_file_indent_step(file_bytes)
        ast_depth = self._ast_depth(sym)
        target_col = ast_depth * len(indent_step)

        # Re-base indentation to the target column and align newlines with the
        # file so that byte offsets recorded in the database stay coherent.
        rebased_code = self.indent_mgr.rebase_indentation(new_code, target_col, file_path=sym.file_path)
        rebased_code = self._match_newline(rebased_code, file_bytes)
        new_bytes = rebased_code.encode("utf-8")

        # 2. Syntax validation.
        #    The isolated snippet check is a diagnostic only: a legitimately
        #    indented method is never a valid module on its own, so the
        #    reconstructed whole-file check is the authoritative barrier.
        # The recorded range starts at the declaration, so an XML doc comment
        # above it is outside the range. When the caller submits a payload that
        # repeats that comment, extend the replacement over the existing doc
        # block instead of prepending a second copy of it.
        replace_start = sym.start_byte
        if self._starts_with_doc_comment(new_code):
            replace_start = self._leading_doc_start(file_bytes, sym.start_byte)

        _isolated_ok, isolated_errs = parser.validate_syntax(new_bytes, sym.file_path)

        spliced_bytes = file_bytes[:replace_start] + new_bytes + file_bytes[sym.end_byte :]
        spliced_ok, spliced_errs = parser.validate_syntax(spliced_bytes, sym.file_path)

        if not spliced_ok:
            # If rebased failed, attempt with raw new_code as provided
            raw_bytes = new_code.encode("utf-8")
            raw_spliced = file_bytes[:replace_start] + raw_bytes + file_bytes[sym.end_byte :]
            raw_ok, _raw_errs = parser.validate_syntax(raw_spliced, sym.file_path)
            if raw_ok:
                spliced_bytes = raw_spliced
                rebased_code = new_code
            else:
                err_objs = spliced_errs or isolated_errs
                # Region only when the errors come from the spliced file: the
                # isolated snippet numbers its own lines.
                region = self._line_region(spliced_bytes, replace_start, len(new_bytes)) if spliced_errs else None
                return self._syntax_error_payload(
                    "symbol_id",
                    symbol_id,
                    err_objs,
                    "Syntax validation failed. Operation rejected and file remains untouched.",
                    region=region,
                    touches_last_line=touches_last_line,
                    block_lines=block_lines,
                )

        if spliced_bytes == file_bytes:
            return {
                "status": "noop",
                "symbol_id": symbol_id,
                "reason": "identical_after_reindent",
                "indent": {"ast_level": ast_depth, "columns": target_col, "unit": indent_step},
                "message": (
                    "No change: your code, re-based to AST level " + str(ast_depth)
                    + " (" + str(target_col) + " columns), is identical to the stored block."
                ),
            }

        duplicate = self._duplicate_declaration(parser, sym.file_path, file_bytes, spliced_bytes)
        if duplicate:
            return self._reject_duplicate("symbol_id", symbol_id, duplicate)

        old_lines = sym.end_line - sym.start_line + 1
        new_lines = len(rebased_code.splitlines())
        diff_summary = f"- {old_lines} lines, + {new_lines} lines"

        # 3. Atomic Disk Write
        self._atomic_write(abs_path, spliced_bytes)

        # 4. Reparse and update database
        self._reindex(abs_path)

        call_rows = self.db.get_symbol_calls(symbol_id)
        reparsed_calls = [c["callee_name"] for c in call_rows]

        return {
            "status": "success",
            "symbol_id": symbol_id,
            "diff_summary": diff_summary,
            "indent": {"ast_level": ast_depth, "columns": target_col, "unit": indent_step},
            "validation": {
                "syntax_ok": True,
                "reparsed_calls": reparsed_calls,
            },
        }

    def _missing_file_refusal(self, parent_id: str, file_path: str) -> Dict[str, Any]:
        """Refuse an insertion into a file that does not exist, and say where to go.

        No tool creates a file. An agent that wants a new class tries it anyway: on
        mapab2 a r2 that cost three turns -- insert_block on '<new>.cs', an empty
        search and a read_block -- before it moved the class into an indexed file.
        The refusal names the files that can take it, and the namespace to use.
        """
        payload: Dict[str, Any] = {
            "status": "error",
            "parent_id": parent_id,
            "error": f"File '{file_path}' does not exist on disk.",
        }
        folder = str(Path(file_path).parent).replace("\\", "/")
        mapped = self.db.get_project_map(budget_tokens=1000, file_pattern=folder + "/*")
        candidates = [item for item in mapped["files"] if item["file_path"] != file_path]
        if not candidates:
            return payload
        names = [item["file_path"] for item in candidates[:5]]
        payload["files_that_can_take_it"] = names
        payload["error"] = (
            f"File '{file_path}' does not exist on disk, and no tool creates a file: a symbol "
            "is inserted into an existing one. Files here: " + ", ".join(names) + "."
        )
        holder = None
        for item in candidates:
            for name in item["symbols"]:
                kind, _, bare = str(name).partition(" ")
                if kind == "namespace":
                    holder = item["file_path"] + "::" + bare
                    break
            if holder:
                break
        payload["actionable_hint"] = (
            "Insert it into an existing file instead"
            + (
                ", in the namespace that file declares: insert_block(parent='" + holder + "', text=...)"
                if holder
                else ", for instance insert_block(parent='" + names[0] + "::<Name>', code=...)"
            )
            + ". The namespace of your code must match the file you choose."
        )
        return payload

    @staticmethod
    def _namespace_body_start(file_bytes: bytes, namespace: Any) -> int:
        """First byte INSIDE a namespace: after its declaration line, or after its brace."""
        start = int(namespace.start_byte)
        head = file_bytes[start : start + 400]
        brace = head.find(b"{")
        semicolon = head.find(b";")
        if brace != -1 and (semicolon == -1 or brace < semicolon):
            return start + brace + 1
        if semicolon != -1:
            line_end = file_bytes.find(b"\n", start + semicolon)
            return len(file_bytes) if line_end == -1 else line_end + 1
        return start

    def _file_scoped_namespace_body(self, parser: Any, file_path: str, file_bytes: bytes) -> Optional[int]:
        """Offset just after `namespace X;`, when the file uses a file-scoped namespace."""
        for symbol in parser.parse_file(file_path, file_bytes).symbols:
            if getattr(symbol, "kind", None) != "namespace":
                continue
            start = int(symbol.start_byte)
            line_end = file_bytes.find(b"\n", start)
            head = file_bytes[start : line_end if line_end != -1 else len(file_bytes)]
            if head.rstrip().endswith(b";"):
                return self._namespace_body_start(file_bytes, symbol)
        return None

    def insert_symbol(
        self,
        parent_id: str,
        code: str,
        position: str = "end",
        anchor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a new method, function, or class into an existing scope with auto-calibrated indentation."""
        if anchor in ("start", "end"):
            position = anchor
            anchor = None
        # The printed path (`fichier.cs::Classe::Methode`) is the surface's vocabulary: a parent
        # named that way must seat the child exactly like the stored id would. Without this,
        # `m.cs::N::C` resolved to NOTHING here -- the insert silently fell back to the FILE
        # route and wrote the member outside every type, which is how the torture pass found
        # `insert_block(parent="m.cs", ...)` answering success on invalid C# (defect 10).
        if "::" in parent_id:
            resolved = self._resolve_displayed_path(parent_id)
            if resolved:
                parent_id = resolved
        parent_sym = self.db.get_symbol(parent_id)
        if parent_sym:
            file_path = parent_sym.file_path
        else:
            file_path = parent_id.split("::")[0]

        anchor_sym = None
        if anchor:
            anchor_id = anchor
            if "::" in anchor_id:
                resolved_anchor = self._resolve_displayed_path(anchor_id)
                if resolved_anchor:
                    anchor_id = resolved_anchor
            anchor_sym = self.db.get_symbol(anchor_id)
            if not anchor_sym:
                return {
                    "status": "error",
                    "parent_id": parent_id,
                    "error": f"Anchor symbol '{anchor}' was not found in graph database.",
                    "actionable_hint": "Check the anchor address or omit anchor to insert at default position.",
                }
            if parent_sym:
                if anchor_sym.parent_id != parent_sym.id:
                    return {
                        "status": "error",
                        "parent_id": parent_id,
                        "error": f"Anchor '{anchor}' is not a direct child of container '{parent_id}'.",
                        "actionable_hint": "Specify an anchor symbol that belongs to this container, or omit anchor.",
                    }
            else:
                if anchor_sym.file_path != file_path or anchor_sym.parent_id is not None:
                    return {
                        "status": "error",
                        "parent_id": parent_id,
                        "error": f"Anchor '{anchor}' is not a top-level child of file '{file_path}'.",
                        "actionable_hint": "Specify an anchor symbol at the top level of this file, or omit anchor.",
                    }

        try:
            abs_path = self.config.to_absolute_path(file_path)
        except ValueError as exc:
            return {"status": "error", "parent_id": parent_id, "error": str(exc)}

        if not abs_path.exists():
            return self._missing_file_refusal(parent_id, file_path)

        file_bytes = abs_path.read_bytes()
        parser = self.registry.get_parser_for_file(file_path)
        if not parser:
            return {
                "status": "error",
                "parent_id": parent_id,
                "error": f"No AST parser available for file '{file_path}'.",
            }

        indent_step = self.indent_mgr.detect_file_indent_step(file_bytes)
        at_start = position == "start"

        if parent_sym and parent_sym.kind == "class":
            # Members sit one level below their container, computed from the AST
            # depth (not from the container's current first line, which may itself
            # be mis-indented). `dedent`, not `strip`: strip de-indents only the
            # first line, so re-basing then shifted every following line one level
            # too far -- the bug that produced 8-column members.
            target_col = (self._ast_depth(parent_sym) + 1) * len(indent_step)
            rebased_code = self.indent_mgr.rebase_indentation(textwrap.dedent(code), target_col, file_path=file_path)
            rebased_code = self._match_newline(rebased_code, file_bytes)
            gap = self._newline_bytes(file_bytes) * 2

            if anchor_sym is not None:
                insertion_byte = int(anchor_sym.end_byte)
                insert_bytes = gap + rebased_code.encode("utf-8")
            else:
                first_child = self.db.get_first_child_symbol(parent_sym.id) if at_start else None
                if first_child is not None:
                    insertion_byte = first_child.start_byte
                    insert_bytes = rebased_code.encode("utf-8") + gap
                else:
                    # "end" means the end of the *body*. Anchoring on the symbol's own
                    # end_byte placed the member just after the class' closing brace:
                    # orphaned code that tree-sitter accepts without an error. Anchor
                    # on the last child instead, and fall back to the closing brace.
                    last_child = self.db.get_last_child_symbol(parent_sym.id)
                    if last_child is not None:
                        insertion_byte = last_child.end_byte
                    else:
                        insertion_byte = parent_sym.end_byte
                        if insertion_byte > 0 and file_bytes[insertion_byte - 1 : insertion_byte] == b"}":
                            insertion_byte -= 1
                    insert_bytes = gap + rebased_code.encode("utf-8")
        else:
            target_col = 0
            rebased_code = self.indent_mgr.rebase_indentation(textwrap.dedent(code), target_col, file_path=file_path)
            rebased_code = self._match_newline(rebased_code, file_bytes)
            gap = self._newline_bytes(file_bytes) * 2

            if anchor_sym is not None:
                insertion_byte = int(anchor_sym.end_byte)
                insert_bytes = gap + rebased_code.encode("utf-8")
            elif parent_sym is not None and parent_sym.kind == "namespace":
                # A member cannot live before `namespace X;` nor outside the braces:
                # "start" means the namespace FIRST MEMBER, and a namespace that has
                # none takes the code right after its declaration.
                first_child = self.db.get_first_child_symbol(parent_sym.id) if at_start else None
                if first_child is not None:
                    insertion_byte = int(first_child.start_byte)
                    insert_bytes = rebased_code.encode("utf-8") + gap
                else:
                    insertion_byte = self._namespace_body_start(file_bytes, parent_sym)
                    insert_bytes = gap + rebased_code.encode("utf-8")
            elif self._is_using_directive(rebased_code):
                # A using directive has ONE legal place: ABOVE every other declaration. Appended
                # ("end"), it landed after the file-scoped namespace -- CS1529, the file stopped
                # compiling while the reply said success with syntax_ok=True, twice in one run
                # (base 160d773, t19 and t24, two builds lost). Targeting IS the point here: the
                # new directive joins its siblings instead of being dumped at the end.
                usings = [s for s in parser.parse_file(file_path, file_bytes).symbols if s.kind == "using"]
                if usings:
                    insertion_byte = int(usings[-1].end_byte)
                    insert_bytes = gap + rebased_code.encode("utf-8")
                else:
                    insertion_byte = 0
                    insert_bytes = rebased_code.encode("utf-8") + gap
            else:
                first_sym = self.db.get_first_file_symbol(file_path) if at_start else None
                if first_sym is not None:
                    insertion_byte = int(first_sym.start_byte)
                    insert_bytes = rebased_code.encode("utf-8") + gap
                else:
                    insertion_byte = len(file_bytes)
                    insert_bytes = gap + rebased_code.encode("utf-8")
                # No member may precede a FILE-SCOPED namespace declaration:
                # tree-sitter accepts it, the C# compiler does not (CS8956), and the
                # tool answered success with syntax_ok=True on mapab2 a r1 -- the build
                # failed two turns later, on code the agent had every reason to trust.
                body_start = self._file_scoped_namespace_body(parser, file_path, file_bytes)
                if body_start is not None and insertion_byte <= body_start:
                    insertion_byte = body_start

        spliced_bytes = file_bytes[:insertion_byte] + insert_bytes + file_bytes[insertion_byte:]
        spliced_ok, spliced_errs = parser.validate_syntax(spliced_bytes, file_path)

        if not spliced_ok:
            return self._syntax_error_payload(
                "parent_id",
                parent_id,
                spliced_errs,
                "Syntax validation failed for inserted symbol.",
                region=self._line_region(spliced_bytes, insertion_byte, len(insert_bytes)),
            )

        duplicate = self._duplicate_declaration(parser, file_path, file_bytes, spliced_bytes)
        if duplicate:
            return self._reject_duplicate("parent_id", parent_id, duplicate)

        # Post-condition: when inserting into a type, the new declaration must be
        # a member of that type. Tree-sitter does not flag a member dropped after
        # the closing brace, so the graph itself is the only reliable witness.
        if parent_sym is not None and parent_sym.kind in ("class", "struct", "interface", "record", "enum"):
            before_ids = {s.id for s in parser.parse_file(file_path, file_bytes).symbols}
            after_ids = {s.id for s in parser.parse_file(file_path, spliced_bytes).symbols}
            new_ids = after_ids - before_ids
            if new_ids and not any(identifier.startswith(parent_sym.id + ".") for identifier in new_ids):
                return {
                    "status": "error",
                    "parent_id": parent_id,
                    "error": (
                        "Insertion refused: the declaration would land outside '" + parent_sym.id
                        + "'. The file is left untouched."
                    ),
                    "actionable_hint": (
                        "Insert into the container itself (position start or end of its body), "
                        "not after its closing brace."
                    ),
                }
        # Post-condition: a MEMBER declaration needs a container. Under a code file (or a
        # bare namespace) it lands outside every type -- tree-sitter parses it, the C#
        # compiler does not, and the tool answered success with syntax_ok=True and NO id
        # (torture, defect 10: `insert_block(parent="m.cs", text="public void NewMethod()")`
        # wrote a method after the namespace's closing brace and called it a success). Only
        # a parser that separates containers from members can judge this: in Python a
        # top-level `def` under a file is perfectly legal, so the check follows the parser.
        member_kinds = getattr(parser, "MEMBER_KINDS", None)
        if member_kinds and (parent_sym is None or parent_sym.kind == "namespace"):
            before_ids = {s.id for s in parser.parse_file(file_path, file_bytes).symbols}
            spliced_symbols = parser.parse_file(file_path, spliced_bytes).symbols
            containers = [
                self.displayed_symbol_id(s.id)
                for s in spliced_symbols
                if s.kind in getattr(parser, "CONTAINER_KIND_NAMES", ())
            ]
            # The parser states the tree: a member that landed inside a type carries that
            # type as its parent, and one that landed beside it carries none.
            orphans = [
                s for s in spliced_symbols
                if s.id not in before_ids and s.kind in member_kinds.values() and not s.parent_id
            ]
            if orphans:
                listed = ", ".join(sorted(set(containers))[:4]) or "this file holds no container"
                return {
                    "status": "error",
                    "parent_id": parent_id,
                    "error": (
                        "Insertion refused: '" + orphans[0].name + "' is a MEMBER declaration and '"
                        + parent_id + "' is not a container, so it would have landed outside every "
                        "type -- invalid C# the parser still accepts. The file is left untouched."
                    ),
                    "actionable_hint": "Name the container instead: " + listed + ".",
                }

        new_lines = len(rebased_code.splitlines())
        diff_summary = f"+ {new_lines} lines"

        self._atomic_write(abs_path, spliced_bytes)
        self._reindex(abs_path)

        payload = {
            "status": "success",
            "parent_id": parent_id,
            "diff_summary": diff_summary,
            "indent": {
                "ast_level": target_col // len(indent_step) if indent_step else 0,
                "columns": target_col,
                "unit": indent_step,
            },
            "validation": {"syntax_ok": True},
        }
        # The created SYMBOL, addressed and hashed. On run 1 of base 30d62b4 the agent inserted a
        # member, got no id and no hash back, and read the whole enclosing class to find the name it
        # needed as the next anchor; the reply now names what it made, so the next call can use it.
        created_rows = [
            row
            for row in self.db.get_symbols_for_file(file_path)
            if insertion_byte <= int(row.get("start_byte") or 0) < insertion_byte + len(insert_bytes)
        ]
        if created_rows:
            first_id = str(created_rows[0]["id"])
            payload["created_symbol_id"] = self.displayed_symbol_id(first_id)
            payload["created_symbol_ids"] = [
                self.displayed_symbol_id(str(row["id"])) for row in created_rows
            ]
            state = self.block_metrics(first_id)
            if state is not None:
                payload["line_count"] = state["line_count"]
                payload["numbered_text"] = state["numbered_text"]
        return payload

    def delete_symbol(self, symbol_id: str) -> Dict[str, Any]:
        """Delete a function, method, or class cleanly from the AST and file."""
        sym = self.db.get_symbol(symbol_id)
        if not sym:
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": f"Symbol '{symbol_id}' not found in graph database.",
            }

        try:
            abs_path = self.config.to_absolute_path(sym.file_path)
        except ValueError as exc:
            return {"status": "error", "symbol_id": symbol_id, "error": str(exc)}

        if not abs_path.exists():
            return {
                "status": "error",
                "symbol_id": symbol_id,
                "error": f"File '{sym.file_path}' does not exist.",
            }

        if sym.kind == "namespace":
            return self._reject_namespace_mutation("a deletion", sym)

        file_bytes = abs_path.read_bytes()
        parser = self.registry.get_parser_for_file(sym.file_path)

        # Remove the symbol's documentation comment together with its
        # declaration; otherwise the comment is left orphaned in the file.
        del_start = self._leading_doc_start(file_bytes, sym.start_byte)
        del_end = sym.end_byte
        if del_end < len(file_bytes) and file_bytes[del_end : del_end + 1] == b"\n":
            del_end += 1

        spliced_bytes = file_bytes[:del_start] + file_bytes[del_end:]

        # Collapse an excessive run of blank lines left behind (best effort).
        spliced_text = spliced_bytes.decode("utf-8", errors="replace")
        normalized_text = re.sub(r"\n{4,}", "\n\n\n", spliced_text)
        spliced_bytes = normalized_text.encode("utf-8")

        if parser:
            spliced_ok, spliced_errs = parser.validate_syntax(spliced_bytes, sym.file_path)
            if not spliced_ok:
                return self._syntax_error_payload(
                    "symbol_id",
                    symbol_id,
                    spliced_errs,
                    "Syntax validation failed after deleting symbol.",
                )

        self._atomic_write(abs_path, spliced_bytes)
        self._reindex(abs_path)

        return {
            "status": "success",
            "symbol_id": symbol_id,
            "diff_summary": f"- {sym.end_line - sym.start_line + 1} lines",
            "validation": {"syntax_ok": True},
        }

    # ------------------------------------------------------------------
    # Document mutations (Markdown)
    # ------------------------------------------------------------------
    MARKDOWN_EXTENSIONS = (".md", ".markdown", ".mdx")
    MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

    def _is_markdown(self, file_path: str) -> bool:
        return file_path.lower().endswith(self.MARKDOWN_EXTENSIONS)

    @staticmethod
    def _to_document_text(text: str, newline: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if newline == "\r\n":
            return normalized.replace("\n", "\r\n")
        return normalized

    def _resolve_markdown(self, file_path: str):
        try:
            abs_path = self.config.to_absolute_path(file_path)
        except ValueError as exc:
            return None, None, None, {"status": "error", "file_path": file_path, "error": str(exc)}
        if not abs_path.exists():
            return None, None, None, {
                "status": "error",
                "file_path": file_path,
                "error": f"File '{file_path}' does not exist.",
            }
        file_bytes = abs_path.read_bytes()
        return abs_path, file_bytes.decode("utf-8", errors="replace"), self._newline_bytes(file_bytes).decode("ascii"), None

    @classmethod
    def _leading_markdown_heading(cls, text: str):
        """Return the match of the first non-blank line when it is an ATX heading."""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            return cls.MARKDOWN_HEADING.match(stripped)
        return None

    @staticmethod
    def _parent_scope(layout, position: int, level: int):
        """Index of the nearest preceding heading of a strictly lower level."""
        for j in range(position - 1, -1, -1):
            if int(layout[j]["level"]) < level:
                return layout[j]["index"]
        return None

    def _sibling_clash(self, layout, heading: str, level: int, position: int, exclude_index=None):
        """Return an existing *sibling* section with the same title and level.

        Only sections sharing the same parent scope clash. Changelogs legitimately
        repeat '### New Features' under each released version, so a document-wide
        name check would refuse valid edits.
        """
        normalized = heading.strip().lower()
        scope = self._parent_scope(layout, position, level)
        for j, item in enumerate(layout):
            if exclude_index is not None and item["index"] == exclude_index:
                continue
            if int(item["level"]) != int(level) or item["heading"].strip().lower() != normalized:
                continue
            if self._parent_scope(layout, j, level) == scope:
                return item
        return None

    @staticmethod
    def _reject_duplicate_heading(identifier_key: str, identifier: str, heading: str, level: int) -> Dict[str, Any]:
        return {
            "status": "error",
            identifier_key: identifier,
            "error": (
                "Mutation refused: a section titled '" + heading + "' (level " + str(level)
                + ") already exists in this document. The file is left untouched."
            ),
            "actionable_hint": (
                "Update the existing section instead of adding a second heading with the same title, "
                "or choose a distinct title."
            ),
        }

    @staticmethod
    def _heading_mismatch(section_id: str, heading: str, expected_heading: Optional[str]) -> Optional[Dict[str, Any]]:
        """Refuse an operation whose expected heading does not match the target.

        An identifier is the PATH of a heading, so it designates one paragraph by name:
        the mismatch means that paragraph was renamed since the caller read it. Passing
        the expected heading turns a write under a stale name into a clear refusal.
        """
        expected = (expected_heading or "").strip()
        if not expected or heading.strip().lower() == expected.lower():
            return None
        return {
            "status": "error",
            "section_id": section_id,
            "error": (
                "Section '" + section_id + "' now holds '" + heading.strip() + "', not '" + expected
                + "'. An identifier is the path of a heading, so this paragraph was renamed since "
                "it was read. The file is left untouched."
            ),
            "heading": heading.strip(),
            "actionable_hint": "Re-read the section list, then retry with the current identifier.",
        }

    @staticmethod
    def _trailing_gap(text: str, end: int) -> str:
        """The newline run that already sits immediately before `end`.

        The caller replaces a BLOCK, not the spacing after it. Normalising that gap to
        one blank line made a one-line preamble edit come back with a different
        line_count: the file gained a line nobody wrote, and the agent had to re-read to
        find out why (found by the happy-path verifier).
        """
        cursor = max(0, int(end))
        while cursor > 0 and text[cursor - 1] in ("\r", "\n"):
            cursor -= 1
        return text[cursor:int(end)]

    def update_document_root(self, document_id: str, new_content: str = "") -> Dict[str, Any]:
        """Replace the document's own text: the part no heading owns.

        A file is a block. Its identifier is its path; its content is the preamble --
        or the whole file when it has no heading at all -- and its children are the
        sections. Editing that text is an ordinary block edit; adding a heading to it
        is not, because a heading is a child node, so it is refused and redirected to
        insert_block, which numbers it and keeps the outline consistent.
        """
        if not self._is_markdown(document_id):
            return {
                "status": "error",
                "document_id": document_id,
                "error": "Only Markdown documents (.md, .markdown, .mdx) can be edited; Word and PDF are read-only.",
            }
        abs_path, text, newline, error = self._resolve_markdown(document_id)
        if error:
            error["document_id"] = document_id
            return error
        headings = self._content_headings(new_content)
        if headings:
            first = headings[0]
            return {
                "status": "error",
                "document_id": document_id,
                "error": (
                    "Refused: the document's own text carries no heading, so '" + str(first["title"])
                    + "' (level " + str(first["level"]) + ") would open a new paragraph. The file is "
                    "left untouched."
                ),
                "actionable_hint": (
                    "Add it with insert_block(parent='" + document_id
                    + "', anchor=<a child id, or the parent itself>, text='<title>\\n<body>'): the document "
                    "itself is a parent, and its children are its top-level sections."
                ),
            }
        end = self._document_root_end(text)
        body = self._to_document_text(new_content, newline)
        if body:
            gap = self._trailing_gap(text, end)
            separator = gap if gap else ((newline * 2) if end < len(text) else newline)
            new_text = body + separator + text[end:]
        else:
            new_text = text[end:]
        if new_text == text:
            return {
                "status": "noop",
                "document_id": document_id,
                "message": "Submitted text is identical to the document's own text; nothing to do.",
            }
        self._atomic_write(abs_path, new_text.encode("utf-8"))
        self._reindex(abs_path)
        metrics = self.block_metrics(document_id) or {}
        return {
            "status": "success",
            "document_id": document_id,
            "file_path": document_id,
            "new_lines": len(body.splitlines()) if body else 0,
            "content_hash": metrics.get("content_hash"),
            "line_count": metrics.get("line_count"),
            "numbered_text": metrics.get("numbered_text"),
        }


    def update_file_text(self, file_path: str, new_content: str = "") -> Dict[str, Any]:
        """Replace the whole content of a file that has no AST symbols."""
        try:
            abs_path = self.config.to_absolute_path(file_path)
        except ValueError as exc:
            return {"status": "error", "file_path": file_path, "error": str(exc)}
        if not abs_path.exists():
            return {
                "status": "error",
                "file_path": file_path,
                "error": f"File '{file_path}' does not exist on disk.",
            }
        new_bytes = new_content.encode("utf-8")
        self._atomic_write(abs_path, new_bytes)
        self._reindex(abs_path)
        metrics = self.block_metrics(file_path) or {}
        return {
            "status": "success",
            "block_id": file_path,
            "file_path": file_path,
            "new_lines": len(new_content.splitlines()),
            "content_hash": metrics.get("content_hash", compute_content_hash(new_bytes)),
            "line_count": metrics.get("line_count", len(new_content.splitlines())),
            "numbered_text": metrics.get("numbered_text", numbered(new_content, 1)),
        }

    def update_document_section(
        self,
        section_id: str,
        new_content: str = "",
        expected_heading: Optional[str] = None,
        renumber_following: bool = False,
        edits: Optional[List[Dict[str, Any]]] = None,
        additions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Replace one Markdown paragraph body, keeping its heading.

        `additions` is edit_block's own record of what its operations INSERTED (see
        _added_fragments): it is what lets a call that only ADDS a paragraph at the
        block's own level CREATE it instead of being refused. A caller that submits
        content without operations leaves it None, and every guard below is then exactly
        the one it has always been.
        """
        section = self.db.get_document_section(section_id)
        if not section:
            return {
                "status": "error",
                "section_id": section_id,
                "error": f"Section '{section_id}' not found in graph database.",
            }

        file_path = section["document_id"]
        if not self._is_markdown(file_path):
            return {
                "status": "error",
                "section_id": section_id,
                "error": "Only Markdown documents (.md, .markdown, .mdx) can be edited in place; Word and PDF are read-only.",
            }

        abs_path, text, newline, error = self._resolve_markdown(file_path)
        if error:
            error["section_id"] = section_id
            return error

        layout = markdown_layout(text)
        target = next((item for item in layout if item["index"] == section["order_index"]), None)
        if target is None:
            return {
                "status": "error",
                "section_id": section_id,
                "error": "Section no longer exists in the file; re-run search_documents.",
            }

        mismatch = self._heading_mismatch(section_id, str(target["heading"]), expected_heading)
        if mismatch is not None:
            return mismatch

        # Fragment mode. Markdown indentation is content (lists, code fences), so
        # the replacement is literal; only the matching ignores spacing.
        if edits:
            block = text[target["section_start"] : target["section_end"]]
            applied = apply_edits(block, edits, lambda fragment, _column: fragment)
            if applied.error:
                error = dict(applied.error)
                error["section_id"] = section_id
                return error
            new_content = applied.text
        elif not new_content.strip():
            return {
                "status": "error",
                "section_id": section_id,
                "error": "Nothing to write: pass new_content (whole paragraph) or edits (fragments).",
                "actionable_hint": (
                    "Send new_content with the whole replacement paragraph, or edits=[{replace:[a,b],"
                    "content:'...'}] for a fragment. An empty call changes nothing."
                ),
            }

        gap = self._trailing_gap(text, int(target["section_end"]))
        separator = gap if gap else (newline + newline if target["section_end"] < len(text) else newline)
        body = self._to_document_text(new_content, newline)
        submitted = self._leading_markdown_heading(new_content)
        block_level = len(submitted.group(1)) if submitted is not None else int(target["level"])
        # A call that only ADDS a paragraph at the block's own level CREATES it: see
        # _title_route. The decision is taken before the refusal, and only for that shape.
        routed = self._title_route(
            target, body, block_level, section_id, file_path, text, layout, additions
        )
        if routed is not None:
            return routed
        new_heading: Optional[str] = None
        new_level: Optional[int] = None
        position: Optional[int] = None
        if submitted is not None:
            new_heading = submitted.group(2)
            new_level = len(submitted.group(1))
            position = next(
                (j for j, item in enumerate(layout) if item["index"] == target["index"]), len(layout)
            )
        guard_context: Dict[str, Any] = {
            "target": target,
            "body": body,
            "block_level": block_level,
            "file_path": file_path,
            "layout": layout,
            "new_heading": new_heading,
            "new_level": new_level,
            "position": position,
            # The section being WRITTEN is the one that carries the new title, so the
            # duplicate search must not find it: it is excluded by its identity.
            "exclude_index": target["index"],
            # edit_block's record of what its operations only ADDED (None when it also rewrites):
            # what lets the outline guard serve several nested titles added at the block's end.
            "additions": additions,
        }
        # The layout-based guards, BEFORE the "nothing to change" shortcut: `names` keeps
        # the ORDER of the refusals exactly what it has always been.
        refusal = self._result_guards_refusal(
            text,
            None,
            section_id,
            guard_context,
            ("outline_of_the_edited_block", "duplicate_sibling_title"),
        )
        if refusal is not None:
            return refusal
        if submitted is not None:
            # The caller submitted a heading. Replace the whole section (heading
            # included) instead of nesting a second heading under the existing
            # one: the body-only splice used to leave an empty section behind and
            # shift every following section index.
            new_text = text[: target["section_start"]] + body + separator + text[target["section_end"] :]
        else:
            # Keep the conventional blank line between the heading and its body.
            replacement = newline + body + separator
            new_text = text[: target["body_start"]] + replacement + text[target["section_end"] :]

        if new_text == text:
            return {
                "status": "noop",
                "section_id": section_id,
                "message": "Submitted content is identical to the current section; nothing to do.",
            }

        # ...and the text-based guard, on the WHOLE result. Same entry point, same registry.
        damaged = self._result_guards_refusal(text, new_text, section_id, guard_context)
        if damaged is not None:
            return damaged

        # The region this write replaced, in the coordinates of the file. The
        # suffix is copied verbatim, so its length pins the end exactly -- commas
        # and CRLF included. It is needed to tell the sections the write created
        # from the ones that were already there.
        region_start = target["section_start"] if submitted is not None else target["body_start"]
        region_end = len(new_text) - len(text[target["section_end"] :])

        renumbered: List[str] = []
        if renumber_following and submitted is not None:
            inserted_number = re.match(r"^(\d+)[.)]", new_heading.strip())
            if inserted_number:
                new_text, renumbered = self._renumber_following(
                    new_text, target["section_start"], new_level, int(inserted_number.group(1))
                )

        before_ids = {row["id"] for row in self.db.get_document_sections(file_path)}
        self._atomic_write(abs_path, new_text.encode("utf-8"))
        self._reindex(abs_path)

        level = new_level if submitted is not None else int(target["level"])
        heading = new_heading if submitted is not None else str(target["heading"])
        if submitted is not None and self._block_heading_was_not_resubmitted(text, target, body):
            # The submitted heading is NOT this block's. An insertion that lands BEFORE
            # the block's own heading becomes the first heading of the result (measured:
            # `insert_after 0`), and the reply then announced the block's heading AS the
            # inserted one while the block's own heading and own text were both still
            # there. The field says what the block HAS, so it is read, not assumed.
            heading = str(target["heading"])
        after_layout = markdown_layout(new_text)
        created_ids, split_ids = self._created_sections(
            after_layout, file_path, region_start, region_end, before_ids, level
        )
        warnings = self._structure_warnings(after_layout, target["section_start"], level, heading)
        if split_ids:
            warnings.append(
                "your content opens a new section of the same level (or a higher one), which is "
                "therefore NOT part of the block you edited any more: " + "; ".join(split_ids)
                + ". The numbered_text returned above covers only '" + section_id
                + "'; edit the new section under its own identifier."
            )
        return {
            "status": "success",
            "section_id": section_id,
            "file_path": file_path,
            # The heading the block has NOW, not the one it had before the edit: an
            # agent that retitled a paragraph to drop its number read the old heading
            # here, concluded its fix had not taken effect, and only found out from a
            # later search_documents (declared on mapab2 b r1 of the base a619198).
            "heading": heading,
            "new_lines": len(body.splitlines()),
            # A heading inside the new content splits the section: the caller needs
            # the identifiers that now exist, and any shift of the following ones.
            "resulting_section_ids": self._section_ids_from(
                after_layout,
                file_path,
                target["section_start"],
                level,
            ),
            # Sub-sections are reported by resulting_section_ids; a heading that
            # splits the block into *siblings* is not, because the walk stops at
            # the first section of the same level. Measured on a real run: the
            # agent inserted '### 9.' inside '### 8.', got back the 4 lines of
            # section 8, believed its content had been truncated, and called
            # insert_block -- which rightly refused, since section 9
            # already existed. Six turns lost to a missing identifier.
            "created_section_ids": created_ids,
            "warnings": warnings,
        }

    @classmethod
    def _content_headings(cls, text: str) -> List[Dict[str, Any]]:
        """Headings (level + title) present in a fragment, in document order.

        Fenced code blocks are skipped: a `#` inside a shell snippet is a comment, not
        a new paragraph, and reading it as one refused edits that changed no heading.
        """
        return [
            {"level": level, "title": title, "raw": raw}
            for _number, level, title, raw in heading_lines(text.splitlines())
        ]

    @staticmethod
    def _one_insertion(before: List[int], after: List[int]) -> bool:
        """True when `after` is `before` plus exactly one extra level."""
        if len(after) != len(before) + 1:
            return False
        return any(after[:index] + after[index + 1 :] == before for index in range(len(after)))

    def _only_adds_nested_titles_at_the_end(
        self,
        before: List[int],
        current: List[int],
        additions: Optional[List[Dict[str, Any]]],
        text: str,
        target: Dict[str, Any],
    ) -> bool:
        """True when a call ADDS several nested titles at the END of the block and writes nothing else.

        The outline guard let ONE nested title through and refused two or more (measured: 8 calls
        on the campaign, one turn each, e.g. `insert_after 3` of two `###` titles under a `##`).
        Several titles written INSIDE the block are the same act as one: the text lands verbatim
        and the sections they open are children of the block. What makes it safe is everything
        below, and any doubt keeps the refusal:
          * `additions` exists, so every operation of the call is an insertion (a replace or a
            delete anywhere makes edit_block pass None);
          * each insertion leaves the block's other text untouched (`without` equals the block);
          * each insertion that carries a title lands at the block's END -- only blank lines of
            the block follow it -- and not inside a code fence it leaves open;
          * the outline of the result is the outline of the block plus those titles, in order;
          * no title written is already carried by the block, and no two written titles are the
            same line (a duplicate would be silently merged by the reader below).
        """
        if not additions or len(current) <= len(before) + 1:
            return False
        written: List[str] = []
        for fragment in additions:
            if str(fragment.get("without")) != str(fragment.get("block")):
                return False
            headings = self._content_headings(str(fragment.get("text") or ""))
            if not headings:
                continue
            if not fragment.get("at_end") or fragment.get("at_fence"):
                return False
            written.extend(str(item.get("raw", "")) for item in headings)
        if len(written) != len(current) - len(before) or len(set(written)) != len(written):
            return False
        # `before` must be a subsequence of `current`: nothing of the block's own outline moved.
        cursor = 0
        for level in current:
            if cursor < len(before) and before[cursor] == level:
                cursor += 1
        if cursor != len(before):
            return False
        span = text[target["section_start"] : target["section_end"]]
        carried = {str(item.get("raw", "")) for item in self._content_headings(span)}
        return not any(raw in carried for raw in written)

    @classmethod
    def _added_fragments(
        cls, ops: List[Dict[str, Any]], content: str
    ) -> Optional[List[Dict[str, Any]]]:
        """What each ADDITION of `ops` inserts, and the block with the others applied.

        An insertion cannot remove a line, so a heading one of them carries is a paragraph
        the caller CREATED rather than a piece of the block it sits beside. `without` is the
        block with every OTHER operation applied, which is what tells _title_route whether
        the call also rewrites the block. None as soon as one op replaces or deletes a
        range: that one can remove the block's own heading, and the text left behind would
        no longer be the block -- so the refusal stays, unchanged.
        """
        if not ops or cls._has_range_op(ops):
            return None
        baseline = apply_ops(content, [], lambda fragment, _column: fragment).text
        lines = content.splitlines()
        fragments: List[Dict[str, Any]] = []
        for index, op in enumerate(ops):
            if not isinstance(op, dict) or "insert_after" not in op:
                return None
            others = apply_ops(
                content,
                list(ops[:index]) + list(ops[index + 1 :]),
                lambda fragment, _column: fragment,
            )
            if others.error:
                return None
            position = int(op["insert_after"])
            fragments.append({
                "text": str(op.get("content") or ""),
                "without": others.text,
                "block": baseline,
                "position": position,
                "lines": len(lines),
                # The block's OWN lines the new ones would jump over. Only blank ones
                # may: a paragraph at this level is seated AFTER the block, so any
                # other line still to come means the place asked for is not honoured.
                "at_end": not any(line.strip() for line in lines[position:]),
                # `insert_after n` lands after the n first lines: when they leave a fence
                # OPEN, the new lines are CODE inside it and no title can be read there.
                "at_fence": unclosed_fence_line("\n".join(lines[:position])) is not None,
            })
        return fragments

    def _authored_headings(
        self, body: str, target: Dict[str, Any], block_level: int, text: str
    ) -> List[Dict[str, Any]]:
        """Headings a submitted `body` WRITES, read from the RESULT and nothing else.

        The question this guard answers is "does the edit split the block into two
        sections", and the only text that answers it is the outline the edit PRODUCES --
        never the raw text, and never the inserted fragment read on its own. Both guards
        read it HERE, so they cannot answer it differently: a heading line inside a fence
        is code, and `_content_headings` is the only reader that knows it. Measured by the
        verifier: a `### 9. Boss` sent after the OPENING fence of a block was read as a
        title, a real section was created elsewhere, and the caller's code was written
        nowhere at all.

        The single tolerance is the REPAIR of an unbalanced fence: while the block's own
        span reads as unclosed, the headings after the stray marker are hidden code, and
        the edit that removes the marker EXPOSES them instead of writing them (measured on
        mapab2 a r3: refusing that one-line repair cost 50 turns and max_turns). Everywhere
        else the block is read as Markdown -- so a title equal to a line the block carries
        inside a CLOSED fence is genuinely NEW, and treating it as already there let the old
        path split the block and leave the same title twice in the document.
        """
        after = self._content_headings(body)
        if after and int(after[0]["level"]) == int(block_level):
            after = after[1:]  # the block resubmitted with its own heading
        span = text[target["section_start"] : target["section_end"]]
        if unclosed_fence_line(span) is not None:
            exposed = {
                line.strip()
                for line in span.splitlines()
                if self.MARKDOWN_HEADING.match(line.strip())
            }
        else:
            exposed = {str(item.get("raw", "")) for item in self._content_headings(span)}
        return [item for item in after if str(item.get("raw", "")) not in exposed]

    #: The guards an edit must pass ON THE TEXT IT WOULD PRODUCE. `update_document_section`
    #: applies each at the point named below; `_title_route` applies them to a DRY RUN of the
    #: document it proposes to write, so a route that writes through ANOTHER path
    #: (insert_block) cannot reach a state the ordinary path refuses. The first revision of
    #: the route decided BEFORE these guards and wrote a text whose code fence was left open:
    #: '10. Next Section' stopped being a section, which HEAD refuses and which the ordinary
    #: path still refuses -- the route was the only way in.
    #:
    #: WHERE EACH ONE IS IN THE ORDINARY PATH, and how the route gets it:
    #:   unclosed_code_fence        -- update_document_section, `_result_guards_refusal`
    #:                                 called on the proposal by BOTH paths.
    #:   duplicate_sibling_title    -- update_document_section, `_sibling_clash` ->
    #:                                 `_reject_duplicate_heading`; the route gets it from
    #:                                 insert_block, which runs those same two helpers.
    #:   outline_of_the_edited_block -- update_document_section, `_outline_refusal`; the route
    #:                                 gets it from `_authored_headings`, read on the block's
    #:                                 content IN THE RESULT, which the route requires to be
    #:                                 the block's own content (see `_title_route`).
    #: tests/test_document_guards.py walks this tuple and requires BOTH paths to refuse a
    #: scenario per guard, so a name added here without one fails.
    def _guard_outline_of_the_edited_block(self, text, proposed, section_id, context):
        """update_document_section: `_outline_refusal`, on the block's content IN THE RESULT.

        The route feeds `body` = the block WITHOUT the paragraph it is lifting out, so the
        guard reads the outline the block really keeps, and it is satisfied by construction
        when that content is the block's own (which the route requires).
        """
        body = context.get("body")
        if body is None:
            return None
        return self._outline_refusal(
            context["target"],
            body,
            context["block_level"],
            section_id,
            context["file_path"],
            text,
            context["layout"],
            additions=context.get("additions"),
        )

    def _guard_duplicate_sibling_title(self, text, proposed, section_id, context):
        """update_document_section: `_sibling_clash` -> `_reject_duplicate_heading`.

        Only a section sharing the SAME parent scope is a conflict; a changelog repeats
        '### New Features' under each release and that is legitimate. The section being
        WRITTEN is excluded from the search -- it is the one that carries the new title.
        """
        if context.get("new_heading") is None:
            return None
        clash = self._sibling_clash(
            context["layout"],
            context["new_heading"],
            context["new_level"],
            context["position"],
            context.get("exclude_index"),
        )
        if clash is None:
            return None
        return self._reject_duplicate_heading(
            "section_id", section_id, context["new_heading"], context["new_level"]
        )

    def _guard_unclosed_code_fence(self, text, proposed, section_id, context):
        """update_document_section: the unclosed-fence refusal, on the WHOLE result.

        An unclosed code fence runs to the end of the document, so every heading after it
        stops being a heading and the rest of the file stops being sections. Measured on
        mapab2 a r3: one edit left a stray ``` and the section went from 61 to 826 lines,
        every later repair was refused, and the run died at max_turns. Measured again on the
        first revision of _title_route, which decided before this guard existed: it wrote
        the fence, answered success, and '10. Next Section' left the outline.

        A fence that was ALREADY unclosed is not this edit's doing, so it is not refused
        here: the refusal is owed to the edit that OPENS one.
        """
        if proposed is None:
            return None
        unclosed = unclosed_fence_line(proposed)
        if unclosed is not None and unclosed_fence_line(text) is None:
            return {
                "status": "error",
                "section_id": section_id,
                "error": (
                    "Refused: this edit leaves the code fence opened at line " + str(unclosed)
                    + " unclosed. Every heading after it would stop being Markdown and be read as "
                    "code, so the rest of the document would stop being sections. The file is left "
                    "untouched."
                ),
                "actionable_hint": (
                    "Close the fence inside your content (a line of three backticks, or three "
                    "tildes), or send that line alone with {\"replace\": [n, n], "
                    "\"content\": \"```\"}."
                ),
            }
        return None

    #: THE GUARDS OF A RESULT, AND THE ONE PLACE THEY LIVE. The map IS the registry:
    #: `RESULT_GUARDS` derives from it, so a name can never be declared without an
    #: implementation, and `_result_guards_refusal` -- called by update_document_section AND
    #: by _title_route -- is what runs them. A guard added here is applied by BOTH paths;
    #: there is no way to implement one on one side only, which is what the previous
    #: revision of this comment claimed and could not prove.
    RESULT_GUARD_RUNNERS: Dict[str, Any] = {
        "outline_of_the_edited_block": _guard_outline_of_the_edited_block,
        "duplicate_sibling_title": _guard_duplicate_sibling_title,
        "unclosed_code_fence": _guard_unclosed_code_fence,
    }
    RESULT_GUARDS: Tuple[str, ...] = tuple(RESULT_GUARD_RUNNERS)

    def _result_guards_refusal(
        self,
        text: str,
        proposed: Optional[str],
        section_id: str,
        context: Optional[Dict[str, Any]] = None,
        names: Optional[Tuple[str, ...]] = None,
    ) -> Optional[Dict[str, Any]]:
        """EVERY guard of RESULT_GUARDS, applied to the text an edit would produce.

        ONE entry point, called by update_document_section and by _title_route; the runners
        of RESULT_GUARD_RUNNERS are the implementations, so a guard cannot be added to one
        side only. `names` lets a caller apply a SUBSET where its own order requires it --
        the ordinary path checks the layout-based guards before its 'nothing to change'
        shortcut and the text-based one after -- while the route applies them ALL, in the
        declared order, on a dry run of what it proposes.
        """
        context = dict(context or {})
        for name in names if names is not None else self.RESULT_GUARDS:
            runner = self.RESULT_GUARD_RUNNERS.get(name)
            if runner is None:
                # Unreachable while RESULT_GUARDS derives from the map; a test pins that.
                continue
            refusal = runner(self, text, proposed, section_id, context)
            if refusal is not None:
                return refusal
        return None

    def _dry_run_the_write(
        self, holder: str, section_id: str, text: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """(what insert_block ANSWERS, the document it WOULD write) -- nothing is written.

        `_atomic_write` is the one choke point every write goes through, so a dry run is
        both exact and cheap: the bytes captured here are the bytes the real call writes.
        The payload comes back too, because a refusal of insert_block's (a title a sibling
        already carries) is MORE precise than the outline refusal that would replace it,
        and the route must hand over the better answer, not a weaker one.
        """
        captured: List[bytes] = []
        self._capture_writes = captured
        try:
            payload = self.insert_block(parent_id=holder, where=section_id, text=text)
        finally:
            self._capture_writes = None
        proposed = captured[-1].decode("utf-8", errors="replace") if captured else None
        return payload, proposed

    @staticmethod
    def _where_the_lines_landed(ops: Any, content: str) -> str:
        """Where an all-insertion call put its lines relative to the block, as PROVEN.

        The reply said "the insert landed after its LAST line" whatever the call had asked
        for: with `insert_after 0` the lines land BEFORE the block's first line, and the
        sentence asserted a fact the reply had not checked. `insert_after 0` is the one
        position that lands above the block; a position no non-blank line of the block
        follows lands below it; anything else did not leave the block's text alone, and this
        reply is not emitted for it. A wording that guesses is the class of defect this
        depot has paid for twice.
        """
        positions = [
            int(op["insert_after"])
            for op in ops or []
            if isinstance(op, dict) and "insert_after" in op
        ]
        if not positions:
            return "outside it"
        if max(positions) == 0:
            return "BEFORE its first line"
        lines = content.splitlines()
        if not any(line.strip() for line in lines[max(positions):]):
            return "after its LAST line"
        return "outside it"

    @staticmethod
    def _without_line_numbers(numbered_text: str) -> str:
        """The text of a numbered block WITHOUT its 'N | ' prefixes -- numbered() undone.

        render_edit_snippet numbers the lines it prints. Handed a block's numbered_text it numbered
        them a second time ('+ 1 | 1 | def b():'): the model read two sets of numbers on one line.
        """
        import re

        return "\n".join(
            re.sub(r"^\s*\d+ \| ?", "", line, count=1) for line in numbered_text.splitlines()
        )

    def _block_heading_was_not_resubmitted(
        self, text: str, target: Dict[str, Any], body: str
    ) -> bool:
        """True when the leading heading of `body` is NOT the block's own heading.

        The block's own heading line is looked for AFTER the first heading of the result:
        while it is still there, the first one was INSERTED before it, and the block has
        not been retitled -- which is the only thing that lets `heading` be `new_heading`.
        """
        own = self._leading_markdown_heading(
            text[target["section_start"] : target["section_end"]]
        )
        if own is None:
            return False
        own_line = own.group(0).strip()
        written = self._content_headings(body)
        return any(str(item.get("raw", "")) == own_line for item in written[1:])

    def _title_route(
        self,
        target: Dict[str, Any],
        body: str,
        block_level: int,
        section_id: str,
        file_path: str,
        text: str,
        layout: List[dict],
        additions: Optional[List[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        """Create the sibling a call of ADDITIONS only asked for, instead of refusing it.

        edit_block edits inside one node, and a heading at the block's OWN level belongs to
        the block's holder: the refusal sent the caller to insert_block for it. On
        registered_v2 that cost one turn in 13 runs out of 20, and every one of the 13 was
        repaired on the very next turn by a successful insert_block -- the whole repair was
        already known, so refusing spent a turn on nothing and carried 322,366 characters to
        the end of the run. When the call can only ADD, the heading is a paragraph the
        caller CREATED, the level it must take is the one it already carries, and the holder
        is already computed here: nothing is left to infer, so the section is created on the
        spot, by the path insert_block uses, and the reply carries insert_block's own
        fields.

        Nothing is routed when the call ALSO writes into the block: creating the section and
        rewriting the block are TWO writes, and a call that can half-apply is worse than a
        refusal that costs one turn. The refusal then stands, unchanged.

        An insertion whose point falls INSIDE an open fence is never routed either: the new
        lines are CODE there, so the result gains no title, and returning None lets the
        ordinary path write them verbatim -- which is what HEAD does, and what the verifier
        measured this route destroying. An insertion point with the block's OWN lines still
        to come is refused in words that name it: a paragraph at this level is seated AFTER
        the block, which is not the place that was asked for.
        """
        if not additions:
            return None
        carriers: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
        for fragment in additions:
            headings = self._content_headings(str(fragment["text"]))
            if headings:
                carriers.append((fragment, headings))
        if len(carriers) != 1:
            # No title at all, or more than one: the outline the caller wants is not
            # decidable from one insertion, and the refusal says so.
            return None
        fragment, headings = carriers[0]
        if int(headings[0]["level"]) != int(block_level):
            return None
        # SEVERAL titles in the one insertion: the leading one is at the block's level and the
        # others are its own children (deeper) or further SIBLINGS (same level). A title ABOVE
        # the block's level would reorganise the tree, which is not an addition: refusal stays.
        if any(int(item["level"]) < int(block_level) for item in headings):
            return None
        siblings_written = [item for item in headings if int(item["level"]) == int(block_level)]
        # The heading has to OPEN what was inserted: lines before it would have no place
        # to go, and where they belong would be a guess.
        if self._leading_markdown_heading(str(fragment["text"])) is None:
            return None
        # CODE, not a title: the insertion point is inside a fence the block leaves open.
        if fragment.get("at_fence"):
            return None
        # ...and the title must be one the RESULT really gains, read the way the guard
        # reads it -- never the fragment on its own. Anything else is not this route's
        # business, and the guard decides.
        authored = self._authored_headings(body, target, block_level, text)
        if [str(item.get("raw", "")) for item in authored] != [
            str(item.get("raw", "")) for item in headings
        ]:
            return None
        if len({str(item.get("raw", "")) for item in headings}) != len(headings):
            return None            # the same title line twice: which one is meant is not decidable
        # The call adds a paragraph and NOTHING else: any other line it writes would move
        # the block, and this route's promise would be a lie. The guard refuses it, in its
        # own words, and both descriptions name this condition.
        if str(fragment["without"]) != str(fragment["block"]):
            return None
        # The place asked for cannot be honoured by a section: say so, do not move it.
        if not fragment["at_end"]:
            return self._title_route_off_point(section_id, block_level, fragment)

        position = next(
            (j for j, item in enumerate(layout) if item["index"] == target["index"]), None
        )
        if position is None:
            return None
        holder_index = self._holder_index(layout, position)
        holder = (
            self._layout_ids(file_path, layout)[holder_index]
            if holder_index is not None
            else file_path
        )
        # The level insert_block DERIVES from that holder must be the one the caller wrote.
        # Otherwise the paragraph created is not this block's sibling at all, and the
        # refusal is the honest answer. (Same derivation as insert_block, one level down.)
        if holder_index is not None:
            derived_level = int(layout[holder_index]["level"]) + 1
        else:
            first = next((item for item in layout if int(item["level"]) > 0), None)
            if first is None:
                return None
            derived_level = int(first["level"])
        if derived_level != int(block_level):
            return None

        if len(siblings_written) > 1:
            return self._title_routes_in_sequence(
                target, block_level, section_id, file_path, text, layout, fragment, holder
            )

        # THE GUARDS COME BEFORE THE WRITE. The route reaches the file by ANOTHER path
        # (insert_block), so nothing else would run them on its result: the first revision
        # decided here and wrote a document whose code fence was left open, and everything
        # after it stopped being sections -- an edit HEAD refuses, and which the ordinary
        # path still refuses. The dry run is the result, so the guards read it, not a guess.
        proposal, proposed = self._dry_run_the_write(holder, section_id, str(fragment["text"]))
        if proposal.get("status") != "success":
            # insert_block refuses for its own reasons -- a title a sibling already
            # carries, a name no file system accepts: that answer is the truthful one, it
            # names the repair, and it is MORE precise than the outline refusal this route
            # would otherwise fall back to. Nothing was written.
            return proposal
        if proposed is None:
            return None
        # The SAME entry point the ordinary path uses, and ALL THREE runners: the outline of
        # the block WITHOUT the paragraph being lifted out (its content in the result), the
        # title insert_block is about to write, and the document the dry run would leave.
        route_position = next(
            (j for j, item in enumerate(layout) if item["index"] == target["index"]), None
        )
        damaged = self._result_guards_refusal(
            text,
            proposed,
            section_id,
            {
                "target": target,
                "body": self._to_document_text(str(fragment["without"]), "\n"),
                "block_level": block_level,
                "file_path": file_path,
                "layout": layout,
                "new_heading": proposal.get("heading"),
                "new_level": proposal.get("level"),
                "position": route_position,
                # The paragraph is NEW: the block is a candidate to clash with, so nothing
                # is excluded here, unlike the ordinary path's retitle.
                "exclude_index": None,
            },
        )
        if damaged is not None:
            return damaged

        before_hash = str((self.read_block(section_id) or {}).get("content_hash") or "")
        created = self.insert_block(
            parent_id=holder, where=section_id, text=str(fragment["text"])
        )
        if created.get("status") != "success":
            # insert_block refuses for its own reasons -- a title a sibling already carries,
            # a name no file system accepts: that answer is the truthful one, and it names
            # the repair. Nothing was written.
            return created
        # The host is re-READ, never assumed. Seating a paragraph after the LAST block of a
        # file re-lays the end of the file, so the host's own text can gain a blank line and
        # its content_hash can move. Measured by the verifier: the reply used to hand back
        # the OLD hash with "still opens it" written beside it, and the next edit sent with
        # that hash answered "Stale read". A field is read from the file or it is not
        # emitted -- and the flag is true only when the file says it is.
        after = self.read_block(section_id) or {}
        after_hash = str(after.get("content_hash") or "")
        unchanged = bool(before_hash) and after_hash == before_hash
        created["section_id"] = section_id
        #: Read by edit_block: the FILE gained a section, so the reply must not be re-read as
        #: though the host's hash described what was created.
        created["title_route"] = True
        created["placement"] = "after"
        created["host_block_unchanged"] = unchanged
        created["host_content_hash"] = after_hash or before_hash
        created["warnings"] = [
            "a heading of level " + str(block_level) + " cannot live inside '" + str(section_id)
            + "' (level " + str(block_level) + "): it was created as its SIBLING, right AFTER "
            "it, under '" + self.displayed_id(holder) + "', with the level, the number and the "
            "identifier insert_block derives. "
            + (
                "'" + str(section_id) + "' itself is UNCHANGED -- content_hash "
                + after_hash + " still opens it."
                if unchanged
                else "'" + str(section_id) + "' is the LAST block of the file, and seating a "
                "paragraph after it re-lays the end of the file: its content_hash is NOW "
                + (after_hash or before_hash) + " (it was " + (before_hash or "unreadable")
                + "), so the stamp you sent answers 'Stale read'. Use the new one."
            ),
        ] + list(created.get("warnings") or [])
        return created

    def _title_routes_in_sequence(
        self,
        target: Dict[str, Any],
        block_level: int,
        section_id: str,
        file_path: str,
        text: str,
        layout: List[dict],
        fragment: Dict[str, Any],
        holder: str,
    ) -> Dict[str, Any]:
        """Create SEVERAL consecutive sibling paragraphs, each with its own sub-titles, or none.

        The call only ADDS, at the block's end (the caller of this method checked it, and checked
        that the titles the result gains are exactly the ones the fragment carries). Each chunk --
        a title at the block's level and what follows it up to the next title of that level -- is
        created by the SAME path as the single title (insert_block, which derives the level, the
        number and the identifier), the first one right after the block and each next one right
        after the previous. Two things replace the single title's dry run, because a chain cannot
        be dry-run (the second insertion needs the first on disk):
          * the first chunk IS dry-run and passes every result guard, exactly like the single title;
          * the real sequence is ALL-OR-NOTHING: the file is snapshotted, and a refusal at any
            step, or a guard that fails on the FINAL document, restores it byte for byte and
            re-indexes it. A half-written outline is never left behind.
        """
        chunks = self._sibling_chunks(str(fragment["text"]), int(block_level))
        if len(chunks) < 2:
            return {}  # unreachable: the caller counted the titles at this level
        proposal, proposed = self._dry_run_the_write(holder, section_id, chunks[0])
        if proposal.get("status") != "success":
            return proposal
        if proposed is None:
            return {}
        position = next(
            (j for j, item in enumerate(layout) if item["index"] == target["index"]), None
        )
        context = {
            "target": target,
            "body": self._to_document_text(str(fragment["without"]), "\n"),
            "block_level": block_level,
            "file_path": file_path,
            "layout": layout,
            "new_heading": proposal.get("heading"),
            "new_level": proposal.get("level"),
            "position": position,
            "exclude_index": None,
        }
        damaged = self._result_guards_refusal(text, proposed, section_id, context)
        if damaged is not None:
            return damaged
        if self._preview_pass or self._capture_writes is not None:
            # A validation pass or an outer dry run: nothing may be written, and the chain
            # cannot be simulated. The first chunk has passed everything a dry run can check.
            return {
                "status": "success",
                "section_id": section_id,
                "title_route": True,
                "placement": "after",
                "warnings": [],
            }

        abs_path, _text, _newline, error = self._resolve_markdown(file_path)
        if error:
            return error
        snapshot = Path(str(abs_path)).read_bytes()
        before_hash = str((self.read_block(section_id) or {}).get("content_hash") or "")
        created_ids: List[str] = []
        created_blocks: List[str] = []
        warnings: List[str] = []
        first: Dict[str, Any] = {}
        where = section_id
        failure: Optional[Dict[str, Any]] = None
        for index, chunk in enumerate(chunks):
            step = self.insert_block(parent_id=holder, where=where, text=chunk)
            if step.get("status") != "success" or not step.get("block_id"):
                failure = step if step.get("status") != "success" else {
                    "status": "error",
                    "section_id": section_id,
                    "error": "the paragraph number " + str(index + 1) + " was written but no block "
                    "was found for it.",
                }
                break
            if index == 0:
                first = step
            where = str(step["block_id"])
            created_blocks.append(str(step.get("created_block_id") or step["block_id"]))
            created_ids.extend(str(one) for one in step.get("created_section_ids") or [])
            warnings.extend(str(one) for one in step.get("warnings") or [])
        if failure is None:
            final_text = Path(str(abs_path)).read_bytes().decode("utf-8", errors="replace")
            failure = self._result_guards_refusal(text, final_text, section_id, context)
        if failure is not None:
            self._atomic_write(Path(str(abs_path)), snapshot)
            self._reindex(abs_path)
            failure = dict(failure)
            failure["rolled_back"] = True
            failure["error"] = (
                str(failure.get("error", "refused")) + " NOTHING was written: the " + str(len(chunks))
                + " paragraphs are created together or not at all, and the file was restored "
                "byte for byte."
            )
            return failure

        after = self.read_block(section_id) or {}
        after_hash = str(after.get("content_hash") or "")
        unchanged = bool(before_hash) and after_hash == before_hash
        result = dict(first)
        result["created_section_ids"] = created_ids
        result["created_block_ids"] = created_blocks
        result["section_id"] = section_id
        result["title_route"] = True
        result["placement"] = "after"
        result["host_block_unchanged"] = unchanged
        result["host_content_hash"] = after_hash or before_hash
        result["warnings"] = [
            str(len(chunks)) + " headings of level " + str(block_level) + " cannot live inside '"
            + str(section_id) + "': they were created as " + str(len(chunks)) + " consecutive "
            "SIBLINGS, right AFTER it, in the order you wrote them, each with the level, the number "
            "and the identifier insert_block derives and with the sub-titles that followed it. "
            + (
                "'" + str(section_id) + "' itself is UNCHANGED -- content_hash " + after_hash
                + " still opens it."
                if unchanged
                else "'" + str(section_id) + "' is now stamped " + (after_hash or before_hash)
                + " (it was " + (before_hash or "unreadable") + "): use the new stamp."
            )
        ] + warnings
        return result

    @classmethod
    def _sibling_chunks(cls, fragment_text: str, level: int) -> List[str]:
        """The fragment cut at every title of `level`: each chunk is one paragraph and its children."""
        lines = fragment_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        starts = [
            number for number, found, _title, _raw in heading_lines(lines) if int(found) == int(level)
        ]
        if not starts or starts[0] != next((i for i, one in enumerate(lines) if one.strip()), -1):
            return []
        bounds = starts + [len(lines)]
        return [
            "\n".join(lines[bounds[i] : bounds[i + 1]]).strip("\n") for i in range(len(starts))
        ]

    @staticmethod
    def _title_route_off_point(
        section_id: str, block_level: int, fragment: Dict[str, Any]
    ) -> Dict[str, Any]:
        """The refusal for a title asked for at a line the block's own lines still follow.

        A paragraph at the block's level can only be seated AFTER the block, so the route
        used to move the new lines past the ones the caller had asked them to precede and
        say nothing about it: `insert_after 0` and `insert_after 3` produced the same file
        with the same placement. Nothing is written here, and the caller is told which two
        different things the two repairs do.
        """
        return {
            "status": "error",
            "section_id": section_id,
            "error": (
                "Refused: the new title was asked for at line " + str(fragment["position"])
                + " of '" + str(section_id) + "', and " + str(
                    max(0, int(fragment["lines"]) - int(fragment["position"]))
                ) + " of the block's OWN line(s) still follow that point. A heading of level "
                + str(block_level) + " cannot live inside the block, and seating it after the "
                "block would move your lines PAST the ones you asked them to precede -- so the "
                "place you asked for cannot be respected, and the file is left untouched."
            ),
            "actionable_hint": (
                "Two ways out, and they do different things. To ADD A PARAGRAPH after '"
                + str(section_id) + "', send the same content with insert_after "
                + str(fragment["lines"]) + " (the block's LAST line): the heading then really "
                "is at the end of the block, and it is created as its sibling. To put these "
                "lines INSIDE the block at the line you named, drop the heading -- edit_block "
                "edits inside one block only."
            ),
        }

    def _outline_refusal(
        self,
        target: Dict[str, Any],
        body: str,
        block_level: int,
        section_id: str,
        file_path: str,
        text: str,
        layout: List[dict],
        additions: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Refuse a content edit that would reshape the outline.

        edit_block edits *inside* one node. It may retitle the sub-headings that
        are already there, or add exactly one of them, but it may not split the
        block with a heading at its own level (or above), delete a sub-heading,
        change a sub-heading's level, or reorder them. The number and the place of
        a paragraph are structure, not text: insert_block and
        delete_document_section own those, and they keep the numbering consistent.

        Measured: four of the nine archived runs that edited one block this way
        inserted a sibling heading through edit_block. The block was split, the
        reply showed only its first half, and the agent spent two to six turns --
        once renaming an unrelated section -- repairing a change it never asked
        for. Refusing costs one turn and keeps 'one node in, one node out' true.
        """
        # ONE reader, shared with _title_route: the headings the RESULT really gains, with
        # the fence-repair tolerance and nothing else. Reading the RAW block text here, the
        # way this guard used to, is what let a title equal to a line the block carries
        # inside a CLOSED fence pass as 'already there': the block was cut in two and the
        # document carried the same title twice. See _authored_headings.
        authored = self._authored_headings(body, target, block_level, text)
        siblings = [item for item in authored if int(item["level"]) <= int(block_level)]
        if siblings:
            first = siblings[0]
            # The caller is told which block HOLDS this one, instead of being told to
            # find it: on mapab2 a r2 that cost a whole insert_block call with the
            # document as parent, refused, before the right parent was named.
            holder = section_id
            index = next(
                (j for j, item in enumerate(layout) if item["index"] == target["index"]), None
            )
            if index is not None:
                holder_index = self._holder_index(layout, index)
                holder = (
                    self._layout_ids(file_path, layout)[holder_index]
                    if holder_index is not None
                    else file_path
                )
            return {
                "status": "error",
                "section_id": section_id,
                "error": (
                    "Refused: your content opens the heading '" + str(first["title"]) + "' at level "
                    + str(first["level"]) + ", which is not inside the block you are editing (level "
                    + str(block_level) + "). It would split '" + section_id + "' into two sections, and "
                    "edit_block edits inside one block only (one node in, one node out). The file is left untouched."
                ),
                "actionable_hint": (
                    "An edit_block operation cannot simultaneously edit a section and split it by introducing a sibling heading. "
                    "Decouple in two steps: "
                    "1) Use edit_block(path='" + self.displayed_id(section_id) + "', ops=[...]) to update this section's text without the heading. "
                    "2) Use insert_block(parent='" + self.displayed_id(holder) + "', anchor='" + self.displayed_id(section_id)
                    + "', text='" + str(first["title"]) + "\\n<body>') to insert the sibling section right after it. "
                    "For a CHILD instead, call insert_block(parent='" + section_id + "', anchor='" + section_id + "', "
                    "text=<title>\\n<body>)."
                ),
            }
        before = [int(item["level"]) for item in self._content_headings(text[target["body_start"] : target["section_end"]])]
        current = [int(item["level"]) for item in authored]
        if current == before or self._one_insertion(before, current):
            return None
        if self._only_adds_nested_titles_at_the_end(before, current, additions, text, target):
            return None
        return {
            "status": "error",
            "section_id": section_id,
            "error": (
                "Refused: this content changes the outline of '" + section_id + "' ("
                + str(len(before)) + " nested heading(s) before, " + str(len(current)) + " after). "
                "A heading inside a block creates a new paragraph, and one edit may add at most one. "
                "The file is left untouched."
            ),
            "actionable_hint": (
                "Add the paragraph with insert_block, one at a time; remove one with "
                "delete_block."
            ),
        }

    @classmethod
    def _numbered_heading(
        cls, layout: List[dict], position: int, level: int, heading: str
    ) -> Tuple[str, Optional[int]]:
        """(heading, number) with the number the outline implies, if any.

        The number is assigned here rather than asked of the caller: it is structure,
        and the tool is what knows the surrounding outline. A title inserted after a
        numbered sibling takes the next number; a title inserted at the head of the
        list takes the first one, which is pushed down; a title in an unnumbered
        document stays exactly as given, because inventing a numbering the document
        does not use would be worse than leaving it alone.

        A title that already begins with a DIGIT is never touched: the caller is
        managing its own numbering ("9. OAuth", "1bis. Notes", "2) Other"), and
        prepending a second number produced "2. 1bis. Nouvelle" on a real attempt.
        Only a plain "N." / "N)" also yields a number the following siblings can be
        shifted against.
        """
        stripped = heading.strip()
        if re.match(r"^\d", stripped):
            given = re.match(r"^(\d+)[.)](?!\d)", stripped)
            return heading, int(given.group(1)) if given else None
        previous = cls._previous_numbered_sibling(layout, position, level)
        if previous is not None:
            return str(previous + 1) + ". " + heading, previous + 1
        following = cls._next_numbered_sibling(layout, position, level)
        if following is not None:
            return str(following) + ". " + heading, following
        return heading, None

    @staticmethod
    def _next_numbered_sibling(layout: List[dict], position: int, level: int) -> Optional[int]:
        """The number of the first numbered section at `level` after `position`, if any.

        Walking forward stops at a section of a HIGHER level: that is where the parent
        scope ends.
        """
        for index in range(position, len(layout)):
            item = layout[index]
            item_level = int(item["level"])
            if item_level < level:
                return None
            if item_level > level:
                continue
            match = re.match(r"^(\d+)[.)](?!\d)", str(item["heading"]).strip())
            return int(match.group(1)) if match else None
        return None

    @staticmethod
    def _previous_numbered_sibling(layout: List[dict], position: int, level: int) -> Optional[int]:
        """The number of the nearest numbered section at `level` before `position`.

        Walking back stops at a section of a HIGHER level: that is where the parent
        scope begins, so there is no sibling before the insertion point. Only a plain
        "N." / "N)" counts: `1.1 Detail` is a two-level number, and deriving "2." from
        it produced "2. Detail bis" for a child section -- measured, then fixed.
        """
        for index in range(position - 1, -1, -1):
            item = layout[index]
            item_level = int(item["level"])
            if item_level < level:
                return None
            if item_level > level:
                continue
            match = re.match(r"^(\d+)[.)](?!\d)", str(item["heading"]).strip())
            return int(match.group(1)) if match else None
        return None

    @staticmethod
    def _subtree_end(layout: List[dict], index: int, total: int) -> int:
        """Offset just after a section AND its descendants.

        Inserting a sibling right after the section's own body would land before its
        children, which are deeper: the new section would then swallow them.
        """
        level = int(layout[index]["level"])
        for item in layout[index + 1 :]:
            if int(item["level"]) <= level:
                return int(item["section_start"])
        return total

    @staticmethod
    def _renumber_following(
        text: str, block_start: int, level: int, number: int
    ) -> Tuple[str, List[str]]:
        """Increment the leading numbers of the following sections at the same level.

        Inserting 'N. Title' above an existing 'N. Other' collides. A human
        renumbers the rest in the same edit, so the tool does too: it saves the
        agent a second pass over the document just to fix numbering.

        It also returns what it rewrote, because those are headings the CALLER did
        not touch: an agent saw its siblings renumbered, read the reply as saying the
        insertion had done nothing else, and spent four turns restoring numbers the
        tool had deliberately kept coherent (declared on mapab2 a r1 of the base
        e3a8c08: "silently renumbered the pre-existing sibling headings").
        """
        edits = []
        changes: List[str] = []
        inserted_seen = False
        for item in markdown_layout(text):
            if item["section_start"] < block_start:
                continue
            if not inserted_seen:
                inserted_seen = True
                continue
            if int(item["level"]) < level:
                break
            if int(item["level"]) != level:
                continue
            # Only a plain "N." or "N)" is a section number: "1.1" is a two-level
            # number, and rewriting it would turn it into "2.1".
            prefix = re.match(r"^(\d+)([.)])(?!\d)", str(item["heading"]).strip())
            if not prefix:
                continue
            current = int(prefix.group(1))
            if current < number:
                continue
            line = text[item["section_start"] : item["body_start"]]
            changes.append(
                "'" + str(item["heading"]).strip() + "' -> '"
                + line.replace(str(current) + prefix.group(2), str(current + 1) + prefix.group(2), 1)
                .strip().lstrip("#").strip()
                + "'"
            )
            edits.append(
                (
                    item["section_start"],
                    item["body_start"],
                    line.replace(str(current) + prefix.group(2), str(current + 1) + prefix.group(2), 1),
                )
            )
        for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
            text = text[:start] + replacement + text[end:]
        return text, changes

    @staticmethod
    def _number_label(heading: str) -> Optional[str]:
        """The whole number a heading carries: "2.3" is not the number "2.".

        Comparing only the leading digit made `### 2.3 Databricks PWAF Compliance` warn that
        "section number '2.' is already used by: 2.1 ...; 2.2 ...; 2.3 ..." -- two headings
        that do not conflict, with the one that does buried among them. The agent reported the
        warning as ambiguous (mapab2 b r2 of the base 957f704).
        """
        match = re.match(r"^(\d+(?:\.\d+)*)[.)]?(?=\s|$)", heading.strip())
        return match.group(1) if match else None

    @classmethod
    def _structure_warnings(cls, layout: List[dict], start_offset: int, level: int, heading: str) -> List[str]:
        """Warnings about the structure a write just produced.

        The tool knows the surrounding outline, so it can flag a heading inserted
        at the wrong level and a section number already in use -- two defects seen
        on a real run, invisible to the textual verifier.
        """
        warnings: List[str] = []
        previous_level = 0
        for item in layout:
            if item["section_start"] < start_offset:
                previous_level = int(item["level"])

        if previous_level and level > previous_level + 1:
            warnings.append(
                "heading level jumps from " + str(previous_level) + " to " + str(level)
                + "; the surrounding structure uses level " + str(previous_level)
            )

        next_level = None
        inserted_seen = False
        for item in layout:
            if item["section_start"] < start_offset:
                continue
            if not inserted_seen:
                inserted_seen = True
                continue
            next_level = int(item["level"])
            break
        if next_level is not None and next_level > level + 1:
            warnings.append(
                "the next heading is level " + str(next_level) + " after a level " + str(level)
                + " heading: the outline now skips level " + str(level + 1)
            )

        label = cls._number_label(heading)
        if label:
            inserted = next(
                (j for j, item in enumerate(layout) if item["section_start"] >= start_offset), None
            )
            holder = cls._holder_index(layout, inserted) if inserted is not None else None
            carriers = [
                j
                for j, item in enumerate(layout)
                if cls._number_label(str(item["heading"])) == label
                # Only a SIBLING is a conflict: the same number under another parent
                # is a different list. Warning about one sent an agent to "repair"
                # numbering that was already right (declared on mapab2 a r1 of the
                # base ef07986: two turns chasing a conflict that did not exist).
                and cls._holder_index(layout, j) == holder
            ]
            # The section being WRITTEN is excluded from its own notice -- by IDENTITY,
            # never by its title. `start_offset` names it while it keeps that offset; when
            # an insertion lands BEFORE it the block moves and a NEW section takes the
            # offset, so the block is then the carrier whose heading IS the one passed.
            # When TWO sections carry that heading, the title identifies nothing: every
            # other carrier is a real duplicate and IS named. Excluding by title instead
            # silenced a TRUE warning -- two verifiers found it on
            # ops = [insert_after 0, insert_after 4], where the file really holds the same
            # '### 8. Databricks ...' twice and HEAD warned.
            own = next(
                (j for j in carriers if int(layout[j]["section_start"]) == start_offset),
                None,
            )
            if own is None:
                same = [
                    j
                    for j in carriers
                    if str(layout[j]["heading"]).strip() == str(heading).strip()
                ]
                if len(same) == 1:
                    own = same[0]
            used_by = [str(layout[j]["heading"]).strip() for j in carriers if j != own]
            if used_by:
                warnings.append(
                    "section number '" + label + "' is already used by: " + "; ".join(used_by[:3])
                )
        return warnings

    def _section_ids_from(
        self,
        layout: List[dict],
        document_id: str,
        start_offset: int,
        level: int,
        end_offset: Optional[int] = None,
    ) -> List[str]:
        """Identifiers of the section starting at start_offset, plus its sub-sections (or all sections up to end_offset).

        A heading inside the submitted content becomes a section of its own, so
        the caller cannot guess how many sections its write produced. Reporting
        them avoids a full table-of-contents read -- and the mistaken belief that
        the content was truncated to its first paragraph. The ids are read back
        from the freshly re-indexed document, so they are the stable ones.
        """
        order_indices: List[int] = []
        for item in layout:
            if item["section_start"] < start_offset:
                continue
            if end_offset is not None:
                if item["section_start"] >= end_offset:
                    break
            elif order_indices and int(item["level"]) <= level:
                break
            order_indices.append(int(item["index"]))
        by_index = {
            int(row["order_index"]): row["id"]
            for row in self.db.get_document_sections(document_id)
        }
        return [by_index[index] for index in order_indices if index in by_index]

    def _created_sections(
        self,
        layout: List[dict],
        document_id: str,
        region_start: int,
        region_end: int,
        before_ids: Any,
        level: int,
    ) -> Tuple[List[str], List[str]]:
        """Sections a write created inside its own region: (all, those that split).

        The written region is known exactly (byte offsets inside the new text), so
        the sections that start in it and whose identifier did not exist before
        the write are precisely the ones the write created. Those at the edited
        level or above end the edited block: the caller is editing a block that is
        no longer whole, which is the part it cannot guess.
        """
        by_index = {
            int(row["order_index"]): row["id"]
            for row in self.db.get_document_sections(document_id)
        }
        created: List[str] = []
        split: List[str] = []
        for item in layout:
            start = int(item["section_start"])
            if start < region_start or start >= region_end:
                continue
            if start == region_start:
                # The rewritten section itself: a rename gives it a new id, but it
                # is already reported as resulting_section_ids[0].
                continue
            identifier = by_index.get(int(item["index"]))
            if not identifier or identifier in before_ids or identifier in created:
                continue
            created.append(identifier)
            if int(item["level"]) <= level:
                split.append(identifier)
        return created, split

    def _blocks_named(self, name: str) -> List[str]:
        """The short paths of the blocks whose NAME is exactly this one, workspace-wide.

        A bare name is not a path, and answering "it does not exist yet" about a name that
        EXISTS somewhere is a lie the caller acts on.
        """
        wanted = self._fold_name(name)
        # Every ROW is kept, duplicates included: the count is what the caller needs, and two
        # paragraphs of one title print the SAME short path -- de-duplicating the list made the
        # refusal announce "1 block(s)" for a name three paragraphs carried.
        found: List[str] = []
        for row in self.db.search_symbols(name, limit=50):
            identifier = str(row.get("symbol_id") or "")
            if "::" not in identifier:
                continue
            # The stored id IS the printed path, except for an overload, whose tail carries
            # a sha1 the surface never prints: the NAME is the tail without that suffix and
            # without a parameter list.
            tail = identifier.rsplit("::", 1)[-1].split("#", 1)[0]
            bare = self._split_parameter_list(tail)[0].strip()
            if self._fold_name(bare) != wanted:
                continue
            found.append(str(row.get("file") or "") + "::" + bare)
        for row in self.db.search_documents(name, limit=50):
            if self._fold_name(str(row.get("heading") or "")) != wanted:
                continue
            found.append(str(row.get("document_id") or "") + "::" + str(row.get("heading") or ""))
        return found

    def _cannot_receive_a_child(self, parent_id: str) -> Dict[str, Any]:
        """Why nothing can be created under `parent_id`, and which door does exist.

        The bare "no document or section" is true and useless to a caller that is trying to
        CREATE something: an agent aiming at 'docs/runbook.md' passes the path it wants, not
        the directory that would hold it, and is then told only that its future file does not
        exist -- a dead end that costs a turn (mapab2 a r1: the flat "no such identifier" then
        three searches that answered nothing).
        """
        payload: Dict[str, Any] = {
            "status": "error",
            "parent_id": parent_id,
            "error": "No document or section has the identifier '" + parent_id + "'.",
        }
        # Containment comes FIRST, exactly as it does in `_unresolved_block`: a path that
        # leaves the workspace is not a name nobody has yet, and telling the caller it "does
        # not exist yet" invents a cause the code never tested (torture, defect 16).
        if self._outside_the_workspace(parent_id):
            return {
                "status": "error",
                "parent_id": parent_id,
                "error": (
                    "'" + parent_id + "' resolves outside the workspace root and is refused: "
                    "this index only ever answers for paths inside it."
                ),
                "actionable_hint": (
                    "The workspace root is '" + str(self.config.workspace_root) + "'; name a "
                    "path inside it."
                ),
            }
        # A bare NAME is not a path. Before pretending it is a missing file, ask the index
        # whether some block carries that name: the door is a path, and it is one `::` away.
        # Measured -- `parent='C'` on a class C living in src/T.cs told the agent to create a
        # FILE named C in '.', and the class it wanted was one separator away.
        if "#" not in parent_id and "::" not in parent_id and "/" not in parent_id:
            named = self._blocks_named(parent_id)
            if named:
                paths = sorted(set(named))
                listed = ", ".join(paths[:4])
                if len(paths) > 4:
                    listed += " (and " + str(len(paths) - 4) + " more)"
                return {
                    "status": "error",
                    "parent_id": parent_id,
                    "error": (
                        "'" + parent_id + "' is a NAME, not a path: " + str(len(named))
                        + " block(s) in this workspace carry it, and a parent is written as a path."
                    ),
                    "actionable_hint": "Pass the whole path instead: " + listed + ".",
                }
        # A section or symbol id is not a path: there is no directory that could hold it,
        # so there is no path advice to give.
        if "#" in parent_id or "::" in parent_id:
            return payload
        try:
            candidate = self.config.to_absolute_path(parent_id)
        except ValueError:
            return payload
        if candidate.is_file():
            payload["error"] = (
                "'" + parent_id + "' is a FILE, not a block that can take a child: nothing was "
                "created."
            )
            payload["actionable_hint"] = (
                "A code file's children are symbols -- insert_block(parent='" + parent_id
                + "', ...) creates one. insert_block creates the TREE (files and directories) and "
                "document paragraphs."
            )
            return payload
        holder = candidate.parent
        if holder.is_dir():
            payload["error"] = (
                "'" + parent_id + "' does not exist yet, so it cannot receive a child, and "
                "nothing was created for it."
            )
            payload["actionable_hint"] = (
                "Create it first, in the directory that would hold it: insert_block(parent='"
                + self._relative_path(holder) + "', text='" + candidate.name + "') for a file, or "
                "text='" + candidate.name + "/' for a directory."
            )
        return payload

    # No file system takes these in a name; refusing them by name beats an OSError the
    # caller cannot act on.
    INVALID_NAME_CHARS = '<>:"|?*'

    def _create_child_of(
        self, parent_id: str, directory: Path, name_text: str, body_text: str, where: str
    ) -> Dict[str, Any]:
        """Create ONE child of a directory: a file, or a directory if the name ends in a slash.

        The new child sits DIRECTLY in its parent, so its name carries no separator: that is
        what keeps the tree the only truth about where a block lives. Nothing is written if
        anything is wrong -- a half-created block is a read that lies.
        """
        payload: Dict[str, Any] = {"status": "error", "parent_id": parent_id}
        if where != parent_id:
            payload["where"] = where
            payload["error"] = (
                "A directory orders its children by NAME, so there is no position for 'where' to "
                "name: the new child was NOT created. Leave 'where' out (or pass the parent), and "
                "the child's place follows its own name."
            )
            payload["actionable_hint"] = (
                "insert_block(parent='" + parent_id + "', text='<name>') creates the child."
            )
            return payload

        name = name_text.strip()
        is_directory = name.endswith("/") or name.endswith("\\")
        name = name.rstrip("/\\").strip()
        if not name:
            payload["error"] = (
                "The new block has no name: the first line of 'text' is the name of the child, "
                "and a trailing slash marks a directory."
            )
            return payload
        if name in (".", "..") or "/" in name or "\\" in name:
            payload["error"] = (
                "'" + name + "' is not a name: a new block is a DIRECT child of '" + parent_id
                + "', so its name carries no separator and is neither '.' nor '..'. Nothing was written."
            )
            payload["actionable_hint"] = (
                "Create the parent first (text='sub/'), then the child inside it: "
                "insert_block(parent='sub', text='...')."
            )
            return payload
        bad = sorted({character for character in name if character in self.INVALID_NAME_CHARS})
        if bad:
            payload["error"] = (
                "The name contains " + ", ".join(bad) + ", which no file system accepts: nothing "
                "was written."
            )
            return payload

        target = directory / name
        relative = self._relative_path(target)
        payload["path"] = relative
        indexer = getattr(self, "indexer", None)
        if indexer is not None and indexer.should_ignore(target):
            payload["error"] = (
                "'" + relative + "' matches an ignore pattern of this workspace, so it would "
                "exist on disk and stay invisible to every read: nothing was written."
            )
            return payload
        if target.exists():
            payload["existing_type"] = "dir" if target.is_dir() else "file"
            payload["error"] = (
                "'" + relative + "' already exists (" + payload["existing_type"] + "). A name is "
                "an identity: creating over it would change what every child id already handed "
                "out means. The existing block is untouched."
            )
            payload["actionable_hint"] = (
                "read_block('" + relative + "') reads what is there; choose another name for the "
                "new block."
            )
            return payload

        if is_directory:
            try:
                target.mkdir()
            except OSError as error:
                payload["error"] = "The directory could not be created: " + str(error)
                return payload
            return {
                "status": "success",
                "kind": "directory",
                "block_id": relative,
                "parent_id": parent_id,
                "name": name + "/",
                "created": relative,
                "actionable_hint": (
                    "It holds no child yet: insert_block(parent='" + relative + "', "
                    "text='<name>') creates the first one."
                ),
            }

        body = body_text.encode("utf-8")
        try:
            self._atomic_write(target, body)
        except OSError as error:
            payload["error"] = "The file could not be written: " + str(error)
            return payload
        if indexer is not None:
            try:
                self._reindex(target)
            except Exception:
                pass
        result: Dict[str, Any] = {
            "status": "success",
            "kind": "file",
            "block_id": relative,
            "parent_id": parent_id,
            "name": name,
            "bytes": len(body),
            "created": relative,
        }
        # The invariant (12.2): a write may slow a read, never make it false. So the reply
        # says whether the path it just created is a block the reader will answer for.
        # That question is exactly read_block -- not block_metrics, which is None for every
        # code file by design (its outline comes from the unresolved-block path).
        if self.read_block(relative).get("status") == "success":
            result["readable_as_a_block"] = True
            result["actionable_hint"] = "read_block('" + relative + "') reads it back."
        else:
            result["readable_as_a_block"] = False
            result["warning"] = (
                "'" + relative + "' is on disk, but Scalpel indexes no block inside it, so "
                "read_block will not answer for this path. The write itself is correct; the file "
                "is simply outside the parsed set."
            )
        return result

    def insert_block(
        self,
        parent_id: Optional[str] = None,
        where: Optional[str] = None,
        text: str = "",
        *,
        parent: Optional[str] = None,
        anchor: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a new block as a CHILD of `parent_id`, and return its id.

        `parent_id` is the block that receives the child. A document path means the
        document itself, whose children are its top-level sections. `where` is an ID and
        is OPTIONAL: the id of one of the parent's children (the new block goes right
        after it), or `parent_id` itself, or simply omitted -- both of the last two insert
        the new block first.

        `text` is the new block: its FIRST LINE is the title, the rest is the body. A
        leading "#" marker on that line is accepted and dropped: the level comes from
        the parent (one deeper) and the number from the children around the insertion
        point, so the caller never states either. A title that begins with a digit
        keeps its own number, and nothing is invented in a document without numbering.

        Nothing is addressed by POSITION in the document: the parent says where in the
        tree, `where` says where among its children. That is what stops a new block
        from re-parenting what follows it.
        """
        if parent_id is None:
            parent_id = parent or ""
        if where is None and anchor is not None:
            where = anchor
        # The PRINTED path (`fichier.md::Section mere::Paragraphe`) is the surface's own
        # vocabulary, so a parent named that way must seat the child exactly like the stored id
        # would. Resolution happens HERE, once, for every caller: `insert_block` and `read_block`
        # must not answer to different names (measured: a paragraph path read fine and was
        # refused as a parent, which left `insert_block` reachable only through `#slug`).
        if "::" in parent_id:
            resolved = self._resolve_displayed_path(parent_id)
            if resolved:
                parent_id = resolved
        if where and "::" in where:
            resolved_where = self._resolve_displayed_path(where)
            if resolved_where:
                where = resolved_where
        # Resilient interface: an omitted `where` means "first child", exactly like
        # passing the parent's own id. An agent that just wants to prepend should not have
        # to know any magic value.
        if where is None or where == "":
            where = parent_id
        if content is not None:
            heading_text = text.strip()
            body_text = content
        else:
            heading_text, body_text = self._split_block_text(text)
        if not heading_text:
            return {
                "status": "error",
                "parent_id": parent_id,
                "error": "The new block has no title: its first line is the title.",
            }

        # A DIRECTORY is a block whose children are FILES, so inserting into one CREATES the
        # child: creating a file and inserting a paragraph are the same call, and the parent's
        # kind decides what the child is (ROADMAP 12.3 etape 3). The name carries the type --
        # a trailing slash means a directory -- because 12.2 says identity IS the name: a
        # separate "kind" field could disagree with the name, and two blocks could share one.
        directory = self._existing_path(parent_id)
        if directory is not None and directory.is_dir():
            return self._create_child_of(parent_id, directory, heading_text, body_text, where)

        # The parent names the document too: a path IS the document.
        document_id = parent_id if self.db.get_document(parent_id) is not None else None
        parent = None
        parent_live = None
        if document_id is None:
            # A section id is a NAME: it is resolved against the file, so a stored
            # table that is one step behind cannot seat the children under a
            # neighbour. The table only answers for a legacy positional id
            # (`file#2`), which is not a name and never claimed to be one.
            parent_live = self._live_section(parent_id)
            if parent_live is not None:
                document_id = parent_live[0]
            else:
                parent = self.db.get_document_section(parent_id)
                if not parent:
                    return self._cannot_receive_a_child(parent_id)
                document_id = parent["document_id"]
        if not self._is_markdown(document_id):
            return {
                "status": "error",
                "document_id": document_id,
                "error": (
                    "Only Markdown documents (.md, .markdown, .mdx) can take a new block; Word and "
                    "PDF are read-only."
                ),
            }
        abs_path, document_text, newline, error = self._resolve_markdown(document_id)
        if error:
            error["document_id"] = document_id
            return error
        layout = markdown_layout(document_text)

        # The receiving node, and the span of its direct children.
        parent_index: Optional[int] = None
        if parent_live is not None:
            parent_index = next(
                (j for j, item in enumerate(layout) if item["index"] == parent_live[3]["index"]), None
            )
        elif parent is not None:
            parent_index = next(
                (j for j, item in enumerate(layout) if item["index"] == parent["order_index"]), None
            )
        if parent_live is not None or parent is not None:
            if parent_index is None:
                return {
                    "status": "error",
                    "parent_id": parent_id,
                    "error": (
                        "That block no longer exists in the file; re-run read_block on the "
                        "file for its current outline."
                    ),
                }
            if parent is not None:
                # Legacy positional identifier: the table is what it means, and it
                # is checked against the file so a stale row cannot write elsewhere.
                live = str(layout[parent_index]["heading"]).strip()
                if live.lower() != str(parent["heading"]).strip().lower():
                    return {
                        "status": "error",
                        "parent_id": parent_id,
                        "error": (
                            "'" + parent_id + "' now holds '" + live + "', not '" + str(parent["heading"])
                            + "'. Identifiers are resolved by position, so it no longer designates the same "
                            "block. The file is left untouched."
                        ),
                        "actionable_hint": "Re-read the file outline (read_block on the file path), then retry.",
                    }
            parent_level = int(layout[parent_index]["level"])
            children_start = parent_index + 1
            children_end = len(layout)
            for j in range(parent_index + 1, len(layout)):
                if int(layout[j]["level"]) <= parent_level:
                    children_end = j
                    break
            level = parent_level + 1
        else:
            children_start = next(
                (j for j, item in enumerate(layout) if int(item["level"]) > 0), len(layout)
            )
            children_end = len(layout)
            level = int(layout[children_start]["level"]) if children_start < len(layout) else 1
        children = [
            j for j in range(children_start, children_end) if int(layout[j]["level"]) == level
        ]

        # Where among the children. IDS ONLY: a child id to insert after, or the
        # parent's own id to insert first. There is no magic 'start' string to remember,
        # and passing the parent is what the agents already did (6 of 21 campaign runs,
        # a r3/r5/r8 and b r0/r1/r4) -- the previous interface refused it for a turn.
        if where == parent_id:
            if children:
                offset = int(layout[children[0]]["section_start"])
            elif parent_index is not None:
                offset = int(layout[parent_index]["section_end"])
            else:
                offset = self._document_root_end(document_text)
        else:
            # `where` is a NAME too: it is matched against the names the FILE produces,
            # never against a stored position. The table used to answer (name -> its
            # order_index -> the layout entry at that index), so a table one step behind
            # the file seated the new block beside the wrong paragraph. Either the name
            # exists in the file, or it does not.
            live_ids = self._layout_ids(document_id, layout)
            child_index = next(
                (j for j, identifier in enumerate(live_ids) if identifier == where), None
            )
            if child_index is None:
                # A legacy positional identifier (`file#2`) is not a name: the table is
                # the only thing that can say what it means, and it is what it means.
                child = self.db.get_document_section(where)
                if child is not None and child["document_id"] == document_id:
                    child_index = next(
                        (j for j, item in enumerate(layout) if item["index"] == child["order_index"]),
                        None,
                    )
            if child_index is None or child_index not in children:
                return self._where_not_a_child(
                    parent_id, where, document_id, layout, live_ids, child_index
                )
            # After the child AND its own descendants: landing right after its body
            # would make the new block swallow them, they are one level deeper.
            offset = self._subtree_end(layout, child_index, len(document_text))

        insert_position = sum(1 for item in layout if int(item["section_start"]) < offset)
        heading_text, number = self._numbered_heading(layout, insert_position, level, heading_text)
        clash = self._sibling_clash(layout, heading_text, level, insert_position)
        if clash is not None:
            return self._reject_duplicate_heading("parent_id", parent_id, heading_text, level)

        body = self._to_document_text(body_text, newline)
        block = ("#" * level) + " " + heading_text + newline + newline + body + newline + newline
        if offset >= len(document_text):
            trimmed = document_text.rstrip()
            block_start = len(trimmed)
            new_text = trimmed + newline + newline + block
        else:
            block_start = offset
            new_text = document_text[:offset] + block + document_text[offset:]

        renumbered: List[str] = []
        if number is not None:
            new_text, renumbered = self._renumber_following(new_text, block_start, level, number)

        self._atomic_write(abs_path, new_text.encode("utf-8"))
        self._reindex(abs_path)
        after_layout = markdown_layout(new_text)
        block_end = block_start + len(block)
        created = self._section_ids_from(after_layout, document_id, block_start, level, end_offset=block_end)
        warnings = self._structure_warnings(after_layout, block_start, level, heading_text)
        if renumbered:
            # The tool rewrote headings the caller never mentioned: it must say so, or
            # the caller reads the reply as a plain insertion and goes looking for a
            # change it cannot see.
            warnings.insert(0, self._renumber_warning(renumbered, number))
        payload = {
            "status": "success",
            "block_id": created[0] if created else None,
            "parent_id": parent_id,
            "where": where,
            "heading": heading_text,
            "level": level,
            "number": number,
            # A block whose text carries further headings creates several: list them,
            # without which the caller reads the first id it guesses and believes its
            # text was truncated.
            "created_section_ids": created,
            "warnings": warnings,
        }
        # What the caller just CREATED, addressed and hashed exactly as read_block would. This
        # reply used to name the id and nothing else: measured on run 1 of base f2a9649, the agent
        # inserted a section here and then spent TWO read_block calls (the parent, then the new
        # siblings) finding what it had just written and the hash to keep editing.
        if created:
            state = self.block_metrics(created[0])
            if state is not None:
                # The STATE, not a fresh stamp: all four fields come from ONE snapshot, so the
                # hash describes exactly the numbering returned beside it. The hash used to be
                # withheld ("insert has nothing to guard"), but a caller that edits what it just
                # inserted needs a stamp, and without one it GUESSES: measured on a r4 of the base
                # 5014c43 (t21 -> t23), the agent sent the SHA-256 of the empty string as
                # expected_hash, took the -- correct -- stale-read refusal for a tool fault, and
                # declared it as an anomaly. One turn, plus a wrong belief about the tool.
                payload["created_block_id"] = self.displayed_section_id(created[0])
                payload["line_count"] = state["line_count"]
                payload["numbered_text"] = state["numbered_text"]
                payload["content_hash"] = state["content_hash"]
        return payload

    @staticmethod
    def _renumber_warning(changes: List[str], number: Optional[int]) -> str:
        """What the numbering pass rewrote on its own, named so the caller can check it.

        Without it the reply reads as a plain insertion while three sibling headings
        changed: the agent re-listed the document to discover it, then spent four turns
        restoring numbers the tool keeps coherent on purpose (mapab2 a r1 of the base
        e3a8c08).
        """
        shown = "; ".join(changes[:3])
        if len(changes) > 3:
            shown += "; and " + str(len(changes) - 3) + " more"
        return (
            "the new paragraph took number " + str(number) + ", so the tool renumbered the "
            "sibling headings that followed it: " + shown + ". They are kept in sequence on "
            "purpose; a title that already carries its own number is left as written."
        )

    @staticmethod
    def _holder_index(layout: List[dict], index: int) -> Optional[int]:
        """Index of the block that HOLDS layout[index], or None for the document root.

        The nearest heading above it that is less deep: the tree is read from the
        headings themselves, so naming a holder never depends on a stored position.
        """
        level = int(layout[index]["level"])
        for j in range(index - 1, -1, -1):
            if int(layout[j]["level"]) < level:
                return j
        return None


    def _where_not_a_child(
        self,
        parent_id: str,
        where: Any,
        document_id: str,
        layout: List[dict],
        live_ids: List[str],
        child_index: Optional[int],
    ) -> Dict[str, Any]:
        """Refuse a `where` that is not a child of `parent_id`, naming its real parent.

        The file already knows which block holds it, so the refusal says which one.
        Without that the caller guesses the parent and burns a turn, or reads the
        whole outline to rediscover it: measured on mapab2 r0, this wall cost four
        turns in arm A and seven in arm B, one of them the refused call repeated
        verbatim because the message did not say what was wrong with it.
        """
        payload: Dict[str, Any] = {
            "status": "error",
            "parent_id": parent_id,
            "error": (
                "where must be the id of a child of '" + parent_id + "', or '" + parent_id
                + "' itself to insert first: '" + str(where)
                + "' is neither, so the new block would not sit under that parent."
            ),
            "actionable_hint": (
                "Name a child of '" + parent_id + "' to insert after it, or pass anchor='"
                + parent_id + "' to insert first."
            ),
        }
        if child_index is None:
            return payload
        level = int(layout[child_index]["level"])
        if level == 0:
            # The old refusal said the preamble was "a child of" the very parent it was
            # refused under, and its hint told the caller to repeat the failing call.
            payload["error"] = (
                "'" + str(where) + "' is the document's PREAMBLE -- the text before the first "
                "heading. It is a leaf, so nothing can be placed after it, and nothing was "
                "inserted."
            )
            payload["actionable_hint"] = (
                "Pass anchor='" + parent_id + "' to insert as the FIRST child, or name one of the "
                "children read_block('" + document_id + "') lists."
            )
            return payload
        holder_index = self._holder_index(layout, child_index)
        holder = live_ids[holder_index] if holder_index is not None else document_id
        payload["error"] = (
            "where must be the id of a child of '" + parent_id + "', or '" + parent_id
            + "' itself to insert first: '" + str(where)
            + "' is a level-" + str(level) + " block, a child of '" + holder + "', so the new block "
            "would not sit under '" + parent_id + "'."
        )
        payload["actionable_hint"] = (
            "To place the new block beside it, call insert_block(parent='" + holder
            + "', anchor='" + str(where) + "', text=<title>\\n<body>). To place it first inside '"
            + parent_id + "', pass anchor='" + parent_id + "'."
        )
        payload["parent_of_where"] = holder
        return payload

    @classmethod
    def _split_block_text(cls, text: str) -> Tuple[str, str]:
        """(title, body) of a new block: its first line is the title.

        A leading "#" marker is dropped: the level comes from the parent, so an agent
        that writes "## OAuth" out of habit gets the same block as one that writes
        "OAuth". The number, if any, is left alone -- _numbered_heading decides.
        """
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        title_index: Optional[int] = None
        for index, line in enumerate(lines):
            if line.strip():
                title_index = index
                break
        if title_index is None:
            return "", ""
        title = lines[title_index].strip()
        marker = re.match(r"^#{1,6}\s+(.*)$", title)
        if marker:
            title = marker.group(1).strip()
        body = "\n".join(lines[title_index + 1 :]).strip("\n")
        return title, body

    def displayed_section_id(self, section_id: str) -> str:
        """The path the surface PRINTS for a paragraph: its heading chain, rank included.

        Two sibling headings of one title are two different paragraphs that printed ONE path --
        `docs/guide.md::New Features` twice -- so nothing could address the second one. The
        symbol printer has carried a rank since this night (`_symbol_labels`); the paragraph
        printer only had it in its ambiguity CANDIDATES. Same rule everywhere: what the surface
        prints is what it accepts, and two blocks never print the same address.
        """
        row = self.db.get_document_section(section_id)
        if row is None:
            return section_id
        document_id = str(row.get("document_id") or "")
        _abs_path, text, _newline, error = self._resolve_markdown(document_id)
        if error:
            return section_id
        layout = markdown_layout(text)
        identifiers = self._layout_ids(document_id, layout)
        chains = self._section_chains(layout, self._section_labels(layout))
        for index, identifier in enumerate(identifiers):
            if identifier == section_id and index < len(chains):
                label = "::".join(chains[index])
                # The preamble has no heading chain, so its address is the document's own PATH --
                # never 'file.md::'. Measured on the run of base 684fbb3: an entry came back as
                # '...md#preamble' in one place and 'file.md::' in another, both from this gap.
                return document_id + "::" + label if label else document_id
        return section_id
    def displayed_symbol_id(self, symbol_id: str) -> str:
        """The path the surface PRINTS for a symbol: ancestry and name, never the stored sha1.

        An overload's id carries a suffix derived from its declaration -- that stability is
        what keeps an id from designating another declaration after a reorder -- but the
        suffix is an INTERNAL coordinate. `search_symbols` was the one surface that printed
        it: 19 of 20 lines of a real query came back as `...GetColumnName#500f990e`. What a
        developer writes, and what resolution matches, is the parameter list.
        """
        symbol = self.db.get_symbol(symbol_id)
        if symbol is None:
            return symbol_id
        # `_symbol_rows`, not `get_symbols_for_file`: the labels need the SIGNATURE (it carries
        # the parameter list that discriminates an overload), and that method does not select it.
        rows = self._symbol_rows(symbol.file_path)
        identifiers = self._symbol_id_chains(rows).get(symbol_id)
        if not identifiers:
            return symbol_id
        labels = self._symbol_labels(rows)
        return symbol.file_path + "::" + "::".join(labels[identifier] for identifier in identifiers)

    #: Fields of a tool reply that carry a BLOCK ADDRESS. The caller reads them and retypes them, so
    #: every one speaks the printed grammar -- never a stored id (file#slug) nor a storage symbol id
    #: (file::A.B).
    ADDRESS_FIELDS = (
        "id",
        "block_id",
        "section_id",
        "parent_id",
        "where",
        "symbol_id",
        "created_section_ids",
        "resulting_section_ids",
        "created_symbol_id",
        "created_symbol_ids",
        "expected_hash_belongs_to",
    )

    def document_addresses(self, document_id: str) -> Dict[str, str]:
        """{stored section id -> printed address}, in ONE layout pass.

        `displayed_section_id` re-reads and re-parses the file for every section it is asked about:
        fine for one address, ruinous for a Dossier that names every paragraph of a document. The
        Dossier is the biggest consumer of addresses on the whole surface, so the mapping is built
        once per document.
        """
        _abs_path, text, _newline, error = self._resolve_markdown(document_id)
        if error:
            return {}
        layout = markdown_layout(text)
        identifiers = self._layout_ids(document_id, layout)
        chains = self._section_chains(layout, self._section_labels(layout))
        printed: Dict[str, str] = {}
        for index, identifier in enumerate(identifiers):
            if index >= len(chains):
                continue
            label = "::".join(chains[index])
            if label:
                printed[identifier] = document_id + "::" + label
            else:
                # The preamble is the document's OWN text, and the address read_block prints for it
                # is the FILE PATH, never the storage id. Measured on the run of base 684fbb3: a
                # Dossier entry came back as '...partnership_and_enrollment_plan.md#preamble' -- one
                # storage id left in a surface that speaks a single grammar.
                printed[identifier] = document_id
        return printed

    def symbol_addresses(self, file_path: str) -> Dict[str, str]:
        """{stored symbol id -> printed address} for one file, in ONE pass."""
        rows = self._symbol_rows(file_path)
        if not rows:
            return {}
        labels = self._symbol_labels(rows)
        printed: Dict[str, str] = {}
        for identifier, chain in self._symbol_id_chains(rows).items():
            parts = [labels.get(step) for step in chain]
            if any(part is None for part in parts):
                continue
            printed[identifier] = file_path + "::" + "::".join(str(part) for part in parts)
        return printed

    def displayed_id(self, stored: str) -> str:
        """The printed path for ANY stored id: a paragraph, a symbol, or a plain path.

        Storage keeps file#slug and file::A.B; the surface prints file.md::Section::Paragraphe and
        file.cs::Classe::Methode, and that is what resolution accepts. A reply that says
        block_id='docs/g.md#guide' beside created_block_id='docs/g.md::Guide' is two grammars in one
        answer, and the caller has to guess which spelling a tool takes.
        """
        if not stored:
            return stored
        if "#" in stored:
            printed = self.displayed_section_id(stored)
            if printed != stored:
                return printed
            if stored.rsplit("#", 1)[1] == "preamble":
                # The preamble of a document IS the document's own text: its address is the path.
                return stored.split("#", 1)[0]
        if self.db.get_symbol(stored) is not None:
            return self.displayed_symbol_id(stored)
        if "::" in stored:
            # A path the surface prints, possibly spelled with its heading number: resolve it, then
            # print the address that IS canonical, so the caller can see the difference.
            resolved = self._resolve_displayed_path(stored)
            if resolved and "#" in resolved:
                return self.displayed_section_id(resolved)
            if resolved:
                return self.displayed_symbol_id(resolved)
        return stored

    def _address_hint(self, used: Any) -> str:
        """What to say when a caller spelled an address with its heading number.

        Both spellings resolve -- refusing a heading copied off the file would be a trap -- but only
        one is the address, and the caller should not have to discover which by trial.
        """
        seen: Dict[str, str] = {}
        for address in used or ():
            if not isinstance(address, str) or not address:
                continue
            canonical = self.displayed_id(address)
            if canonical != address:
                # parent and anchor are often the SAME address: say it once.
                seen.setdefault(address, canonical)
        pairs = list(seen.items())
        if not pairs:
            return ""
        quoted = "; ".join("'" + given + "' is written '" + canonical + "'" for given, canonical in pairs[:3])
        return (
            "addresses never carry the heading number -- the tools assign it, and it belongs to the "
            "heading, not to the name: " + quoted + ". Both resolve; the printed one is what the "
            "surface returns."
        )

    def unify_addresses(self, payload: Any, used: Any = ()) -> Any:
        """Rewrite a reply into the printed grammar: its address fields AND the ids its text quotes.

        Applied at the MCP boundary, so the storage layer keeps its own spelling and the caller only
        ever sees one. The strings are rewritten too, because a hint the caller copies verbatim is an
        address like any other -- and only the ids THIS payload names are replaced, so no unrelated
        text is touched.
        """
        if not isinstance(payload, dict):
            return payload
        out = dict(payload)
        if isinstance(out.get("blocks"), list):
            out["blocks"] = [self.unify_addresses(item) for item in out["blocks"]]
        stored_ids: List[str] = []
        for key in self.ADDRESS_FIELDS:
            value = out.get(key)
            if isinstance(value, str) and value:
                stored_ids.append(value)
            elif isinstance(value, list):
                stored_ids.extend(str(item) for item in value if item)
        table = {stored: self.displayed_id(stored) for stored in stored_ids}
        for key, value in list(out.items()):
            if key in self.ADDRESS_FIELDS and isinstance(value, str):
                out[key] = table.get(value, self.displayed_id(value))
            elif key in self.ADDRESS_FIELDS and isinstance(value, list):
                out[key] = [table.get(str(item), self.displayed_id(str(item))) for item in value]
            elif isinstance(value, str) and value:
                rewritten = value
                for stored, printed in table.items():
                    if stored != printed and stored in rewritten:
                        rewritten = rewritten.replace(stored, printed)
                out[key] = rewritten
        hint = self._address_hint(used)
        if hint:
            out["address_hint"] = hint
        return out

    def delete_block(
        self,
        path: Optional[str] = None,
        expected_heading: Optional[str] = None,
        *,
        paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Remove ONE or SEVERAL blocks addressed by PATH or PATHS.

        The path is the one the surface PRINTS (`fichier.md::Section mere::Paragraphe`,
        `fichier.cs::Classe::Methode`): a block that reads back must also delete. The dispatch
        used to look at the extension alone and hand the raw string to the section route, which
        answers for STORED ids only -- so `read_block('d.md::A')` returned the paragraph and
        `delete_block('d.md::A')` answered "Section 'd.md::A' not found in graph database":
        a cause that was not merely unhelpful, it was FALSE. A directory and a document root get
        their own cause too, instead of the same "not found".
        """
        if paths:
            results = [self.delete_block(p) for p in paths]
            all_ok = all(r.get("status") in ("success", "noop") for r in results)
            return {
                "status": "success" if all_ok else "error",
                "results": results,
                "deleted_count": sum(1 for r in results if r.get("status") == "success"),
            }
        if not path:
            return {
                "status": "error",
                "error": "delete_block requires 'path' (one block) or 'paths' (several blocks). Neither was given.",
                "actionable_hint": "delete_block(path=...) removes one block; delete_block(paths=[...]) removes several blocks at once.",
            }
        resolved = self._resolve_displayed_path(path) if "::" in path else None
        target = resolved or path
        block = self._block_and_kind(target)
        if block is not None:
            kind = block[0]
            if kind == "symbol":
                return self.delete_symbol(symbol_id=target)
            if kind == "section":
                return self.delete_document_section(
                    section_id=target, expected_heading=expected_heading
                )
            return {
                "status": "error",
                "id": path,
                "error": (
                    "'" + path + "' is the DOCUMENT ITSELF, not a block inside it: deleting it "
                    "would remove the whole file, and nothing was touched."
                ),
                "actionable_hint": (
                    "Delete its paragraphs one by one, by their paths, or remove the file "
                    "outside the tools."
                ),
            }
        # A row the live file no longer matches keeps its own, more precise refusal.
        if self.db.get_symbol(target) is not None:
            return self.delete_symbol(symbol_id=target)
        if self.db.get_document_section(target) is not None:
            return self.delete_document_section(section_id=target, expected_heading=expected_heading)
        absolute = self._existing_path(target)
        if absolute is not None and absolute.is_dir():
            return {
                "status": "error",
                "id": path,
                "error": (
                    "'" + path + "' is a DIRECTORY -- a block that HOLDS files -- and this tool "
                    "removes one block of one file, so nothing was touched."
                ),
                "actionable_hint": (
                    "Remove the files it holds one by one, or the directory outside the tools."
                ),
            }
        if absolute is not None and absolute.is_file():
            return {
                "status": "error",
                "id": path,
                "error": (
                    "'" + path + "' is a whole FILE, not a block inside one, and nothing was "
                    "touched."
                ),
                "actionable_hint": "Delete its symbols or its paragraphs one by one, by their paths.",
            }
        return self._unresolved_block(path)

    def delete_document_section(
        self,
        section_id: str,
        expected_heading: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Remove one Markdown paragraph (heading and body) and re-index the document."""
        section = self.db.get_document_section(section_id)
        if not section:
            return {
                "status": "error",
                "section_id": section_id,
                "error": f"Section '{section_id}' not found in graph database.",
            }
        displayed_id = self.displayed_section_id(section_id)

        file_path = section["document_id"]
        if not self._is_markdown(file_path):
            return {
                "status": "error",
                "section_id": section_id,
                "error": "Only Markdown documents (.md, .markdown, .mdx) can be edited in place; Word and PDF are read-only.",
            }

        abs_path, text, newline, error = self._resolve_markdown(file_path)
        if error:
            error["section_id"] = section_id
            return error

        layout = markdown_layout(text)
        target = next((item for item in layout if item["index"] == section["order_index"]), None)
        if target is None:
            return {
                "status": "error",
                "section_id": section_id,
                "error": "Section no longer exists in the file; re-run search_documents.",
            }

        mismatch = self._heading_mismatch(section_id, str(target["heading"]), expected_heading)
        if mismatch is not None:
            return mismatch

        # A section's span covers its heading and its OWN text: the paragraphs it holds are
        # NOT inside it, so they survive and move up to its parent. The description claimed a
        # deletion removes "everything it holds", and the reply said nothing about what it had
        # just reparented -- measured by the torture pass (defect 14). Name them.
        level = int(target["level"])
        survivors: List[str] = []
        for item in layout[layout.index(target) + 1 :]:
            if int(item["level"]) <= level:
                break
            survivors.append(str(item["heading"]))

        removed = text[target["section_start"] : target["section_end"]]
        new_text = text[: target["section_start"]] + text[target["section_end"] :]
        # Collapse the blank-line run left behind by the removal (best effort).
        new_text = re.sub(r"\n{4,}", "\n\n\n", new_text)

        if new_text == text:
            return {"status": "noop", "section_id": displayed_id, "message": "Nothing to delete."}

        self._atomic_write(abs_path, new_text.encode("utf-8"))
        self._reindex(abs_path)
        return {
            "status": "success",
            "section_id": displayed_id,
            "file_path": file_path,
            "heading": target["heading"],
            "removed_lines": removed.count("\n") + 1,
            # Named only when there are some: an empty list is noise for the 90% of
            # paragraphs that hold nothing.
            **({"reparented": survivors} if survivors else {}),
        }

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------
    def _reindex(self, abs_path) -> None:
        """Re-index a file a write just changed -- and NOTHING during a DRY RUN.

        A dry run that indexes is not a dry run. Measured on an index deliberately BEHIND
        the file: a routed call that a guard then REFUSED still left four database writes
        (upsert_document, reset_search_index, add_search_entries, set_file_hash), moved
        `indexed_files` and `documents.content_hash`, and doubled the WAL -- while the same
        refusal through the ordinary path left zero. `_atomic_write` suppresses the FILE
        write; this suppresses the INDEX, which is the other half of the same consequence.
        """
        if self._capture_writes is not None:
            return
        self.indexer.index_file(abs_path)

    def _atomic_write(self, target_path: Path, content_bytes: bytes) -> None:
        """Write bytes to a temp file and atomically replace target."""
        if self._capture_writes is not None:
            # A DRY RUN: the caller asked what WOULD be written (see _title_route), so the
            # guards that read the result can be applied before anything moves. Checked
            # before _preview_pass, because a dry run inside a validation pass still needs
            # the bytes.
            self._capture_writes.append(content_bytes)
            return
        if self._preview_pass:
            # A VALIDATION PASS writes nothing (see Patcher._edit_batch). It returns before the
            # watcher suppression too: nothing changed, so nothing has to be told that it did.
            return
        if self._batch_after is not None:
            # The state THIS call leaves the file in, and which entry left it there. It is what
            # tells OUR OWN write from a THIRD PARTY one, and its warning names the entry.
            self._batch_after[str(target_path)] = (content_bytes, self._batch_entry_index)
        # THE REBASER IS TOLD NOW, not when the reply is written: the next entry numbers are
        # expressed against the version the caller READ, and this is the write that moved them.
        self._record_write(str(target_path), content_bytes)

        if self.suppression:
            self.suppression.suppress(target_path)

        commit_id = None
        if (
            self.history_manager is not None
            and not self._restoring_files
            and self._batch_originals is None
        ):
            target_exists = target_path.exists()
            before_bytes = target_path.read_bytes() if target_exists else None
            before_sha = self.history_manager.save_blob(before_bytes) if before_bytes is not None else None
            after_sha = self.history_manager.save_blob(content_bytes)
            rel_path = self._relative_path(target_path)
            commit_id = str(uuid.uuid4())
            parent_id = self.history_manager.get_head_commit_id()
            tool_name = "insert_block" if (before_sha is None) else "edit_block"
            self.history_manager.record_pending_commit(
                commit_id=commit_id,
                parent_id=parent_id,
                kind="scalpel_edit",
                tool_name=tool_name,
                files=[{
                    "file_path": rel_path,
                    "before_sha": before_sha,
                    "after_sha": after_sha,
                }],
            )

        tmp_name = f".tmp_{uuid.uuid4().hex}"
        tmp_path = target_path.parent / tmp_name
        try:
            tmp_path.write_bytes(content_bytes)
            os.replace(tmp_path, target_path)
            if commit_id is not None:
                self.history_manager.commit_transaction(commit_id)
        except Exception:
            if commit_id is not None:
                self.history_manager.abort_transaction(commit_id)
            raise
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

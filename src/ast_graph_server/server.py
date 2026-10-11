"""Model Context Protocol (MCP) server implementation for AST Code-Graph."""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .config import ServerConfig
from .context_builder import ContextBuilder
from .database import DatabaseManager
from .documents import resolve_referenced_files
from .embedding import load_embedder
from .externals import (
    MAX_EXTERNAL_MATCHES,
    external_query_terms,
    render_external_contract,
    render_external_matches,
)
from .indexer import Indexer
from .lineops import numbered, format_single_edit_result, format_batch_edit_result
from .parsers import compute_content_hash
from .patcher import Patcher
from .rendering import (
    render_document_matches,
    render_documents,
    render_outline,
    render_project_map,
    render_section_batch,
    render_symbol_matches,
    render_symbol_view,
)
from .watcher import SuppressionRegistry, WorkspaceWatcher


#: The contract of read_block, kept in ONE text: the harness sends its OWN copy to the
#: model (benchmarks/toolsets.py), and tests/test_surface_agreement.py pins the two
#: together, because a capability promised by one surface and refused by the other is
#: worse than no capability at all.
READ_BLOCK_DESCRIPTION = (
    "A PATH returns the COMPLETE list of that block's children -- never paged, never truncated: a "
    "directory its entries ('/' marks a directory), a code file its symbols, a .md file its paragraphs "
    "plus the text no heading owns (with its hash), a .csproj/.json/.yaml its named properties. Each "
    "child carries its SHORT name inside its parent (never a full address; an overload adds its parameter "
    "list), its line span and its size (bytes for code, characters for a document), nested by "
    "indentation; a symbol also carries its KIND and its DECLARATION (class Spark, method "
    "Connect(string master) -> SparkContext), so the listing says what each member IS without reading "
    "it. A SYMBOL or a PARAGRAPH returns TEXT too: ONE header line (its address, its [kind], its [h:hash] and its "
    "number of lines), then its lines numbered from 1 -- the hash in the header is the expected_hash "
    "edit_block takes, and format='json' returns the same block as an object (id, content_hash, "
    "line_count, numbered_text). Address a child by writing its parent's path once, then '::' and the name printed "
    "here (src/a.cs::Classe::Methode, docs/spec.md::Section mere::Paragraphe); inside a directory, '/' "
    "and the name. Case, spaces, accents and punctuation are folded, so a copied name resolves; a short "
    "name unique in its file may be written alone; a name several blocks of one file share is refused, "
    "naming every candidate with its parameters and lines. A LISTING is rendered as text or as JSON -- "
    "the server's answer_format decides, and this call's format overrides it: text prints the block's "
    "own path on the first line, then the children indented under it, and comes back AS TEXT -- its "
    "line breaks are line breaks; JSON returns the object tree. A file NO parser and NO document reader "
    "claims (a .ps1, a .txt, a shell script) returns its WHOLE text, numbered from 1 with its hash, and is "
    "READ-ONLY. Ask for SEVERAL blocks in ONE call with paths=[...] -- THIS is what makes ONE turn read N blocks, and it is the biggest saving the tool offers: a block you would otherwise read on its OWN turn costs a turn, and a run that reads one block at a time pays for every one of them; if the paths you need are not yet known, name the ones you DO know in the same call instead of one call each: every requested block comes back in "
    "full, with its own numbered text and hash, so N blocks cost one turn instead of N. A WRITE takes a "
    "block, never a listing: a "
    "symbol or a paragraph is one, and so is a document path (its root text); a code file or a directory "
    "is not -- name one of the blocks printed under it."
)



def empty_document_list_note(
    db: DatabaseManager, file_pattern: Optional[str], on_disk: int = 0
) -> str:
    """Why `list_documents` is empty -- a checked fact, never an empty body.

    An empty text made the tool indistinguishable from a path that does not exist: an
    agent asked it about three `.cs` files, got `count 0` with nothing else, and said so
    -- "The empty success result is ambiguous -- it reads like \"file does not exist\"
    when in fact the tool simply does not index source files" (mapab2 b r2 of the base
    c819545). This tool lists Markdown, Word and PDF documents; the index knows whether
    the pattern matched a source file instead, so the answer says which of the two it is.
    """
    matching = db.files_matching(file_pattern) if file_pattern else []
    if matching:
        sample = ", ".join(matching[:3])
        return (
            "No document matches '" + str(file_pattern) + "', but " + str(len(matching))
            + " indexed file(s) do: " + sample + ". This tool lists Markdown, Word and PDF "
            "documents only; a source file is read with read_block or search_symbols."
        )
    if file_pattern:
        if on_disk:
            return (
                "No document matches '" + str(file_pattern) + "', and no INDEXED file matches "
                "it -- but " + str(on_disk) + " file(s) do on disk: this tool lists Markdown, "
                "Word and PDF documents, and Scalpel parses code files, so neither covers them."
            )
        return (
            "No document matches '" + str(file_pattern) + "', and no indexed file matches it "
            "either (check the path). This tool lists Markdown, Word and PDF documents only."
        )
    return "No document is indexed yet."

def _file_pattern_clause(db: DatabaseManager, file_pattern: str, on_disk: int = 0) -> str:
    """What a `file_pattern` selected: a checked fact, not a taught rule.

    The message used to add "(a folder is 'folder/*')". That became false the day a
    folder started scoping to its content (`path_matches`), and it stayed on the
    document note for one more campaign: in that run the pattern WAS a folder and had
    been applied to the search, yet the empty answer still told the agent to rewrite
    it (found on mapab2 a r1 of the base edfe80a). So the tool says what the index
    holds -- no file matches, or files match and none of them carries the query.
    """
    matching = db.files_matching(file_pattern)
    if not matching:
        if on_disk:
            return (
                "no INDEXED file matches it, but " + str(on_disk) + " file(s) do on disk: "
                "Scalpel parses code files and documents, and those are neither (check the "
                "extension). A folder scopes to its content, e.g. 'src' or 'src/*'"
            )
        return (
            "no indexed file matches that pattern (check the path; a folder scopes to "
            "its content)"
        )
    return str(len(matching)) + " indexed file(s) match it and none carries the query"


def empty_symbol_search_note(
    db: DatabaseManager,
    query: str,
    kind: str,
    file_pattern: Optional[str],
    on_disk: int = 0,
) -> str:
    """Why a symbol search is empty -- and NEVER a cause the tool has not checked.

    The first version of this message blamed "an external package" for a symbol that
    a file_pattern had merely filtered out; an agent called it out: "the message's
    explanation ('a type from an external package') was misleading since the symbol
    is local" (mapab2 b r2 of the base 7c0c5bb). So the tool re-runs the SAME query
    without the filter and reports what it finds: a fact, not a guess.
    """
    if file_pattern:
        elsewhere = db.search_symbols(query=query, kind=kind, limit=5)
        clause = _file_pattern_clause(db, file_pattern, on_disk)
        if elsewhere:
            sample = ", ".join(match["symbol_id"] for match in elsewhere[:3])
            return (
                "No symbol matches '" + query + "' under file_pattern '" + file_pattern
                + "': " + clause + ". " + str(len(elsewhere))
                + " match the same query WITHOUT it: " + sample
                + ". Retry without file_pattern, or widen it."
            )
        return (
            "No symbol matches '" + query + "' under file_pattern '" + file_pattern
            + "': " + clause + "."
        )
    # The old text told the agent to "try search_documents" without showing what
    # that search WOULD return, so 71 runs each burned a turn on a symbol query
    # for a requirement that the Dossier states in prose and that
    # search_documents already answers with hits (subagent s2). Say the checked
    # fact instead of teaching the rule.
    prose = db.search_documents(query=query, limit=5)
    if prose:
        sample = ", ".join(str(hit["section_id"]) for hit in prose[:3])
        clause = (
            " No symbol does, but " + str(len(prose)) + " document paragraph(s) do: " + sample
            + ". The requirement is in prose, not in code."
        )
    else:
        clause = (
            " No document paragraph carries it either. Try one key word instead of a sentence, "
            "or read a file that USES the symbol to see its namespace."
        )
    return (
        "No symbol matches '" + query + "' in the workspace. Matching is on names and ids, never on "
        "bodies. A symbol of an external package is searched only when a .csproj references that "
        "package and its XML documentation is in the local NuGet cache; any other external symbol "
        "is not indexed." + clause
    )


def empty_document_search_note(
    db: DatabaseManager,
    query: str,
    file_pattern: Optional[str],
    on_disk: int = 0,
) -> str:
    """Why a document search is empty -- checked, not guessed.

    Same doctrine as empty_symbol_search_note: three of these came back as bare
    `count 0` with an empty body in one run (base c980d51), each costing a manual
    widening by the agent, which said so: "no matches and no next-step guidance".
    """
    if file_pattern:
        elsewhere = db.search_documents(query=query, limit=3)
        clause = _file_pattern_clause(db, file_pattern, on_disk)
        if elsewhere:
            sample = ", ".join(str(match["section_id"]) for match in elsewhere[:3])
            return (
                "No paragraph matches '" + query + "' under file_pattern '" + file_pattern
                + "': " + clause + ". " + str(len(elsewhere))
                + " match the same query WITHOUT it: " + sample
                + ". Retry without file_pattern, or widen it."
            )
        return (
            "No paragraph matches '" + query + "' under file_pattern '" + file_pattern
            + "': " + clause + ". Matching is term by term, on headings and bodies; "
            "a code identifier usually lives in code, which search_symbols covers."
        )
    return (
        "No paragraph matches '" + query + "'. Matching is term by term, on headings and bodies; "
        "try fewer or different terms. An identifier that lives in code is not a document: "
        "search_symbols covers it."
    )


def edit_block_shape_refusal(
    path: Optional[str],
    ops: Optional[List[Dict[str, Any]]],
    expected_hash: Optional[str],
    edits: Any,
) -> Dict[str, str]:
    """Why an `edit_block` call wrote nothing, said with the keys it ACTUALLY received.

    Measured on registered_v2: four refusals read "Neither form was complete, so nothing
    was written", and every one of those calls had reached the tool as an EMPTY argument
    map (benchmarks/agent.py turns a call whose own JSON does not parse into `{}`), so the
    sentence described a shape the caller had not sent. An agent that HAD mixed a top-level
    path/ops/expected_hash with `edits=` was told nothing about the key in excess either:
    "the message did not name *which* extra keys were unexpected". The refusal names what
    arrived, what the nearest form is missing, and what is in excess for it; when nothing
    arrived, it says that. Nothing was written in any of these cases.
    """
    given = (("path", path), ("ops", ops), ("expected_hash", expected_hash), ("edits", edits))
    received = [name for name, value in given if value is not None]
    if not received:
        return {
            "error": (
                "edit_block received NONE of its keys: path, ops, expected_hash and edits are all "
                "absent from this call's arguments, so nothing was written."
            ),
            "actionable_hint": (
                "Nothing reached the tool, so there is no shape to correct: a call whose arguments "
                "were dropped arrives exactly like this -- resend it whole. One block: "
                "edit_block(path='<the block>', ops=[...], expected_hash=...). Several: "
                "edit_block(edits=[{path, ops, expected_hash}, ...])."
            ),
        }
    # An empty list is the only `edits` that reaches this refusal: a filled one takes the batch
    # branch. It names no block, and it is a POSITION, so it says nothing about path or ops.
    empty_edits = "edits" in received and not edits
    one_block = [name for name in received if name != "edits"]
    said = "edit_block received " + ", ".join(received) + ": "
    if one_block:
        missing = [name for name in ("path", "ops") if name not in received]
        said += "the ONE block form (path + ops) is the one you were filling"
        if missing:
            said += ", and it is missing " + " and ".join(missing)
    else:
        said += "the SEVERAL blocks form (edits=[...]) is the one you were filling"
    if empty_edits:
        said += ((", but " if one_block else ", and ")
                 + "edits is an EMPTY list, so it names no block")
        if one_block:
            said += ". edits is the key in excess for the ONE block form: drop it, or fill it"
    said += ". Nothing was written."
    if one_block:
        hint = (
            "Complete that call: edit_block(path='<the block>', ops=[{replace: [first, last], "
            "content: '...'}], expected_hash=...). For SEVERAL blocks in one call, use "
            "edits=[{path, ops, expected_hash}, ...] instead -- one top-level path names one block."
        )
    else:
        hint = (
            "Give every entry of edits its own path and ops: edit_block(edits=[{path, ops, "
            "expected_hash}, ...]). For ONE block: edit_block(path='<the block>', ops=[...])."
        )
    return {"error": said, "actionable_hint": hint}


class ASTGraphServer:
    """Encapsulates the MCP Server, database, indexer, and patcher components."""

    def __init__(self, config: Optional[ServerConfig] = None) -> None:
        self.config = config or ServerConfig.from_env()
        self.db = DatabaseManager(self.config.db_path, workspace_root=self.config.workspace_root)
        self.embedder = load_embedder(self.config.embedder, cache_dir=self.config.embedding_cache_dir)
        self.indexer = Indexer(self.config, self.db, embedder=self.embedder)
        self.context_builder = ContextBuilder(self.config, self.db, embedder=self.embedder)
        self.suppression = SuppressionRegistry()
        self.patcher = Patcher(self.config, self.db, self.indexer, suppression_registry=self.suppression)
        # The Dossier hands out addresses the tools accept: one grammar, and only one printer.
        self.context_builder.addresses = self.patcher
        self.watcher = WorkspaceWatcher(self.config, self.indexer, suppression_registry=self.suppression)
        self.mcp = MCPServer("ast-code-graph")
        self._register_tools()

    def initialize(self, enable_watcher: bool = True, reindex: bool = False) -> int:
        """Perform initial workspace indexing and start background file watcher.

        Thanks to the persisted file hashes, a restart only re-parses the files
        that changed since the previous run. reindex=True forces a full re-parse.
        """
        indexed = self.indexer.index_workspace(force=reindex)
        if enable_watcher:
            self.watcher.start()
        return indexed

    def _designates_a_code_file(self, path: str) -> bool:
        """Does this NAME designate a source file the SYMBOL machinery can address?

        Asked of the parser registry the indexer already holds, never of a suffix list kept
        here: a fourth language must not need a second registration to be addressable. Used
        only for a path that is NOT on disk -- what exists is decided on the file system.
        """
        registry = getattr(self.indexer, "registry", None)
        if registry is None:
            return False
        return registry.get_parser_for_file(path) is not None

    def _referenced_files(self, text: Optional[str]) -> List[str]:
        """Workspace files mentioned inside a document paragraph."""
        if not text:
            return []
        mapping: Dict[str, List[str]] = {}
        for path in self.db.get_indexed_paths():
            mapping.setdefault(path.split("/")[-1], []).append(path)
        return resolve_referenced_files(text, mapping)

    def _symbol_view(self, symbol_id: str, view: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Read one symbol at the requested detail level; returns (payload, error).

        Shared by inspect_symbols and search_symbols so that "find something, then
        look at it" never costs two turns: a search can inline the very detail the
        caller would otherwise have asked for next.
        """
        sym = self.db.get_symbol(symbol_id)
        if not sym:
            return None, "Symbol '" + symbol_id + "' not found in graph database."

        if view == "implementation":
            try:
                abs_path = self.config.to_absolute_path(sym.file_path)
            except ValueError:
                # One poisoned row must not kill the whole call: search_symbols and
                # inspect_symbols are batched, and the uncaught containment error took
                # the entire batch down (measured on an out-of-root junction row).
                return None, (
                    "Symbol '" + symbol_id + "' lives outside the workspace root, so its "
                    "content is refused."
                )
            if not abs_path.exists():
                return None, "File '" + sym.file_path + "' not found on disk."
            try:
                file_bytes = abs_path.read_bytes()
                raw_slice = file_bytes[sym.start_byte : sym.end_byte]
                if compute_content_hash(raw_slice) != sym.content_hash:
                    # The file changed outside the tools, so the stored offsets no
                    # longer describe this symbol. A silently shifted slice is
                    # worse than an error: the agent would act on it. Re-index and
                    # retry once, then report the symbol as stale.
                    #
                    # And the re-index must not be able to TAKE THE CALL DOWN: this is a
                    # read that writes, on the very path `search_symbols(view=...)` uses,
                    # and a database locked by another connection escaped as the SDK's
                    # generic "Error executing tool search_symbols" with no cause at all
                    # (the black-out pass lost 18 calls to exactly that message). Failing
                    # to refresh decides nothing: the hash test below still refuses a
                    # slice it cannot trust.
                    try:
                        self.indexer.index_file(abs_path)
                    except Exception:
                        pass
                    refreshed = self.db.get_symbol(symbol_id)
                    if refreshed is not None:
                        sym = refreshed
                        file_bytes = abs_path.read_bytes()
                        raw_slice = file_bytes[sym.start_byte : sym.end_byte]
                if compute_content_hash(raw_slice) != sym.content_hash:
                    return None, (
                        "stale symbol: the file changed outside the tools and the stored offsets "
                        "no longer match; re-read the file before using this slice."
                    )
                code = raw_slice.decode("utf-8", errors="replace")
                return {
                    "view": "implementation",
                    "file": sym.file_path,
                    # File-absolute lines, for orientation only: the coordinates
                    # edit_block takes are the block-relative ones in numbered_code.
                    "lines": [sym.start_line, sym.end_line],
                    "line_count": len(code.splitlines()),
                    # Same numbering and same hash as read_block, so this view is
                    # directly editable: no read_block before the first edit.
                    "content_hash": compute_content_hash(code.encode("utf-8")),
                    "numbered_code": numbered(code, 1),
                    "code": code,
                    "callers": self.db.get_callers(sym.name, symbol_id),
                }, None
            except Exception as e:
                return None, "Failed to read file slice: " + str(e)

        calls = self.db.get_symbol_calls(symbol_id)
        callers = self.db.get_callers(sym.name, symbol_id)
        return {
            "view": "contract",
            "signature": sym.signature,
            "docstring": sym.docstring,
            "calls": [c["callee_name"] for c in calls],
            "callers": callers,
        }, None

    def _disk_matches(self, file_pattern: Optional[str]) -> int:
        """How many files on disk the pattern selects -- asked only when the index has none.

        A pattern naming a `.csproj` selects nothing indexed, and the tool told the caller to
        check the path, which was right: the file is on disk, Scalpel simply does not parse
        that extension. Counting it turns a suspicion into the checked fact, and the agent
        asked for exactly that signal (declared on mapab2 a r1 of the base 93f3a9a).
        """
        if not file_pattern:
            return 0
        try:
            root = Path(self.config.workspace_root)
            return sum(1 for path in root.glob(file_pattern) if path.is_file())
        except (ValueError, OSError, NotImplementedError):
            return 0

    # -- external (NuGet) contracts -----------------------------------------------------

    def _external_entries(self, address: Any) -> List[Dict[str, Any]]:
        """The external contract entries an address names, or [] for a workspace address.

        The cheap shape test runs first (ExternalIndex.lookup rejects '/', '::' and names without
        a dot), and a name that EXISTS in the workspace is never external: a workspace block
        always wins over a package contract of the same spelling.
        """
        externals = getattr(self.indexer, "externals", None)
        if externals is None or not isinstance(address, str) or not address.strip():
            return []
        try:
            entries = externals.lookup(address)
        except Exception:
            return []
        if not entries:
            return []
        try:
            if self.patcher._existing_path(address.strip()) is not None:
                return []
        except Exception:
            pass
        return entries

    def _external_read(self, address: str, entries: List[Dict[str, Any]], answer_format: Optional[str]) -> Dict[str, Any]:
        """read_block on an external address: its CONTRACT (never a source text, never a hash)."""
        externals = self.indexer.externals
        members: List[Dict[str, Any]] = []
        for entry in entries:
            if entry["kind"] == "external_type":
                members = externals.members_of(entry["symbol_id"])
                break
        mode = str(answer_format or getattr(self.config, "answer_format", "text") or "text").lower()
        if mode == "json":
            return {
                "status": "success",
                "external": True,
                "editable": False,
                "path": address,
                "origin": entries[0]["origin"],
                "entries": entries,
                "members": members,
            }
        return {
            "status": "success",
            "format": "text",
            "text": render_external_contract(address, entries, members),
        }

    @staticmethod
    def _external_refusal(tool: str, address: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A write aimed at an external symbol: refused, with the reason and the next step."""
        entry = entries[0]
        return {
            "status": "error",
            "external": True,
            "error": (
                "'" + address + "' is an EXTERNAL symbol (" + entry["origin"] + "): " + tool + " cannot "
                "change it. Its contract comes from the package's XML documentation; there is no source "
                "file for it in this workspace, so nothing was written."
            ),
            "actionable_hint": (
                "Edit the workspace code that USES it instead. Its contract (signatures, doc, members) "
                "is what read_block(path='" + entry["displayed_id"] + "') returns."
            ),
        }

    def _register_tools(self) -> None:
        """Register the core MCP tools."""

        @self.mcp.tool(
            name="search_symbols",
            description=(
                "Finds functions, methods, or classes matching a query across the codebase. Pass "
                "view='contract' or 'implementation' to also read what it finds, in the same call. An "
                "implementation view carries 1-based line numbers and a content_hash -- the very ones "
                "edit_block expects -- so it can be edited straight from the result, with no read_block. "
                "After the workspace matches, it also lists a few EXTERNAL symbols -- types, methods and "
                "properties of the NuGet packages the .csproj files reference, read from the packages' XML "
                "documentation -- marked [external_type]/[external_method]/[external_property] with their "
                "package and version: CONTRACT ONLY, the exact name to write in code, never editable."
            ),
        )
        async def search_symbols(
            query: str = Field(..., description="Symbol name or substring (e.g., 'process_transaction' or 'AuthService')."),
            kind: Literal[
                "function",
                "method",
                "class",
                "interface",
                "namespace",
                "struct",
                "record",
                "enum",
                "property",
                "constructor",
                "destructor",
                "operator",
                "all",
            ] = Field(
                default="all",
                description=(
                    "Filter by symbol kind: function, method, class, interface, namespace, struct, "
                    "record, enum, property, constructor, destructor, operator, or all."
                ),
            ),
            path: Optional[str] = Field(
                default=None, description="Optional glob filter (e.g., 'src/**/*.py')."
            ),
            limit: int = Field(default=50, description="Maximum number of matches."),
            view: Literal["none", "contract", "implementation"] = Field(
                default="none",
                description=(
                    "Inline the detail of every match IN THIS CALL: 'contract' for signature/docstring/"
                    "calls/callers, 'implementation' for the source. The source of EVERY match this call "
                    "returns is inlined -- 'limit' is the ceiling on how many that is -- and every id "
                    "found is listed, so there is no second turn to read what was left out. Use it to "
                    "read what you just found instead of reading the same ids one by one."
                ),
            ),
            format: Literal["text", "json"] = Field(
                default="text",
                description=(
                    "text (default) returns one compact line per item: ~50% fewer tokens than the JSON "
                    "envelope, which repeats derivable fields. json returns the structured array."
                ),
            ),
        ) -> Any:
            matches = self.db.search_symbols(query=query, kind=kind, file_pattern=path, limit=limit)
            # The stored id of an overload carries a sha1 the caller can neither read nor
            # retype: the match is DISPLAYED under its printed path, and keyed internally by
            # the id the views and the errors are built from.
            for match in matches:
                match["displayed_id"] = self.patcher.displayed_symbol_id(match["symbol_id"])
            views: Dict[str, Any] = {}
            errors: Dict[str, str] = {}
            # A search is not a list the caller chose, so a search never answers a match it
            # skipped with an error each: a query matching 50 symbols produced 45
            # `[error] implementation view limit exceeded` lines for symbols the caller never
            # asked about, burying the two it wanted -- which it then read with read_block
            # anyway (declared on mapab2 a r1 of the base 9662958).
            #
            # And a search never skips a match it DID return either: the hidden cap of
            # `max_implementation_batch` implementations made view='implementation' a lie --
            # limit=10 changed nothing, and the reply still answered "the cap is 3 per call"
            # -- measured on registered_v2: 31 replies announced omitted implementations and
            # 148 implementations were left out, in 17 of the 20 scalpel runs, where the three
            # calls that followed were always reads of what had been omitted. The ceiling is
            # the caller's own `limit`: it bounds how many matches this call returns, and the
            # source of every one of them is inlined.
            if view != "none":
                for match in matches:
                    symbol_id = match["symbol_id"]
                    payload, error = self._symbol_view(symbol_id, view)
                    if error:
                        errors[symbol_id] = error
                        continue
                    views[symbol_id] = payload
            # An empty answer is a dead end unless it says why it is empty -- and it must
            # say what was CHECKED, not a cause it merely suspects. Built BEFORE the
            # format branch, because the JSON form of the SAME call was still answering
            # {"matches": []}: no status, no count, no note, so a caller asking for json
            # got a success it could not act on -- the very default the text form had
            # just been cured of (214 blank successes in the corpus, base 7c0c5bb).
            note = ""
            if not matches and not errors:
                note = empty_symbol_search_note(
                    self.db, query, kind, path, self._disk_matches(path)
                )
            # EXTERNAL contracts (NuGet packages the .csproj files reference), AFTER the workspace
            # matches and outside their `limit`: the workspace stays first, and the externals are
            # capped (MAX_EXTERNAL_MATCHES, 3 once the workspace answered) and, when the workspace
            # answered, restricted to the ones that carry every term or are named by one. A `path`
            # scopes the search to workspace files, so it excludes them. Measured cause: 10 runs of
            # 55 invented the Spark builder's type name (SparkSession.Builder / SparkSessionBuilder)
            # because the real one, Microsoft.Spark.Sql.Builder, lives in the package only.
            external: List[Dict[str, Any]] = []
            externals = getattr(self.indexer, "externals", None)
            if externals is not None and not path:
                cap = MAX_EXTERNAL_MATCHES if len(matches) < MAX_EXTERNAL_MATCHES else 3
                try:
                    external = externals.search(
                        external_query_terms(query), kind=kind, limit=min(cap, max(limit, 1)),
                        strict=bool(matches),
                    )
                except Exception:
                    external = []
            text = render_symbol_matches(matches, views, errors)
            if note:
                text = note
            if external:
                text = (text + "\n\n" if text else "") + render_external_matches(external)

            if format == "json":
                if views or errors:
                    reply: Dict[str, Any] = {"matches": matches, "views": views, "errors": errors}
                    if external:
                        reply["external_matches"] = external
                    return reply
                payload: Dict[str, Any] = {
                    "status": "success",
                    "count": len(matches),
                    "matches": matches,
                    "text": text,
                }
                if note:
                    payload["note"] = note
                if external:
                    payload["external_count"] = len(external)
                    payload["external_matches"] = external
                return payload
            return text


        @self.mcp.tool(name="read_block", description=READ_BLOCK_DESCRIPTION)
        async def read_block(
            path: Optional[str] = Field(
                default=None,
                description=(
                    "The path of ONE block: a file path, or a member path built with :: from the "
                    "SHORT names read_block itself prints -- see the tool description for the contract."
                ),
            ),
            paths: Optional[List[str]] = Field(
                default=None,
                description=(
                    "SEVERAL blocks in ONE call: a list of symbol or paragraph paths. The reply "
                    "carries one numbered text -- and its hash -- per block, so N blocks cost one turn "
                    "instead of N. Use it to reread a batch you already know; use `path` to discover."
                ),
            ),
            format: Optional[Literal["text", "json"]] = Field(
                default=None,
                description=(
                    "How the answer is rendered. A LISTING (a file's symbols, a folder's entries, a project "
                    "file's properties): 'text' prints the block's own path on the first line, then "
                    "its children indented, each with its kind and its declaration (~45% fewer "
                    "characters than 'json' for the same content). A BLOCK (a symbol or a section): 'text' "
                    "prints one header line (address, kind, hash, line count), then the numbered lines "
                    "as they are -- no escaping; 'json' returns the object tree for both. "
                    "Defaults to the server's configured answer format."
                ),
            ),
        ) -> Any:
            # Any, not Dict: a text LISTING is returned as a plain string, and an output
            # schema built from `Dict[str, Any]` makes the MCP layer refuse it.
            if paths:
                blocks = [_text_block(one, format) for one in paths]
                if (format or getattr(self.config, "answer_format", "text")) == "text":
                    # TEXT: ONE text, whatever the blocks answered. A block is already text (header
                    # line + numbered lines, or a listing); a refusal is a dict, rendered as one
                    # '[error] <address>: <reason>' line IN PLACE, so the caller's order is kept and
                    # nothing is wrapped in an envelope that would escape the other blocks' lines.
                    rendered: List[str] = []
                    for block in blocks:
                        if isinstance(block, str):
                            rendered.append(block)
                        elif isinstance(block, dict) and block.get("status") == "error":
                            line = "[error] " + str(block.get("id") or "?") + ": " + str(block.get("error"))
                            if block.get("actionable_hint"):
                                line += "\n  hint: " + str(block["actionable_hint"])
                            rendered.append(line)
                        else:
                            rendered.append(json.dumps(block, ensure_ascii=False))
                    return "\n\n".join(rendered)
                # JSON: always the envelope, and only objects inside it -- the same form whether
                # every block, none or some of them could be read.
                return {
                    "blocks": [
                        block if isinstance(block, dict) else {"status": "success", "text": block}
                        for block in blocks
                    ]
                }
            if not path:
                return {
                    "status": "error",
                    "error": (
                        "read_block needs `path` (one block, or a file/folder to LIST) or `paths` "
                        "(several text blocks in one call). Neither was given."
                    ),
                    "actionable_hint": "read_block(path=...) reads or lists one block; read_block(paths=[...]) reads several text blocks at once.",
                }
            return _text_block(path, format)
        # NOT a closure over the tool's `format`: this helper is a SIBLING of the tool
        # function, so a bare `format` here resolves to the BUILTIN -- the answer stayed
        # JSON while the patcher, called directly, answered text (found by a server-level
        # test, tests/test_listing_format.py::test_the_mcp_surface_honours_the_format).
        def _text_block(one: str, answer_format: Optional[str] = None) -> Any:
            """One block: the text of a LISTING, or the structured payload of everything else."""
            # An EXTERNAL address (a NuGet type or member search_symbols listed) has no file and no
            # block: it is answered with its contract, never with a "not found" that would send
            # the caller back to guessing the name.
            effective_format = answer_format or getattr(self.config, "answer_format", "text")
            external = self._external_entries(one)
            if external:
                payload = self._external_read(one, external, effective_format)
                if isinstance(payload, dict) and payload.get("format") == "text":
                    return payload["text"]
                return payload
            payload = self.patcher.read_block(one, answer_format=effective_format)
            if isinstance(payload, dict) and payload.get("format") == "text":
                # A text listing goes on the wire as TEXT. Wrapped in the JSON envelope, its
                # line breaks arrive escaped -- '"...\n  class Spark"' -- which is exactly
                # what this render exists to avoid; the sibling tools that answer
                # {"format": "text", "text": ...} have been paying that cost all along.
                return payload["text"]
            if isinstance(payload, dict) and payload.get("numbered_text"):
                payload["referenced_files"] = self._referenced_files(payload["numbered_text"])
            # One grammar on the way out: the caller never sees a stored id. The address the caller
            # USED is passed too, so a heading number in it is answered with its canonical form.
            unified = self.patcher.unify_addresses(payload, used=[one])
            if (
                effective_format == "text"
                and isinstance(unified, dict)
                and unified.get("status") == "success"
                and unified.get("numbered_text")
            ):
                # A numbered TEXT block goes on the wire as TEXT too: inside the JSON envelope
                # every line break and quote of the code arrives escaped. The header keeps
                # what the caller needs to edit (address, hash, line count); the numbered
                # lines follow verbatim, and the hint (if any) closes the answer.
                header = (
                    f"{unified.get('id', one)} [{unified.get('kind', 'block')}] "
                    f"[h:{unified.get('content_hash')}] ({unified.get('line_count')} lines)"
                )
                parts = [header, unified["numbered_text"]]
                if unified.get("address_hint"):
                    parts.append(f"hint: {unified['address_hint']}")
                return "\n".join(parts)
            return unified


        @self.mcp.tool(
            name="edit_block",
            description=(
                "Change ONE block by LINE, instead of resending it. ops is a list of operations on the "
                "1-based numbers read_block returned: {\"replace\": [first, last], \"content\": \"...\"}, "
                "{\"delete\": [first, last]}, or {\"insert_after\": n, \"content\": \"...\"} (n=0 inserts at the "
                "top). 'replace' takes a RANGE: [first, last] with last > first replaces that whole "
                "sub-block, so merge adjacent changes into ONE operation instead of one operation per line. "
                "The range is INCLUSIVE: the line just outside it (a-1 or b+1) is NOT consumed, so content "
                "that re-emits that boundary line would leave the block holding it twice: that edit is "
                "REFUSED, nothing is written, and both line numbers are named. An insert_after that "
                "re-emits a line the block already carries is NOT refused (there is no range to extend): it "
                "is written "
                "and the copy is reported in warnings. An insert_after the block's LAST line in a CODE block "
                "writes into the FILE (the code becomes a sibling): the block keeps its own hash, and the "
                "reply says written_outside_block and returns the file listing with the address of what was "
                "created -- never send it again. "
                "Ops are applied BOTTOM-UP on the numbers you read, so an op never shifts the ones still "
                "to apply: an insert_after just above a replace lands BEFORE the replaced lines. To replace "
                "a line and insert next to it, make it ONE replace over the whole range. "
                "Pass expected_hash from read_block: if the block moved on, the edit is refused rather "
                "than applied to the wrong lines, and if the stamp belongs to ANOTHER block the refusal "
                "NAMES that block -- a hash opens one block, and only that one. Send only the new content, at the indentation you see "
                "(read_block shows the REAL indentation): your lines are inserted literally, while the tool "
                "keeps the block's base aligned to its AST level. The reply returns the updated numbered block "
                "and its new hash, so you can chain edits "
                "without re-reading. There is no whole-block variant: a full rewrite is a replace over lines "
                "1..line_count. "
                "edit_block edits INSIDE one block and never changes the outline: in Markdown, when EVERY "
                "operation is an insertion (insert_after only -- no replace and no delete) and the content "
                "opens consecutive headings at the block's level (or a heading with subheadings), they "
                "cannot live inside the block, so they are CREATED as SIBLINGS right AFTER it and the reply "
                "carries insert_block's own fields (the id of the block it created, the parent it was created "
                "under, where it landed, its heading, its level and its number, and every id the call created) "
                "plus placement: 'after', host_block_unchanged: true and the host's content_hash, which still "
                "opens the block you edited -- the renumbering of the following paragraphs is reported in "
                "warnings. In C# code, adding only using directives to a namespace block is routed to the "
                "file's using region. Everything else that would reshape the outline is REFUSED (use "
                "insert_block, which numbers the new paragraph and keeps the following numbers consistent): "
                "a heading ABOVE the block's level, non-consecutive headings, or any call that also replaces "
                "or deletes lines -- the block itself then moves, so its sibling cannot be inferred. "
                "To change SEVERAL blocks in ONE call, pass edits=[{path, ops, expected_hash}, ...]: "
                "an entry that fails SYNTAX validation refuses the WHOLE call -- a batch is "
                "ALL-OR-NOTHING: nothing is written for ANY entry, so every expected_hash you sent "
                "still opens its block -- while an entry refused for any OTHER cause leaves the "
                "rest applied. Either way the reply says how many entries were applied, and N "
                "blocks cost one turn instead of N. "
                'THE ROUTE ABOVE HAS FURTHER CONDITIONS, and a call that breaks one is REFUSED with that condition named. (1) The title must be one the RESULT really gains, read as Markdown: a \'#\' inside a code fence is code and never a title, so an insertion whose point falls inside an open fence is written as code and creates nothing. (2) The insertion point must be the block\'s LAST line -- nothing but blank lines of the block may still follow it -- because a paragraph at this level is seated AFTER the block: when the block\'s own lines still follow the point, the place asked for cannot be respected and the refusal says so, naming the line (a heading sent at line 0 lands BEFORE the block, and that IS honoured where it was asked for). (3) No other operation of the call may write into the block: creating the section and rewriting the block are two writes, and a call that can half-apply is worse than a refusal, so a call that also writes into the block is refused. THE HOST\'S HASH IS RE-READ AFTER THE WRITE: host_content_hash is the one the block holds NOW, and host_block_unchanged is true only when it really did not move -- seating a paragraph after the LAST block of a file re-lays the end of the file, and then the hash HAS moved and the reply says so. AND THE ROUTE PASSES THE SAME GUARDS AS ANY OTHER EDIT, ON THE RESULT, BEFORE IT WRITES: what the call WOULD produce is checked first, so an inserted paragraph that leaves a code fence open is REFUSED exactly as it is inside a block (every heading after the fence would stop being Markdown, and the rest of the document would stop being sections), and a title a sibling already carries is refused by the same test -- no call reaches through edit_block a state the ordinary path refuses. Nothing is left behind by a refusal: the check runs on a DRY RUN of what the call would write, so no file byte AND no index pass. In a batch, every entry is expressed against the version you READ, and the tool translates the '
                'numbers for you. THE CALL LOCKS EVERY FILE IT TOUCHES, in the ALPHABETICAL order of their normalised '
                'absolute paths -- a fixed order is what forbids a deadlock between two calls that cross, [a, b] '
                'against [b, a] -- and holds each lock across the writes AND the index pass: a READER of the same '
                'file waits its turn instead of reading a version the writer is about to move, two DIFFERENT files '
                'never see each other, and a holder that dies is released by the system, the lock living on the OS '
                'and dying with its process. A holder that does not give way within the timeout is answered with a '
                'refusal that says NOTHING was written for ANY entry of the call, so every expected_hash you sent '
                'still opens its block. WHEN AN ENTRY WRITES AND MOVES LINES, the tool REBASES the entries that '
                'follow it: aim every entry at the version you READ, never at an intermediate state, and a rebased '
                'entry is APPLIED, not refused. What is followed is the IDENTITY of a line -- its number in the '
                'version you read -- and NOT the text you read: with a repeated line such as "- dup", the write lands '
                'on the copy you aimed at, not on the other one. THE REBASING REFUSES ONLY TWICE: (1) two entries of '
                'THIS call write the SAME line of the version you read -- a real conflict, and the refusal NAMES the '
                'line and the entry that wrote it first, the reply carrying rebased_by and conflict: true; (2) a '
                'writer OUTSIDE the tool moved the file between your read and this call (a person in an editor, a git '
                'checkout) -- the reply says Stale read and returns the FRESH block, numbered and stamped, to re-send '
                'against in this same call. AN ENTRY THAT WAS REBASED SAYS SO: expected_hash_rebased is true, '
                'rebased_by names the entry that wrote, and rebase carries your_lines (the numbers you wrote) and '
                'moved_by_entry; a warning names that entry, in ONE of TWO formulations. When the lines you aimed at '
                'MOVED: "entry N of THIS call wrote this block after you read it, so the lines you aimed at had '
                'MOVED. The tool rebased them: the operations you sent were applied to the lines you read, nothing '
                'was refused, and your 1-based numbers still designate them. The stamp you sent is NOT reusable -- '
                'this reply carries the current one." When another entry of the same call wrote the same FILE without '
                'moving the lines you aim at: "another entry of THIS call wrote the same FILE after you read it, so '
                'the stamp you sent was one version behind -- but every line this entry aims at still holds exactly '
                'what you read, so nothing had to be rebased. The operations were applied and nothing was refused. '
                'The stamp you sent is NOT reusable: this reply carries the current one." In BOTH cases the stamp you '
                'sent is NOT reusable: the reply carries the current one. A numbering notice names the OTHER sections that carry the number, and the block you edited too when its OWN title is carried elsewhere (a real duplicate is never silenced); where the new lines landed is read from your own operations (\'BEFORE its first line\' when they land above the block).'
            ),
        )
        async def edit_block(
            path: Optional[str] = Field(
                default=None,
                description=(
                    "Symbol id, paragraph id, or a document path (its root text). A code file or a "
                    "directory names no block: name one of the symbols read_block prints under it. "
                    "Omit it when you pass edits=[...]."
                ),
            ),
            ops: Optional[List[Dict[str, Any]]] = Field(
                default=None,
                description=(
                    "Line operations, 1-based, applied bottom-up: {\"replace\": [first, last], "
                    "\"content\": \"...\"}, {\"delete\": [first, last]}, or "
                    "{\"insert_after\": n, \"content\": \"...\"}. Omit it when you pass edits=[...]."
                ),
            ),
            expected_hash: Optional[str] = Field(
                default=None,
                description=(
                    "The version stamp of the block whose text you are editing, returned WITH that text "
                    "(read_block, a search that inlines the content, a Dossier entry that carries it). "
                    "Your numbers describe THAT version; a mismatch refuses the edit and returns the "
                    "fresh numbered text plus the call to send, instead of applying your numbers to "
                    "different lines. Optional, but without it nothing checks the numbers."
                ),
            ),
            edits: Optional[List[Dict[str, Any]]] = Field(
                default=None,
                description=(
                    "SEVERAL blocks in ONE call: a list of {path, ops, expected_hash} objects, applied "
                    "in the order given. A path given at the top level beside edits is USED when the "
                    "call holds ONE entry (the reply says so); with SEVERAL entries each one carries "
                    "ITS OWN path (or an expected_hash that uniquely identifies the block); otherwise "
                    "the refusal names the entry that has none. Every entry is "
                    "validated and answered exactly like a single call, in results, in the same "
                    "order; a batch is ALL-OR-NOTHING: an entry that fails SYNTAX validation "
                    "refuses the WHOLE call -- nothing is written for ANY entry, so every "
                    "expected_hash you sent still opens its block -- while an entry refused for any "
                    "OTHER cause leaves the rest applied. Either way the reply says how many entries "
                    "were applied. Use it to spend one turn on N blocks instead of N turns."
                ),
            ),
            format: Optional[str] = Field(
                default="text",
                description=(
                    "text (default) returns a compact confirmation with a targeted diff snippet and the new content_hash; "
                    "json returns the full structured dictionary with numbered_text."
                ),
            ),
        ) -> Any:
            # An EXTERNAL target (a NuGet contract) is refused before anything reaches the
            # patcher. In a batch it refuses the WHOLE call, like a syntax barrier: nothing is
            # written for any entry, so every expected_hash sent still opens its block.
            if path and not edits:
                external = self._external_entries(path)
                if external:
                    return self._external_refusal("edit_block", path, external)
            # A PATHLESS ENTRY THAT CARRIES ITS STAMP names its block anyway, when ONE live block
            # owns that hash (Patcher._unique_owner_of_hash: unique, re-hashed from the disk,
            # never from the index alone). Measured: 17 entries of 9 calls in the campaign
            # (every one carried an expected_hash), refused for want of a path and re-sent next
            # turn. An entry whose stamp names no block, or several, keeps its refusal below.
            # The top-level path of a ONE-entry call still wins (`inherits`, further down).
            inferred: Dict[int, str] = {}
            if edits and isinstance(edits, list):
                lone_with_path = len(edits) == 1 and bool(path)
                filled: List[Any] = []
                for index, entry in enumerate(edits):
                    if (
                        isinstance(entry, dict)
                        and not entry.get("path")
                        and not lone_with_path
                        and entry.get("expected_hash")
                    ):
                        owner = self.patcher._unique_owner_of_hash(entry.get("expected_hash"))
                        if owner:
                            entry = dict(entry)
                            entry["path"] = owner
                            inferred[index] = owner
                    filled.append(entry)
                edits = filled
            if edits:
                for index, entry in enumerate(edits):
                    target = entry.get("path") if isinstance(entry, dict) else None
                    target = target or (path if len(edits) == 1 else None)
                    external = self._external_entries(target) if target else []
                    if external:
                        refusal = self._external_refusal("edit_block", str(target), external)
                        refusal["error"] = (
                            "entry " + str(index) + " of edits: " + refusal["error"]
                            + " The WHOLE call was refused: no entry of it was applied, so every "
                            "expected_hash you sent still opens its block."
                        )
                        refusal["applied"] = 0
                        return refusal
            # edits WINS. Refusing the whole batch because a single-block field was also present
            # cost a turn and threw away three prepared edits (base f831a4e, t20). Ignoring a
            # field silently would be worse: the reply names what it ignored.
            ignored = [
                name for name, value in (("path", path), ("ops", ops), ("expected_hash", expected_hash))
                if value
            ]
            # ONE entry beside a top-level path is the shape the model reaches for -- nine times
            # across the campaign (5f04158 r0 t25, r3 t19 and t21; 15711e6 r1 t23; and FIVE times in
            # ONE run on 15711e6 r2, t9/t12/t15/t18/t20), one turn each. With a single entry the
            # intent is unambiguous, so the top-level path FILLS it instead of being ignored; with
            # several entries one path cannot name them all, and the refusal names the entry index
            # (backlog §9: the message cost a re-derivation turn before).
            inherits = (
                bool(edits)
                and len(edits) == 1
                and bool(path)
                and not (isinstance(edits[0], dict) and edits[0].get("path"))
            )
            if inherits:
                ignored = [name for name in ignored if name != "path"]
            if edits:
                # The batch goes to the PATCHER as ONE call, never one entry at a time HERE: a
                # batch is one intent, so a syntax barrier on any entry refuses them ALL (see
                # Patcher._edit_batch). Applying them one by one is exactly what left half a
                # batch on disk -- round006_ordre1 t15 answered "applied": 1, "refused": 6, and
                # the partial write killed the expected_hash of every refused entry, so t16
                # retried into five "Stale read" and the six only landed at t17.
                entries: List[Dict[str, Any]] = []
                pathless: List[int] = []
                for index, entry in enumerate(edits):
                    one = dict(entry) if isinstance(entry, dict) else {}
                    if not one.get("path") and inherits:
                        one["path"] = path
                    if not one.get("path"):
                        pathless.append(index)
                    entries.append(one)
                reply = self.patcher._edit_batch(entries)
                results: List[Dict[str, Any]] = list(reply["results"])
                applied = reply["applied"]
                refused = reply["refused"]
                for index in pathless:
                    # A missing path is a shape THIS surface knows and the patcher does not: the
                    # entry is answered here, with the index the caller counted and the reason
                    # the top-level path cannot fill it. The hint reads the `applied` count the
                    # batch really reached (it read "the others were applied" on a batch where
                    # applied was 0 -- three times in the campaign, and an agent quoted the
                    # contradiction with the warning in its own report).
                    results[index] = {
                        "status": "error",
                        "entry_index": index,
                        "block_id": f"entry {index}",
                        "error": (
                            "entry " + str(index) + " of edits has no path: nothing was written "
                            "for it."
                            + ("" if not path else " The top-level path cannot fill it: this call "
                               "carries SEVERAL entries, so each one needs its own path.")
                        ),
                        "actionable_hint": (
                            "Give entry " + str(index) + " its path: edits=[{path: '<the block>', "
                            "ops: [...], expected_hash: '...'}, ...]. Every entry is validated on "
                            "its own: " + (
                                str(applied) + " of the " + str(len(results)) + " entries of this "
                                "call were applied, and this one was not."
                                if applied else
                                "no entry of this call was applied, so NOTHING was written."
                            )
                        ),
                    }
                payload: Dict[str, Any] = {
                    "status": reply["status"],
                    "applied": applied,
                    "refused": refused,
                    "results": results,
                }
                if "error" in reply:
                    payload["error"] = reply["error"]
                if applied == 0 and refused > 0 and "error" not in payload:
                    refused_indices = [
                        i for i, r in enumerate(results)
                        if isinstance(r, dict) and r.get("status") != "success"
                    ]
                    refused_details = ", ".join(
                        f"entry {i} ({r.get('block_id') or r.get('id') or (entries[i].get('path') if i < len(entries) else '') or 'entry ' + str(i)})"
                        for i, r in enumerate(results)
                        if i in refused_indices and isinstance(r, dict)
                    )
                    payload["error"] = (
                        f"Batch refused: 0 of {len(results)} entries applied; "
                        f"refused: {refused_details}. Nothing was written."
                    )
                for key in ("batch_atomic", "atomicity"):
                    if key in reply:
                        payload[key] = reply[key]
                if ignored:
                    payload["warnings"] = [
                        "edits=[...] was given, so the top-level " + ", ".join(ignored) + " "
                        + ("was" if len(ignored) == 1 else "were") + " IGNORED: this call edits "
                        "through the entries of edits alone, each of which carries its own path "
                        "and ops. Send one form or the other."
                    ]
                if inherits:
                    payload["warnings"] = list(payload.get("warnings") or []) + [
                        "entry 0 of edits carried no path, so the top-level path '" + str(path)
                        + "' was USED for it: this call holds ONE entry. Send that path inside the "
                        "entry when the call carries several."
                    ]
                for index, owner in sorted(inferred.items()):
                    payload["warnings"] = list(payload.get("warnings") or []) + [
                        "entry " + str(index) + " of edits carried no path: its expected_hash belongs "
                        "to exactly ONE block, '" + owner + "', so the entry was addressed to it. Send the "
                        "path inside every entry: with a hash shared by several blocks, or one that "
                        "matches none, the entry is refused."
                    ]
                # A batch refused WHOLE carries its own repair sentence ("every entry still
                # matches the file, because nothing was written"), which the count below would
                # throw away: the caller would have to re-derive that the retry is safe.
                if reply.get("actionable_hint"):
                    payload["actionable_hint"] = reply["actionable_hint"]
                elif refused:
                    refused_indices = [
                        i for i, r in enumerate(results)
                        if isinstance(r, dict) and r.get("status") != "success"
                    ]
                    refused_indices_str = ", ".join(f"entry {i}" for i in refused_indices)
                    payload["actionable_hint"] = (
                        str(refused) + f" entry(ies) of edits ({refused_indices_str}) were refused and name their own cause "
                        "and next call in results, in the order sent; " + (
                            str(applied) + " of the " + str(len(results)) + " entries of this call "
                            "were applied."
                            if applied else
                            "NO entry of this call was applied, so nothing was written."
                        )
                    )
                if format == "json":
                    return payload
                if applied > 0:
                    return format_batch_edit_result(payload)
                return payload
            if not path or ops is None:
                # The refusal reads the ARGUMENTS: which keys arrived, which one the nearest form
                # is missing, which one is in excess (see edit_block_shape_refusal).
                refusal = edit_block_shape_refusal(path, ops, expected_hash, edits)
                return {
                    "status": "error",
                    "error": refusal["error"],
                    "actionable_hint": refusal["actionable_hint"],
                }
            reply = self.patcher.unify_addresses(
                self.patcher.edit_block(
                    block_id=path,
                    ops=ops,
                    expected_hash=expected_hash,
                ),
                used=[path],
            )
            if format == "json":
                return reply
            if isinstance(reply, dict) and reply.get("status") == "success":
                return format_single_edit_result(reply)
            return reply




        @self.mcp.tool(
            name="insert_block",
            description=(
                "Insert a new block as a CHILD of parent and return the state of the block you just "
                "made: its id, its numbered text and its hash, so the next call needs no read_block "
                "and no guess. This is "
                "the ONLY way to "
                "add a paragraph: edit_block never touches the outline. parent is the block that "
                "receives the child, written as a PATH like every other name here: a document path means "
                "the document itself, a paragraph path (guide.md::Section mere::Paragraphe) means that "
                "paragraph, a code path (T.cs::Classe::Methode) means that symbol, and a DIRECTORY path "
                "CREATES the child -- a file, or a directory when the first line of text ends in a slash, since "
                "the name carries the type, and nothing on this surface deletes one afterwards, so it is "
                "removed outside the tools). anchor is optional: the "
                "id of a child to insert right after it (and after everything that child holds: a "
                "container owns its whole subtree), or omitted to insert FIRST -- in a document that "
                "means ABOVE its current first section, and since the level comes from the parent a "
                "'##' written there is written as '#'. text's "
                "FIRST LINE is the title, the rest is the "
                "body. The level (parent+1) and the number are DERIVED: an existing number is kept, and "
                "the reply names any sibling it renumbered. A leading '#' is accepted and dropped; "
                "refused if the placement or the title would contradict the tree."
            ),
        )
        async def insert_block(
            parent: str = Field(
                ...,
                description=(
                    "Block that receives the child, as a path: a document, a paragraph "
                    "(guide.md::Section mere::Paragraphe), a code symbol (T.cs::Classe::Methode), or a "
                    "DIRECTORY, which CREATES the child inside it."
                ),
            ),
            anchor: Optional[str] = Field(
                default=None,
                description=(
                    "Id of a child of parent to insert right after it -- and after everything "
                    "that child holds, since a container owns its whole subtree. Omit it to insert "
                    "as the FIRST child."
                ),
            ),
            text: str = Field(
                ...,
                description=(
                    "The new block: first line = title (and, under a directory, the name of the file "
                    "or directory to create), rest = body."
                ),
            ),
        ) -> Dict[str, Any]:
            # La NATURE du parent decide de la syntaxe : sous un fichier de code (ou un symbole, car un
            # symbole est un bloc comme un autre), le texte est du C# et passe par la machinerie des
            # symboles ; sous un document ou un dossier, c est un bloc Markdown ou un fichier.
            external = self._external_entries(parent)
            if external:
                return self._external_refusal("insert_block", parent, external)
            head = parent.split("::", 1)[0].split("#", 1)[0]
            # A DIRECTORY is decided on the FILE SYSTEM first, never on the shape of its name:
            # Path('a/DataLinq.Framework.SparkQuery').suffix is '.SparkQuery', so the name test
            # sent the creation of a file inside that directory to the SYMBOL machinery, which
            # read the DIRECTORY as a source file and raised PermissionError [Errno 13] -- an
            # internal failure whose own text says "this is NOT a parameter problem, your
            # arguments were accepted", so the agent retried three times and worked around it
            # while nothing was ever created (run 2 of the base 9ef860f).
            is_document = head.lower().endswith((".md", ".markdown"))
            known = self.patcher._existing_path(head)
            is_folder = bool(parent.endswith("/") or (known is not None and known.is_dir()))
            if known is None and not is_folder:
                # A path that is not on disk is a code file only when a PARSER claims it: a
                # folder is what everything else designates, and it is the branch that can
                # still refuse cleanly.
                is_folder = not self._designates_a_code_file(head)
            if not is_document and not is_folder:
                return self.patcher.unify_addresses(
                    self.patcher.insert_symbol(parent_id=parent, code=text, anchor=anchor),
                    used=[parent, anchor],
                )
            return self.patcher.unify_addresses(
                self.patcher.insert_block(parent_id=parent, where=anchor, text=text),
                used=[parent, anchor],
            )

        @self.mcp.tool(
            name="delete_block",
            description=(
                "Remove ONE block: a code symbol (file.cs::Classe::Methode) or a Markdown paragraph "
                "(file.md::Section mere::Paragraphe). The block own nature decides which, "
                "so there is no separate tool per kind. Deleting a code symbol removes everything "
                "it holds; deleting a Markdown paragraph removes its heading and its own text, and "
                "the paragraphs under it move up to its parent -- the reply names them. It never "
                "removes a whole FILE or a DIRECTORY, only a block inside one."
            ),
        )
        async def delete_block(
            path: str = Field(..., description="Path of the block to remove."),
            expected_heading: Optional[str] = Field(
                default=None,
                description=(
                    "Optional guard for a Markdown paragraph: the heading you believe sits there. "
                    "Refuses the deletion when the path has shifted to another paragraph."
                ),
            ),
        ) -> Dict[str, Any]:
            external = self._external_entries(path)
            if external:
                return self._external_refusal("delete_block", path, external)
            return self.patcher.unify_addresses(
                self.patcher.delete_block(path=path, expected_heading=expected_heading),
                used=[path],
            )

        @self.mcp.tool(
            name="build_context",
            description=(
                "Build a relevance-ranked, token-budgeted project context. It scores documents, "
                "paragraphs and symbols against a query and returns the best ones at the right level "
                "of detail (0 name, 1 signature/title, 2 +docstring, 3 +content) to fit the budget. "
                "The context returns directly as compact text; format='json' adds the structured "
                "`entries` array. `map` is a LEVEL, not a number: name the level and the tool owns the "
                "budget behind it. The relevance floor is the tool's own configuration."
            ),
        )
        async def build_context(
            query: str = Field(..., description="What you are looking for: the task, a question, or keywords."),
            budget_tokens: int = Field(
                default=self.config.context_budget_tokens,
                description="Token budget for the returned context (server default is configurable).",
            ),
            max_level: int = Field(default=3, description="Maximum detail level (0-3)."),
            breadth_ratio: float = Field(
                default=0.4,
                description="Fraction of the budget spent listing many elements before deepening the best ones.",
            ),
            map_ratio: float = Field(
                default=0.0,
                description=(
                    "Fraction of the budget reserved for a project map (folders, sub-folders, "
                    "file names) placed at the top of the context; 0 disables it."
                ),
            ),
            map_tokens: Optional[int] = Field(
                default=None,
                description=(
                    "Project map budget in TOKENS, on top of `budget_tokens`: the map goes at "
                    "the top of the context and the sections keep the full budget. Use this "
                    "(not `map_ratio`) to compare an arm with the map against an arm without it."
                ),
            ),
            map_detail: Literal["names", "structure"] = Field(
                default="names",
                description=(
                    "What the map carries. 'names' lists folders and file names only. "
                    "'structure' adds the mini-AST -- the same ids, spans and sizes read_block "
                    "returns -- of the files the query ranks highest, within the map budget; "
                    "the tree itself stays complete. Use 'structure' so an arm given the map up "
                    "front is told the same facts an arm discovers by reading the files."
                ),
            ),
            map: Literal["none", "names", "structure"] = Field(
                default="none",
                description=(
                    "Attach a project map to the top of the context, as a LEVEL: 'none' (default), "
                    "'names' = the folder/file tree, 'structure' = the tree PLUS the mini-AST (ids, "
                    "spans, sizes) of the files the query ranks highest. A level, not a token count: "
                    "the map budget is the tool's business. Pick 'structure' when you need to know "
                    "which blocks exist before you read them."
                ),
            ),
            min_score: Optional[float] = Field(
                default=None,
                description=(
                    "OPERATOR calibration only (the harness passes it for the Initial Dossier): drop "
                    "entries scoring below this relevance value. Omitted, the floor is the one set in "
                    "context_config.json -- an agent never sets it, since a raw relevance score means "
                    "nothing to the caller."
                ),
            ),
            detail_level: Optional[int] = Field(
                default=None,
                ge=1,
                le=5,
                description=(
                    "Convenience size level for the returned context: 1 = ~4000 tokens (quick "
                    "look), 3 = ~16000, 5 = ~32000 (deep). Overrides budget_tokens when set."
                ),
            ),
            format: Literal["text", "json"] = Field(
                default="text",
                description=(
                    "'text' returns a compact labelled block (recommended: the same content costs "
                    "~29% more tokens as JSON); 'json' adds the structured `entries` array."
                ),
            ),
            max_entries_per_file: Optional[int] = Field(
                default=None,
                description=(
                    "Maximum entries allowed per source file or document (prevents a single "
                    "document from monopolizing the context budget)."
                ),
            ),
            max_content_chars: Optional[int] = Field(
                default=None,
                description=(
                    "Maximum characters per content entry at level 3 before truncation "
                    "(default 2000; higher for large token budgets to preserve complete classes and hashes)."
                ),
            ),
        ) -> Any:
            resolved_budget = budget_tokens
            if detail_level is not None:
                resolved_budget = self.context_builder.DETAIL_LEVEL_BUDGETS[int(detail_level)]
            # The floor is the configuration's (context_config.json), never the agent's. An explicit
            # number is an OPERATOR override (the harness's Initial Dossier passes one).
            resolved_min_score = min_score
            if resolved_min_score is None:
                resolved_min_score = self.context_builder.configured_floor()
            resolved_map_tokens = map_tokens
            resolved_map_detail = map_detail
            if map != "none":
                if resolved_map_tokens is None:
                    resolved_map_tokens = self.context_builder.MAP_LEVEL_TOKENS[map]
                if resolved_map_detail == "names":
                    resolved_map_detail = map
            payload = self.context_builder.build(
                query=query,
                budget_tokens=resolved_budget,
                max_level=max_level,
                breadth_ratio=breadth_ratio,
                map_ratio=map_ratio,
                map_tokens=resolved_map_tokens,
                map_detail=resolved_map_detail,
                min_score=resolved_min_score,
                max_entries_per_file=max_entries_per_file,
                max_content_chars=max_content_chars,
            )
            if format == "json":
                payload["context"] = payload["text"]
                return payload
            return payload["text"]

        @self.mcp.tool(
            name="get_project_map",
            description="Return a compact, budgeted map of the whole project (documents + top-level symbols). Call this first to orient yourself; it always reflects the live index, including edits made through the tools.",
        )
        async def get_project_map(
            budget_tokens: int = Field(
                default=8000,
                description="Approximate token budget for the map. Increase it (e.g. 16000) if the map is truncated.",
            ),
            path: Optional[str] = Field(default=None, description="Optional glob filter."),
            format: Literal["text", "json"] = Field(
                default="text",
                description=(
                    "text (default) returns a compact block: ~40% fewer tokens than the JSON envelope, "
                    "which repeats derivable fields. json returns the structured payload."
                ),
            ),
        ) -> Any:
            payload = self.db.get_project_map(
                budget_tokens=budget_tokens, file_pattern=path, answer_format=format
            )
            if format == "json":
                return payload
            return render_project_map(payload)



        @self.mcp.tool(
            name="search_documents",
            description=(
                "Find the right document paragraph. The query is matched term by term, so a wording "
                "that no paragraph contains verbatim still returns its best partial evidence; the best "
                "matches come back with their full text, the rest as an excerpt."
            ),
        )
        async def search_documents(
            query: str = Field(default="", description="Search text (an empty query returns every paragraph)."),
            path: Optional[str] = Field(default=None, description="Optional glob filter."),
            limit: int = Field(default=8, description="Maximum number of matches."),
            full_top: int = Field(
                default=2,
                description=(
                    "How many of the best matches come back with their whole text inlined (0 = none). "
                    "Read those directly; no follow-up read_block call is needed for them."
                ),
            ),
            include_content: bool = Field(
                default=False, description="Set true to inline the full text of every match instead of an excerpt."
            ),
            format: Literal["text", "json"] = Field(
                default="text",
                description=(
                    "text (default) returns one compact line per item: ~50% fewer tokens than the JSON "
                    "envelope, which repeats derivable fields. json returns the structured array."
                ),
            ),
        ) -> Any:
            if not query:
                # Sans requete, c est la LISTE COMPLETE des documents : search_documents absorbe
                # list_documents. Une requete vide ne rend plus tous les paragraphes -- mesure :
                # 289 documents = 33 057 caracteres, jamais tronques.
                documents = self.db.list_documents(file_pattern=path, limit=limit)
                listing = render_documents(documents)
                if not documents:
                    listing = empty_document_list_note(self.db, path, self._disk_matches(path))
                if format == "json":
                    return {
                        "status": "success",
                        "count": len(documents),
                        "documents": documents,
                        "text": listing,
                    }
                return listing

            matches = self.db.search_documents(
                query=query,
                file_pattern=path,
                limit=limit,
                include_content=include_content,
                full_top=full_top,
                # An inlined match longer than this is cut and flagged, so the
                # caller knows to read that exact id in full if it matters.
                max_content_chars=4000,
            )
            for match in matches:
                match["referenced_files"] = self._referenced_files(
                    match.get("content") or match.get("snippet")
                )
                # The stored id of a paragraph is a slug path; what the surface PRINTS is the
                # heading chain, with a rank when two sibling headings share a title.
                stored_id = str(match.get("section_id") or "")
                displayed = self.patcher.displayed_section_id(stored_id)
                match["displayed_id"] = displayed
                match["section_id"] = displayed
                if match.get("content") and not match.get("content_truncated"):
                    metrics = self.patcher.block_metrics(displayed)
                    if not metrics and stored_id:
                        metrics = self.patcher.block_metrics(stored_id)
                    if metrics:
                        match["content_hash"] = metrics["content_hash"]
                        match["line_count"] = metrics["line_count"]
                        match["numbered_text"] = metrics["numbered_text"]
                        match["kind"] = metrics.get("kind", "section")
                    else:
                        from .lineops import compute_content_hash, numbered
                        c = match["content"]
                        match["content_hash"] = compute_content_hash(c.encode("utf-8"))
                        match["line_count"] = len(c.splitlines())
                        match["numbered_text"] = numbered(c, 1)
                        match["kind"] = "section"
            # Same doctrine as search_symbols, and for the same reason: the note is
            # built before the format branch, so the JSON form of an empty search is
            # not a bare {"matches": []} with nothing to act on.
            note = ""
            if not matches:
                # An empty answer must say what was checked, never nothing at all.
                note = empty_document_search_note(
                    self.db, query, path, self._disk_matches(path)
                )
            if format == "json":
                payload: Dict[str, Any] = {"status": "success", "count": len(matches), "matches": matches}
                if note:
                    payload["note"] = note
                    payload["text"] = note
                return payload
            text = render_document_matches(matches)
            if note:
                text = note
            return text







    async def run_stdio(self) -> None:
        """Run MCP server over stdio transport."""
        await self.mcp.run_stdio_async()

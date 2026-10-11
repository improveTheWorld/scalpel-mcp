"""Workspace indexer for scanning, parsing, and populating the symbol and document graph."""

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import os

import logging

from .config import ServerConfig
from .database import DatabaseManager
from .documents import DocumentRegistry, stable_section_ids
from .externals import ExternalIndex, external_symbols_enabled
from .parsers import ParserRegistry, compute_content_hash

logger = logging.getLogger("ast_graph_server.indexer")


def _is_reparse_point(path: Path) -> bool:
    """True for a symlink OR an NTFS junction.

    os.path.islink() is False for a junction, and os.walk follows junctions whether or
    not followlinks is set -- so a junction was the one way a directory outside the
    workspace, or a cycle back to the root, reached the index. Anything undecidable is
    treated as one: refusing to index a file is cheap, indexing the wrong workspace is not.
    """
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction is not None and isjunction(path))
    except OSError:
        return True


class Indexer:
    """Recursively scans workspace source and document files and builds the graph.

    File hashes are persisted in the database (table indexed_files), so indexing
    is paid once: a server restart only re-parses files that actually changed.
    """

    def __init__(
        self,
        config: ServerConfig,
        db: DatabaseManager,
        registry: Optional[ParserRegistry] = None,
        document_registry: Optional[DocumentRegistry] = None,
        embedder: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.registry = registry or ParserRegistry()
        self.document_registry = document_registry or DocumentRegistry()
        self.embedder = embedder
        self._file_hashes: Dict[str, str] = {}
        self._hashes_loaded = False
        # External (NuGet) contracts: refreshed at the end of every workspace pass from the
        # .csproj files the walk met. Never allowed to break indexing (see index_workspace).
        self.externals: Optional[ExternalIndex] = None
        if external_symbols_enabled():
            try:
                self.externals = ExternalIndex(db)
            except Exception as exc:  # a read-only or foreign database must not stop the server
                logger.warning("External symbol index unavailable: %s", exc)
                self.externals = None

    def _ensure_hashes_loaded(self) -> None:
        """Load the persistent hash table once, so restarts skip unchanged files."""
        if self._hashes_loaded:
            return
        try:
            self._file_hashes.update(self.db.get_file_hashes())
        except Exception:
            pass
        self._hashes_loaded = True

    def should_ignore(self, path: Path) -> bool:
        """Check if path matches configured ignore patterns."""
        return self.should_ignore_relative(self.config.to_relative_posix(path))

    def ignore_pattern_for(self, rel_posix: str) -> Optional[str]:
        """The first ignore pattern that matches, or None.

        Returning the PATTERN (not the name) is what lets a listing explain an empty
        directory without publishing the names of the files it hides.
        """
        patterns = self.config.ignore_patterns
        if not patterns:
            return None
        compiled = self._compiled_ignore_patterns()
        # normcase ONCE per path, not once per fnmatch call: on Windows fnmatch's
        # normcase is an LCMapStringEx syscall, and a 10 000-entry listing spent
        # 1 120 000 of them (112 per entry) -- 5.6 s of pure case folding.
        # Split FIRST, normalise second: ntpath.normcase turns "/" into "\\", so
        # splitting the normalised string would yield one part and match nothing.
        raw = rel_posix.replace("\\", "/")
        rel = os.path.normcase(raw)
        parts = [os.path.normcase(part) for part in raw.split("/")]
        for index, matcher in enumerate(compiled):
            if matcher.match(rel):
                return patterns[index]
            for part in parts:
                if matcher.match(part):
                    return patterns[index]
        return None

    def _compiled_ignore_patterns(self) -> List[Any]:
        """fnmatch.translate() compiled, cached against the pattern list itself."""
        patterns = tuple(self.config.ignore_patterns)
        cached = getattr(self, "_ignore_cache", None)
        if cached is not None and cached[0] == patterns:
            return cached[1]
        compiled = [re.compile(fnmatch.translate(pattern)) for pattern in patterns]
        self._ignore_cache = (patterns, compiled)
        return compiled

    def should_ignore_relative(self, rel_posix: str) -> bool:
        """The same test on a path the caller ALREADY made relative.

        to_relative_posix resolves the path, so calling should_ignore once per child cost
        a full path resolution each time: ~1.1 ms per entry, 11 s for a 10 000-file
        directory. Nothing about the ignore decision needs the file system.
        """
        return self.ignore_pattern_for(rel_posix) is not None

    def _is_supported(self, rel_posix: str) -> bool:
        if self.registry.get_parser_for_file(rel_posix):
            return True
        return self.document_registry.get_parser_for_file(rel_posix) is not None

    def _forget_documents(self) -> int:
        """Drop the persisted hashes of document files, so the next pass re-parses them.

        Forgetting the hash is enough and it is deliberate: re-parsing a document
        replaces every one of its sections, its search entries and its embeddings,
        while source files keep their hashes and are not touched at all.
        """
        forgotten = 0
        for rel_posix in list(self._file_hashes):
            if self.document_registry.get_parser_for_file(rel_posix) is None:
                continue
            self.db.delete_file_hash(rel_posix)
            self._file_hashes.pop(rel_posix, None)
            forgotten += 1
        return forgotten

    #: Bumped whenever the *content* of the index changes meaning (a new document
    #: type, sections added to the search index, a different parser mapping...).
    #: The hash-only incremental pass cannot detect that: it would skip unchanged
    #: files and silently keep a partial index -- which produced Dossiers built on
    #: 11 files instead of 284. A mismatch forces a full re-parse.
    INDEX_SCHEMA_VERSION = 4

    #: Same idea, restricted to documents. A section identifier comes from the
    #: heading, so a change to that scheme renames every section of every Markdown
    #: file while no file changes and no symbol moves. Bumping the full version
    #: would re-parse the whole workspace (887 files, ~44 s) for a document-only
    #: change; this one drops the hashes of the documents alone, and a document
    #: re-parse replaces all of its sections, its search entries and its embeddings.
    #: Version 2 removed the paragraph number from the identifier. Version 4 gave a
    #: duplicated heading the path of its parents, so that deleting one paragraph can
    #: no longer re-point a sibling's identifier at a paragraph of another parent.
    DOCUMENT_SCHEMA_VERSION = 5

    def index_workspace(self, force: bool = False) -> int:
        """Scan and index supported files. Returns the number of files re-parsed.

        With force=True the persisted hashes are dropped so every file is parsed
        again. A persisted index built under a different INDEX_SCHEMA_VERSION
        implies force, so a schema change can never leave a stale partial index.
        A different DOCUMENT_SCHEMA_VERSION is narrower: it drops the hashes of the
        documents only, because their identifiers can change while no file does.
        """
        self._ensure_hashes_loaded()
        stored_document_version = self.db.get_meta("document_schema_version")
        if not force and stored_document_version != str(self.DOCUMENT_SCHEMA_VERSION):
            forgotten = self._forget_documents()
            logger.info(
                "Document schema version %s != %s: re-parsing %d document(s).",
                stored_document_version,
                self.DOCUMENT_SCHEMA_VERSION,
                forgotten,
            )
        stored_version = self.db.get_meta("index_schema_version")
        if not force and stored_version != str(self.INDEX_SCHEMA_VERSION):
            logger.info(
                "Index schema version %s != %s: forcing a full re-index.",
                stored_version,
                self.INDEX_SCHEMA_VERSION,
            )
            force = True
        if force:
            self.db.clear_indexed_files()
            self.db.clear_search_index()
            self._file_hashes.clear()

        indexed_count = 0
        workspace = self.config.workspace_root
        root_real = workspace.resolve()
        seen: set = set()
        # A real directory is entered ONCE: a link back to the root would otherwise be
        # walked without end (measured: 60 levels inside the first 60 yields).
        walked: set = {root_real}
        pending_embeddings: List[tuple] = []
        # .csproj files met by the walk (they are not parsed as code): their PackageReference
        # items name the NuGet packages whose contracts are indexed as external symbols.
        projects: List[Path] = []

        for root, dirs, files in os.walk(workspace):
            # A reparse point (symlink or NTFS junction) is not a directory of THIS
            # workspace: os.walk follows a junction even with followlinks=False, so one
            # pointing outside put outside files in the index, and one pointing back at
            # the root made the walk unbounded. Both are refused on the RESOLVED path.
            kept: List[str] = []
            for name in dirs:
                candidate = Path(root) / name
                if _is_reparse_point(candidate):
                    continue
                try:
                    real = candidate.resolve()
                    rel_posix = real.relative_to(root_real).as_posix()
                except (OSError, ValueError):
                    continue
                # The resolved path serves BOTH decisions: containment and the ignore
                # list. should_ignore() resolved it a second time per entry, which cost
                # 230 s on a 100 000-file workspace.
                if self.should_ignore_relative(rel_posix):
                    continue
                if real in walked:
                    continue
                walked.add(real)
                kept.append(name)
            dirs[:] = kept

            for file in files:
                file_path = Path(root) / file
                if _is_reparse_point(file_path):
                    continue
                try:
                    resolved = file_path.resolve()
                    rel_posix = resolved.relative_to(root_real).as_posix()
                except (OSError, ValueError):
                    continue
                if self.should_ignore_relative(rel_posix):
                    continue
                if file.lower().endswith(".csproj"):
                    projects.append(resolved)
                if not self._is_supported(rel_posix):
                    continue

                seen.add(rel_posix)
                if self.index_file(resolved, _pending_embeddings=pending_embeddings):
                    indexed_count += 1

        # Embeddings are computed in global batches after the scan, not per file.
        if self.embedder is not None and pending_embeddings:
            stored = self._embed_many(pending_embeddings)
            logger.info("Stored %d embeddings for %d inputs", stored, len(pending_embeddings))

        # Drop index entries the current scan no longer covers, in two cases: the
        # file is gone from disk, OR it now matches the ignore list. The second one
        # is what makes an ignore pattern RETROACTIVE: without it, adding a pattern
        # (a build directory, a secret filename) left the already-indexed file in
        # the index -- and therefore in the Dossier -- until a full --reindex.
        for stale in [path for path in self._file_hashes if path not in seen]:
            try:
                absolute = self.config.to_absolute_path(stale)
            except ValueError:
                absolute = None
            if absolute is None or not absolute.exists() or self.should_ignore(absolute):
                self.remove_file(absolute if absolute is not None else stale)

        self.refresh_externals(projects, force=force)

        self.db.set_meta("index_schema_version", str(self.INDEX_SCHEMA_VERSION))
        self.db.set_meta("document_schema_version", str(self.DOCUMENT_SCHEMA_VERSION))
        return indexed_count

    def refresh_externals(self, projects: List[Path], force: bool = False) -> Optional[Dict[str, Any]]:
        """Re-read the NuGet contracts the given projects reference (cached per package+version).

        A failure here is logged and swallowed: the external contracts are a complement to the
        workspace index, and they must never be the reason a workspace is not indexed.
        """
        if self.externals is None:
            return None
        try:
            if force:
                self.externals.clear()
            return self.externals.refresh(projects, self.config.workspace_root)
        except Exception as exc:
            logger.warning("External symbol refresh failed: %s", exc)
            return None


    def index_file(self, file_path: Path | str, _pending_embeddings: Optional[List[tuple]] = None) -> bool:
        """Index or re-index a single source or document file.

        Returns True only when the file was actually (re-)parsed, i.e. its hash
        changed since the last run.
        """
        self._ensure_hashes_loaded()
        abs_path = Path(file_path).resolve()
        try:
            abs_path.relative_to(self.config.workspace_root.resolve())
        except ValueError:
            # Never store a path that left the workspace: to_relative_posix falls back to
            # the ABSOLUTE path when resolve() leaves the root, so the index then served
            # content from outside it (measured: an NTFS junction inside the workspace).
            return False
        if not abs_path.exists() or not abs_path.is_file():
            rel_posix = self.config.to_relative_posix(abs_path)
            self.db.delete_file_symbols(rel_posix)
            self.db.delete_document(rel_posix)
            self.db.delete_file_hash(rel_posix)
            self.db.reset_search_index(rel_posix)
            self.db.delete_embeddings_for_file(rel_posix)
            self._file_hashes.pop(rel_posix, None)
            return False

        rel_posix = self.config.to_relative_posix(abs_path)
        code_parser = self.registry.get_parser_for_file(rel_posix)
        doc_parser = self.document_registry.get_parser_for_file(rel_posix)
        if not code_parser and not doc_parser:
            return False

        try:
            content = abs_path.read_bytes()
        except OSError:
            return False

        content_hash = compute_content_hash(content)
        # Persisted + in-memory hash: skip if identical content was already parsed
        if self._file_hashes.get(rel_posix) == content_hash:
            return False

        if code_parser:
            result = code_parser.parse_file(rel_posix, content)
            if result.symbols and len({s.id for s in result.symbols}) < len(result.symbols):
                if hasattr(code_parser, "_disambiguate_overloads"):
                    code_parser._disambiguate_overloads(result.symbols, result.calls)
            self.db.upsert_file_symbols(rel_posix, result.symbols, result.calls)
            entries = self._entries_for_symbols(rel_posix, result.symbols)
            self.db.reset_search_index(rel_posix)
            self.db.add_search_entries(entries)
            self._queue_embeddings(rel_posix, entries, _pending_embeddings)
        else:
            if doc_parser is None or len(content) > self.config.max_document_bytes:
                return False
            document = doc_parser.parse(rel_posix, content)
            self.db.upsert_document(rel_posix, document, content_hash)
            entries = self._entries_for_document(rel_posix, document)
            self.db.reset_search_index(rel_posix)
            self.db.add_search_entries(entries)
            self._queue_embeddings(rel_posix, entries, _pending_embeddings)

        self.db.set_file_hash(rel_posix, content_hash)
        self._file_hashes[rel_posix] = content_hash
        return True

    @staticmethod
    def _embed_inputs(entries: List[Dict[str, str]]) -> List[tuple]:
        """(ref_id, text) pairs to embed: title + a bounded slice of the body."""
        return [
            (entry["ref_id"], (entry.get("title", "") + "\n" + entry.get("body", ""))[:512])
            for entry in entries
        ]

    def _queue_embeddings(
        self,
        rel_posix: str,
        entries: List[Dict[str, str]],
        pending: Optional[List[tuple]],
    ) -> None:
        """Either defer embeddings to a global batch, or embed this file now.

        This is the optional vector pass: it is skipped entirely when no embedder
        is configured, so indexing stays BM25-only by default.
        """
        if self.embedder is None or not entries:
            return
        self.db.delete_embeddings_for_file(rel_posix)
        if pending is not None:
            pending.extend(self._embed_inputs(entries))
        else:
            self._embed_many(self._embed_inputs(entries))

    def _embed_many(self, items: List[tuple]) -> int:
        """Embed (ref_id, text) pairs in batches, storing each batch immediately.

        Incremental storing keeps memory bounded and makes progress visible; a
        failing batch does not discard the others.
        """
        if self.embedder is None or not items:
            return 0
        embed_batch = getattr(self.embedder, "embed_batch", None)
        batch_size = 128
        stored = 0
        for start in range(0, len(items), batch_size):
            chunk = items[start : start + batch_size]
            try:
                if callable(embed_batch):
                    vectors = embed_batch([text for _ref, text in chunk])
                else:
                    vectors = [self.embedder(text) for _ref, text in chunk]
            except Exception as exc:
                logger.warning("Embedding batch %d failed: %s", start // batch_size, exc)
                continue
            pairs = [(ref, list(vector)) for (ref, _text), vector in zip(chunk, vectors) if vector]
            if pairs:
                self.db.store_embeddings(pairs)
                stored += len(pairs)
            logger.info("Embedded %d/%d", min(start + batch_size, len(items)), len(items))
        return stored

    @staticmethod
    def _entries_for_symbols(rel_posix: str, symbols) -> List[Dict[str, str]]:
        """Search entries for code symbols: name as title, kind/signature/docstring as body."""
        entries: List[Dict[str, str]] = []
        for symbol in symbols:
            body = symbol.kind + " " + symbol.signature
            if symbol.docstring:
                body += "\n" + symbol.docstring
            entries.append(
                {
                    "kind": "symbol",
                    "ref_id": symbol.id,
                    "file_path": rel_posix,
                    "title": symbol.name,
                    "body": body,
                }
            )
        return entries

    @staticmethod
    def _entries_for_document(rel_posix: str, document) -> List[Dict[str, str]]:
        """Search entries for a document and each of its paragraphs."""
        entries: List[Dict[str, str]] = [
            {
                "kind": "document",
                "ref_id": rel_posix,
                "file_path": rel_posix,
                "title": document.title or rel_posix,
                "body": document.title or rel_posix,
            }
        ]
        section_ids = stable_section_ids(rel_posix, document.sections)
        for section, section_id in zip(document.sections, section_ids):
            entries.append(
                {
                    "kind": "section",
                    "ref_id": section_id,
                    "file_path": rel_posix,
                    "title": section.heading,
                    "body": section.content[:4000],
                }
            )
        return entries

    def remove_file(self, file_path: Path | str) -> None:
        """Remove symbols, document rows and the persisted hash of a deleted file."""
        rel_posix = self.config.to_relative_posix(file_path)
        self.db.delete_file_symbols(rel_posix)
        self.db.delete_document(rel_posix)
        self.db.delete_file_hash(rel_posix)
        self.db.reset_search_index(rel_posix)
        self.db.delete_embeddings_for_file(rel_posix)
        self._file_hashes.pop(rel_posix, None)

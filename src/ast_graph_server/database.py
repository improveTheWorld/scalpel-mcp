"""SQLite database manager for symbol code-graph."""

import array
import difflib
import fnmatch
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .documents import ParsedDocument, stable_section_ids
from .parsers.base import CallInfo, SymbolInfo, compute_content_hash


_TERM_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.+]*")


def path_matches(file_path: str, file_pattern: Optional[str]) -> bool:
    """Does a file match the pattern the caller passed as `file_pattern`?

    Callers pass whatever they hold: a glob ("src/**/*.cs"), a path, or just the
    file name they saw in a document ("SparkContext.cs"). Matching the full path
    alone made a bare name match NOTHING, silently: the agent then concluded the
    file carried no such symbol and searched again by another route (measured on
    mapab2 b r1, and again on b of the base 7c40a9e, which reported it).
    """
    if hasattr(file_pattern, "default"):
        file_pattern = file_pattern.default if str(file_pattern.default) != "PydanticUndefined" else None
    if not file_pattern:
        return True
    if fnmatch.fnmatch(file_path, file_pattern):
        return True
    if fnmatch.fnmatch(Path(file_path).name, file_pattern):
        return True
    normalized = file_path.replace("\\", "/")
    if normalized.endswith(file_pattern):
        return True
    # A pattern that names a FOLDER scopes to everything under it: three searches
    # returned nothing at all because the caller passed a directory where a glob was
    # expected ("DataLinq.Enterprise/spark"), each costing a manual widening
    # (declared by the agent on mapab2 a r1 of the base c980d51).
    return normalized.startswith(file_pattern.rstrip("/") + "/")


def query_terms(query: str, limit: int = 12) -> List[str]:
    """Split a search query into the terms the document search matches on.

    Matching the whole query as a single LIKE pattern made a multi-word question
    return nothing at all ("OAuth machine-to-machine M2M U2M service principal"
    matched no paragraph), and an empty answer pushes the caller into rephrasing
    the same concept one turn at a time. Matching the terms and ranking by how
    many of them a paragraph carries returns a partial answer instead of a blank.
    """
    terms: List[str] = []
    for raw in _TERM_RE.findall(query or ""):
        term = raw.strip("._+").lower()
        if len(term) < 2 or term in terms:
            continue
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


class DatabaseManager:
    """Manages SQLite storage, query execution, and concurrent transactions."""

    def __init__(self, db_path: Path | str, workspace_root: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path)
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The root the rows are supposed to describe. Containment at INDEX time is not
        # enough: a row written by another tool, or left behind by a pre-fix run, was
        # still served in full by the search, the listing and the project map. The read
        # path filters stored rows too, so containing the indexer cannot be undone by
        # whoever wrote the database.
        self._root = Path(workspace_root).resolve() if workspace_root is not None else None
        self._inside_workspace_cache: Dict[str, bool] = {}
        self._virtual_paths: set[str] = set()
        self._init_db()

    def _inside_workspace(self, file_path: Any) -> bool:
        """False for a stored path that resolves outside the workspace root."""
        if self._root is None:
            return True
        key = str(file_path)
        cached = self._inside_workspace_cache.get(key)
        if cached is not None:
            return cached
        norm_key = key.replace("\\", "/")
        if norm_key == ".." or norm_key.startswith("../") or "/../" in f"/{norm_key}/":
            ans = False
        else:
            try:
                resolved = (self._root / key).resolve()
                resolved.relative_to(self._root)
                if key in self._virtual_paths or norm_key in self._virtual_paths:
                    ans = True
                else:
                    ans = resolved.exists()
            except (OSError, ValueError):
                ans = False
        self._inside_workspace_cache[key] = ans
        return ans

    def get_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with WAL and a busy timeout configured.

        ORDER MATTERS, and it used to be wrong. `PRAGMA journal_mode = WAL` CONVERTS the file,
        and on a base that is not in WAL yet -- absent, just created, just recreated -- that
        conversion takes the WRITE LOCK. The busy timeout was set AFTER it, so the conversion ran
        with no retry while another instance sharing the same `.agent_graph.db` was creating the
        tables: that instance failed with `database is locked`. Once converted the pragma is a
        no-op, which is exactly why the SAME call failed seven times and then succeeded --
        reproduced, traced and explained by the adversarial pass (4 threads starting on a
        workspace without a base: one to three of them failed).

        So: the timeout comes FIRST (and it is RAISED -- 5000 ms was silently lowering the 10 s
        the driver already allowed), and a conversion that cannot take the lock is not fatal:
        the base stays usable in its current mode and the next connection converts it.
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000;")
        for attempt in range(3):
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                break
            except sqlite3.OperationalError:
                if attempt == 2:
                    break
                time.sleep(0.05 * (attempt + 1))
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        """Initialize relational database schema and indexes."""
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS symbols (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    docstring TEXT,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS symbol_calls (
                    caller_id TEXT NOT NULL,
                    callee_name TEXT NOT NULL,
                    call_expr TEXT NOT NULL,
                    callee_symbol_id TEXT,
                    FOREIGN KEY(caller_id) REFERENCES symbols(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS staged_changes (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    symbol_id TEXT,
                    action TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    title TEXT,
                    section_count INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS document_sections (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    heading TEXT NOT NULL,
                    level INTEGER NOT NULL DEFAULT 1,
                    order_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS indexed_files (
                    file_path TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Small persisted settings. `index_schema_version` is what lets a
                -- schema change force a full re-parse instead of silently keeping
                -- an index whose *content* no longer matches what should be indexed.
                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- Search index used ONLY for relevance scoring (BM25).
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                    kind UNINDEXED,
                    ref_id UNINDEXED,
                    file_path UNINDEXED,
                    title,
                    body,
                    tokenize = 'unicode61'
                );

                -- Optional embedding store for hybrid (vector) relevance.
                CREATE TABLE IF NOT EXISTS embeddings (
                    ref_id TEXT PRIMARY KEY,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL
                );

                -- Virtual table for document sections full-text search (BM25)
                CREATE VIRTUAL TABLE IF NOT EXISTS document_sections_fts USING fts5(
                    section_id UNINDEXED,
                    document_id UNINDEXED,
                    heading,
                    content,
                    tokenize = 'unicode61'
                );

                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
                CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(file_path);
                CREATE INDEX IF NOT EXISTS idx_sections_document ON document_sections(document_id);
                CREATE INDEX IF NOT EXISTS idx_sections_heading ON document_sections(heading);
                CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
                CREATE INDEX IF NOT EXISTS idx_calls_caller ON symbol_calls(caller_id);
                CREATE INDEX IF NOT EXISTS idx_calls_callee ON symbol_calls(callee_name, callee_symbol_id);
                CREATE INDEX IF NOT EXISTS idx_staged_file ON staged_changes(file_path);
                """
            )
            sec_count = conn.execute("SELECT COUNT(*) FROM document_sections").fetchone()[0]
            if sec_count > 0:
                fts_count = conn.execute("SELECT COUNT(*) FROM document_sections_fts").fetchone()[0]
                if fts_count == 0:
                    conn.execute(
                        """
                        INSERT INTO document_sections_fts (section_id, document_id, heading, content)
                        SELECT id, document_id, heading, content FROM document_sections
                        """
                    )

    def upsert_file_symbols(self, file_path: str, symbols: List[SymbolInfo], calls: List[CallInfo]) -> None:
        """Replace all symbols and calls for a given file atomically."""
        self._virtual_paths.add(str(file_path))
        self._inside_workspace_cache.pop(str(file_path), None)
        with self.get_connection() as conn:
            with conn:
                # Delete existing symbols (cascades to calls)
                conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))

                # Insert new symbols with uniqueness safety net
                seen_ids: set = set()
                for sym in symbols:
                    sym_id = sym.id
                    if sym_id in seen_ids:
                        ordinal = 2
                        candidate = f"{sym_id}-{ordinal}"
                        while candidate in seen_ids:
                            ordinal += 1
                            candidate = f"{sym_id}-{ordinal}"
                        sym_id = candidate
                        sym.id = sym_id
                    seen_ids.add(sym_id)
                    conn.execute(
                        """
                        INSERT INTO symbols (
                            id, file_path, parent_id, name, kind, signature,
                            docstring, start_byte, end_byte, start_line, end_line, content_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sym_id,
                            sym.file_path,
                            sym.parent_id,
                            sym.name,
                            sym.kind,
                            sym.signature,
                            sym.docstring,
                            sym.start_byte,
                            sym.end_byte,
                            sym.start_line,
                            sym.end_line,
                            sym.content_hash,
                        ),
                    )

                # Insert calls with candidate symbol resolution
                for c in calls:
                    # Look up potential matching symbol in workspace
                    callee_symbol_id = c.callee_symbol_id
                    if not callee_symbol_id:
                        # Extract the bare method/function name from callee_name
                        bare_name = c.callee_name.split(".")[-1]
                        row = conn.execute(
                            "SELECT id FROM symbols WHERE name = ? LIMIT 1",
                            (bare_name,),
                        ).fetchone()
                        if row:
                            callee_symbol_id = row["id"]

                    conn.execute(
                        """
                        INSERT INTO symbol_calls (caller_id, callee_name, call_expr, callee_symbol_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (c.caller_id, c.callee_name, c.call_expr, callee_symbol_id),
                    )

    def delete_file_symbols(self, file_path: str) -> None:
        """Remove all symbols for a deleted file."""
        self._virtual_paths.discard(str(file_path))
        self._inside_workspace_cache.pop(str(file_path), None)
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))

    # ------------------------------------------------------------------
    # Persistent index state (survives a server restart)
    # ------------------------------------------------------------------
    def find_blocks_by_hash(self, content_hash: str, limit: int = 8) -> List[Dict[str, str]]:
        """Every block whose CURRENT text hashes to `content_hash`, symbols first.

        A hash is a stamp on ONE block, but nothing in the caller's request says which: the Dossier
        attaches a hash to every entry it prints, so a stamp carried from another file is a
        predictable mistake. Measured on the corpus (30/09/2026): of 53 stale-read refusals, 26 had
        hashes differing at the FIRST character -- another block's stamp, never a version drift --
        and none of them named the owner, because the only lookup was over the children of the
        target document. This walks the index instead.
        """
        if not content_hash:
            return []
        found: List[Dict[str, str]] = []
        is_prefix = len(content_hash) < 64
        op = "LIKE ? || '%'" if is_prefix else "= ?"
        with self.get_connection() as conn:
            for row in conn.execute(
                f"SELECT id, file_path FROM symbols WHERE content_hash {op} LIMIT ?",
                (content_hash, limit),
            ).fetchall():
                found.append({"id": row["id"], "kind": "symbol", "file_path": row["file_path"]})
            for row in conn.execute(
                f"SELECT s.id AS id, d.file_path AS file_path FROM document_sections s "
                f"JOIN documents d ON d.id = s.document_id "
                f"WHERE s.content_hash {op} LIMIT ?",
                (content_hash, limit),
            ).fetchall():
                found.append({"id": row["id"], "kind": "section", "file_path": row["file_path"]})
            for row in conn.execute(
                f"SELECT id, file_path FROM documents WHERE content_hash {op} LIMIT ?",
                (content_hash, limit),
            ).fetchall():
                found.append({"id": row["id"], "kind": "document", "file_path": row["file_path"]})
        return found[:limit]

    def get_file_hash(self, file_path: str) -> Optional[str]:
        """Return the hash recorded the last time a file was indexed."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT content_hash FROM indexed_files WHERE file_path = ?", (file_path,)
            ).fetchone()
            return row["content_hash"] if row else None

    def get_indexed_paths(self) -> List[str]:
        """Every file path currently in the persistent index."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT file_path FROM indexed_files").fetchall()
            return [row["file_path"] for row in rows]

    def files_matching(self, file_pattern: Optional[str]) -> List[str]:
        """Which indexed files the caller's `file_pattern` actually selects.

        An empty search has two very different causes -- the pattern selects no file
        at all, or it selects files that simply do not carry the query -- and a
        message may only report the one it checked. The first version taught a rule
        instead ("a folder is 'folder/*'") and kept teaching it after a folder had
        started scoping to its content (declared on mapab2 a r1 of the base edfe80a).
        """
        if not file_pattern:
            return []
        return [
            file_path
            for file_path in self.get_indexed_paths()
            if self._inside_workspace(file_path) and path_matches(file_path, file_pattern)
        ]

    def get_meta(self, key: str) -> Optional[str]:
        """Read a small persisted setting (e.g. the index schema version)."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Persist a small setting."""
        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (key, value)
                )

    def get_file_hashes(self) -> Dict[str, str]:
        """Return every recorded file hash, for a one-shot startup preload."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT file_path, content_hash FROM indexed_files").fetchall()
            return {row["file_path"]: row["content_hash"] for row in rows}

    def set_file_hash(self, file_path: str, content_hash: str) -> None:
        """Record the hash of a file that was just indexed."""
        self._virtual_paths.add(str(file_path))
        self._inside_workspace_cache.pop(str(file_path), None)
        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO indexed_files (file_path, content_hash, indexed_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (file_path, content_hash),
                )

    def delete_file_hash(self, file_path: str) -> None:
        """Forget a file (used when it is deleted)."""
        self._virtual_paths.discard(str(file_path))
        self._inside_workspace_cache.pop(str(file_path), None)
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM indexed_files WHERE file_path = ?", (file_path,))

    def clear_indexed_files(self) -> None:
        """Force a full re-index on the next scan."""
        self._inside_workspace_cache.clear()
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM indexed_files")

    # ------------------------------------------------------------------
    # Search index (relevance scoring) and optional embeddings
    # ------------------------------------------------------------------
    def reset_search_index(self, file_path: str) -> None:
        """Drop the search entries of a file before re-adding them."""
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM search_index WHERE file_path = ?", (file_path,))

    def add_search_entries(self, entries: List[Dict[str, str]]) -> None:
        """Insert relevance entries (kind, ref_id, file_path, title, body)."""
        if not entries:
            return
        for e in entries:
            fp = str(e["file_path"])
            self._virtual_paths.add(fp)
            self._inside_workspace_cache.pop(fp, None)
        with self.get_connection() as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO search_index (kind, ref_id, file_path, title, body) VALUES (?, ?, ?, ?, ?)",
                    [
                        (e["kind"], e["ref_id"], e["file_path"], e.get("title", ""), e.get("body", ""))
                        for e in entries
                    ],
                )

    SEMANTIC_KIND_WEIGHTS: Dict[str, float] = {
        # Code symbols: x 1.5
        "class": 1.5,
        "interface": 1.5,
        "method": 1.5,
        "function": 1.5,
        "struct": 1.5,
        "record": 1.5,
        "enum": 1.5,
        # Documentation: x 1.0
        "section": 1.0,
        "document": 1.0,
        # JSON properties and scalars: x 0.3
        "property": 0.3,
        "array": 0.3,
        "string": 0.3,
        "number": 0.3,
        "boolean": 0.3,
        "variable": 0.3,
    }

    def search_index(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """BM25 search over the ontology. Higher score means more relevant."""
        if not query or not query.strip():
            return []
        fetch_limit = max(limit * 3, 100)
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT kind, ref_id, file_path, bm25(search_index) AS score "
                "FROM search_index WHERE search_index MATCH ? ORDER BY score LIMIT ?",
                (query, fetch_limit),
            ).fetchall()
            results = []
            for row in rows:
                if not self._inside_workspace(row["file_path"]):
                    continue
                kind = row["kind"]
                raw_score = -float(row["score"])
                weight = self.SEMANTIC_KIND_WEIGHTS.get(kind, 1.0)
                results.append(
                    {
                        "kind": kind,
                        "ref_id": row["ref_id"],
                        "file_path": row["file_path"],
                        "score": raw_score * weight,
                    }
                )
            results.sort(key=lambda item: item["score"], reverse=True)
            return results[:limit]

    def get_search_meta(self, ref_id: str) -> Optional[Dict[str, str]]:
        """Look up the kind and file of a search-index reference."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT kind, ref_id, file_path FROM search_index WHERE ref_id = ? LIMIT 1", (ref_id,)
            ).fetchone()
            if not row:
                return None
            return {"kind": row["kind"], "ref_id": row["ref_id"], "file_path": row["file_path"]}

    def clear_search_index(self) -> None:
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM search_index")

    def store_embeddings(self, items: List[Tuple[str, List[float]]]) -> None:
        """Persist embeddings for (ref_id, vector) pairs."""
        with self.get_connection() as conn:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO embeddings (ref_id, dim, vector) VALUES (?, ?, ?)",
                    [(ref_id, len(vector), array.array("f", vector).tobytes()) for ref_id, vector in items],
                )

    def delete_embeddings_for_file(self, file_path: str) -> None:
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM embeddings WHERE ref_id LIKE ?", (file_path + "%",))

    def all_embeddings(self) -> List[Tuple[str, List[float]]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT ref_id, vector FROM embeddings").fetchall()
            out: List[Tuple[str, List[float]]] = []
            for row in rows:
                vector = array.array("f")
                vector.frombytes(row["vector"])
                out.append((row["ref_id"], list(vector)))
            return out

    @staticmethod
    def _row_to_symbol(row: sqlite3.Row) -> SymbolInfo:
        """Convert a database row into a SymbolInfo instance."""
        return SymbolInfo(
            id=row["id"],
            file_path=row["file_path"],
            parent_id=row["parent_id"],
            name=row["name"],
            kind=row["kind"],
            signature=row["signature"],
            docstring=row["docstring"],
            start_byte=row["start_byte"],
            end_byte=row["end_byte"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            content_hash=row["content_hash"],
        )

    def get_symbol(self, symbol_id: str) -> Optional[SymbolInfo]:
        """Fetch symbol metadata by fully qualified ID."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
            return self._row_to_symbol(row) if row else None

    def get_first_child_symbol(self, parent_id: str) -> Optional[SymbolInfo]:
        """Fetch the first (lowest byte offset) direct child of a symbol."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM symbols WHERE parent_id = ? ORDER BY start_byte ASC LIMIT 1",
                (parent_id,),
            ).fetchone()
            return self._row_to_symbol(row) if row else None

    def get_last_child_symbol(self, parent_id: str) -> Optional[SymbolInfo]:
        """Fetch the last (highest byte offset) direct child of a symbol."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM symbols WHERE parent_id = ? ORDER BY start_byte DESC LIMIT 1",
                (parent_id,),
            ).fetchone()
            return self._row_to_symbol(row) if row else None

    def get_first_file_symbol(self, file_path: str) -> Optional[SymbolInfo]:
        """Fetch the first top-level (module scope) symbol of a file."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM symbols WHERE file_path = ? AND parent_id IS NULL "
                "ORDER BY start_byte ASC LIMIT 1",
                (file_path,),
            ).fetchone()
            return self._row_to_symbol(row) if row else None

    def get_symbol_calls(self, symbol_id: str) -> List[Dict[str, Any]]:
        """Fetch all calls made by a given symbol."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT callee_name, call_expr, callee_symbol_id FROM symbol_calls WHERE caller_id = ?",
                (symbol_id,),
            ).fetchall()
            return [
                {
                    "callee_name": r["callee_name"],
                    "call_expr": r["call_expr"],
                    "callee_symbol_id": r["callee_symbol_id"],
                }
                for r in rows
            ]


    def get_callers(
        self, symbol_name: str, symbol_id: str, limit: int = 20
    ) -> List[str]:
        """Fetch symbol ids of callers that call the given symbol (by name or symbol_id)."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT caller_id FROM symbol_calls
                WHERE callee_symbol_id = ? OR callee_name = ?
                ORDER BY caller_id
                LIMIT ?
                """,
                (symbol_id, symbol_name, limit),
            ).fetchall()
            return [r["caller_id"] for r in rows]

    def search_symbols(
        self,
        query: str,
        kind: str = "all",
        file_pattern: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Search symbols by name or id with kind and file filtering.

        The query is matched TERM BY TERM, exactly as search_documents does: a
        multi-word query is the union of its terms, ranked by how many of them a
        symbol carries. Matching the whole query as one LIKE pattern made a phrase
        return an EMPTY list with no explanation -- "ApplyAuthentication AuthMode
        SparkAuthMode" found nothing while each term alone found the symbol -- and
        the caller then split it by hand and concluded the symbols did not exist
        (measured on mapab2 a r1: two turns and a false negative).
        """
        terms = query_terms(query)
        if not terms and (query or "").strip():
            terms = [(query or "").strip().lower()]
        params: List[Any] = []
        if terms:
            patterns = ["%" + term + "%" for term in terms]
            where = "(" + " OR ".join("name LIKE ? OR id LIKE ?" for _ in terms) + ")"
            if kind and kind != "all":
                where += " AND kind = ?"
            hits = " + ".join(
                "CASE WHEN name LIKE ? OR id LIKE ? THEN 1 ELSE 0 END" for _ in terms
            )
            ordering = (
                "(" + hits + ") DESC, "
                "CASE WHEN name = ? THEN 1 WHEN name LIKE ? THEN 2 ELSE 3 END, id ASC"
            )
            params = [pattern for pattern in patterns for _ in (0, 1)]
            if kind and kind != "all":
                params.append(kind)
            params += [pattern for pattern in patterns for _ in (0, 1)]
            params += [query, f"{query}%"]
        else:
            where = "1"
            ordering = "id ASC"
        with self.get_connection() as conn:
            sql = (
                "SELECT id, file_path, kind, start_line, signature, name FROM symbols WHERE "
                + where + " ORDER BY " + ordering
            )
            rows = conn.execute(sql, params).fetchall()
            results: List[Dict[str, Any]] = []

            for r in rows:
                fpath = r["file_path"]
                if not self._inside_workspace(fpath):
                    continue
                if not path_matches(fpath, file_pattern):
                    continue

                results.append(
                    {
                        "symbol_id": r["id"],
                        "kind": r["kind"],
                        "file": r["file_path"],
                        "line": r["start_line"],
                        "signature": r["signature"],
                    }
                )
                if len(results) >= limit:
                    break

            return results

    # ------------------------------------------------------------------
    # Documents (project ontology)
    # ------------------------------------------------------------------
    def upsert_document(self, file_path: str, document: ParsedDocument, content_hash: str) -> None:
        """Replace a document and its sections atomically."""
        self._virtual_paths.add(str(file_path))
        self._inside_workspace_cache.pop(str(file_path), None)
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM documents WHERE file_path = ?", (file_path,))
                conn.execute("DELETE FROM document_sections_fts WHERE document_id = ?", (file_path,))
                conn.execute(
                    """
                    INSERT INTO documents (id, file_path, doc_type, title, section_count, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (file_path, file_path, document.doc_type, document.title, len(document.sections), content_hash),
                )
                # Stable, position-independent identifiers: a paragraph keeps its
                # id when other paragraphs are inserted or removed around it.
                section_ids = stable_section_ids(file_path, document.sections)
                for order_index, section in enumerate(document.sections):
                    section_id = section_ids[order_index]
                    section_hash = compute_content_hash(section.content.encode("utf-8"))
                    conn.execute(
                        """
                        INSERT INTO document_sections
                            (id, document_id, heading, level, order_index, content, content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (section_id, file_path, section.heading, section.level, order_index, section.content, section_hash),
                    )
                    conn.execute(
                        """
                        INSERT INTO document_sections_fts (section_id, document_id, heading, content)
                        VALUES (?, ?, ?, ?)
                        """,
                        (section_id, file_path, section.heading, section.content),
                    )

    def delete_document(self, file_path: str) -> None:
        """Remove a document and its sections."""
        self._virtual_paths.discard(str(file_path))
        self._inside_workspace_cache.pop(str(file_path), None)
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM documents WHERE file_path = ?", (file_path,))
                conn.execute("DELETE FROM document_sections_fts WHERE document_id = ?", (file_path,))

    def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Fetch document metadata by workspace-relative path."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            return dict(row) if row else None

    def get_symbols_for_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Every symbol of one file, in source order: the facts a file outline is made of."""
        normalized = str(file_path).replace("\\", "/")
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, name, parent_id, start_line, end_line, start_byte, end_byte "
                "FROM symbols WHERE file_path = ? ORDER BY start_byte ASC",
                (normalized,),
            ).fetchall()
        return [dict(row) for row in rows]
    def find_enclosing_symbol(self, file_path: str, line: int) -> Optional[Dict[str, Any]]:
        """Find the innermost symbol enclosing a 1-based file line number."""
        normalized = str(file_path).replace("\\", "/")
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, name, parent_id, start_line, end_line, start_byte, end_byte, content_hash "
                "FROM symbols WHERE file_path = ? AND start_line <= ? AND end_line >= ? "
                "ORDER BY (end_line - start_line) ASC, (end_byte - start_byte) ASC LIMIT 1",
                (normalized, line, line),
            ).fetchall()
        return dict(rows[0]) if rows else None

    def list_documents(self, file_pattern: Optional[str] = None, limit: Optional[int] = 200) -> List[Dict[str, Any]]:
        """List indexed documents with their title and section count."""
        if hasattr(file_pattern, "default"):
            file_pattern = file_pattern.default if str(file_pattern.default) != "PydanticUndefined" else None
        if hasattr(limit, "default"):
            limit = limit.default if str(limit.default) != "PydanticUndefined" else 200
        safe_limit = int(limit) if limit is not None else None
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, file_path, doc_type, title, section_count FROM documents ORDER BY file_path ASC"
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                if not self._inside_workspace(row["file_path"]):
                    continue
                if not path_matches(row["file_path"], file_pattern):
                    continue
                results.append(dict(row))
                if safe_limit is not None and len(results) >= safe_limit:
                    break
            return results

    def search_documents(
        self,
        query: str = "",
        file_pattern: Optional[str] = None,
        limit: int = 8,
        include_content: bool = False,
        max_content_chars: int = 4000,
        full_top: int = 0,
    ) -> List[Dict[str, Any]]:
        """Search document paragraphs by heading or content.

        The query is matched term by term and the paragraphs are ranked by how
        many terms they carry (a heading hit outranks a body hit), so a question
        that no single paragraph answers verbatim still returns its best partial
        evidence instead of an empty list.

        The best full_top matches come back with their body text inline, so the
        caller can act on the search alone instead of spending a further turn on
        get_document_sections. A body cut by max_content_chars is flagged with
        content_truncated/content_total_chars and never presented as complete.
        """
        if hasattr(query, "default"):
            query = query.default if str(query.default) != "PydanticUndefined" else ""
        if hasattr(file_pattern, "default"):
            file_pattern = file_pattern.default if str(file_pattern.default) != "PydanticUndefined" else None
        if hasattr(limit, "default"):
            limit = limit.default if str(limit.default) != "PydanticUndefined" else 8
        if hasattr(include_content, "default"):
            include_content = include_content.default if str(include_content.default) != "PydanticUndefined" else False
        if hasattr(max_content_chars, "default"):
            max_content_chars = max_content_chars.default if str(max_content_chars.default) != "PydanticUndefined" else 4000
        if hasattr(full_top, "default"):
            full_top = full_top.default if str(full_top.default) != "PydanticUndefined" else 0

        query_str = str(query or "")
        safe_limit = int(limit) if limit is not None else 8
        safe_full_top = max(0, int(full_top)) if full_top is not None else 0
        safe_max_content_chars = int(max_content_chars) if max_content_chars is not None else 4000
        include_content = bool(include_content)

        terms = query_terms(query_str)
        if not terms and query_str.strip():
            terms = [query_str.strip().lower()]
        columns = ("d.id AS document_id, d.file_path, d.doc_type, d.title, "
                   "s.id AS section_id, s.heading, s.level, s.order_index, "
                   "substr(s.content, 1, 240) AS snippet")
        params: List[Any] = []
        fts_tokens: List[str] = []
        if terms:
            patterns = ["%" + term + "%" for term in terms]
            heading_hits = " + ".join("CASE WHEN s.heading LIKE ? THEN 1 ELSE 0 END" for _ in terms)
            content_hits = " + ".join("CASE WHEN s.content LIKE ? THEN 1 ELSE 0 END" for _ in terms)
            phrase_bonus = "CASE WHEN s.content LIKE ? THEN 20 ELSE 0 END"
            score = "(100 * (" + heading_hits + ") + (" + content_hits + ") + " + phrase_bonus + ")"
            where = "(" + " OR ".join("s.heading LIKE ? OR s.content LIKE ?" for _ in terms) + ")"
            params = patterns + patterns + ["%" + query_str.strip() + "%"]
            params += [pattern for pattern in patterns for _ in (0, 1)]
            for term in terms:
                for tok in re.findall(r"\w{2,}", term, flags=re.UNICODE):
                    if tok not in fts_tokens:
                        fts_tokens.append(tok)
        else:
            score = "0"
            where = "1"
        fts_match_expr = " OR ".join(f'"{tok}"*' for tok in fts_tokens)
        with self.get_connection() as conn:
            rows = []
            if terms and fts_match_expr:
                fts_score = (
                    "(100 * (" + heading_hits + ") + (" + content_hits + ") + "
                    + phrase_bonus + " + ROUND(-bm25(document_sections_fts, 10.0, 1.0), 3))"
                )
                fts_params = patterns + patterns + ["%" + query_str.strip() + "%", fts_match_expr]
                try:
                    rows = conn.execute(
                        "SELECT " + columns + ", " + fts_score + " AS score "
                        "FROM document_sections s "
                        "JOIN documents d ON d.id = s.document_id "
                        "JOIN document_sections_fts ON s.id = document_sections_fts.section_id "
                        "WHERE document_sections_fts MATCH ? "
                        "ORDER BY score DESC, d.file_path ASC, s.order_index ASC",
                        fts_params,
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                rows = conn.execute(
                    "SELECT " + columns + ", " + score + " AS score "
                    "FROM document_sections s JOIN documents d ON d.id = s.document_id "
                    "WHERE " + where + " "
                    "ORDER BY score DESC, d.file_path ASC, s.order_index ASC",
                    params,
                ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if not self._inside_workspace(item["file_path"]):
                continue
            if not path_matches(item["file_path"], file_pattern):
                continue
            results.append(item)
            if len(results) >= safe_limit:
                break
        inlined = safe_full_top
        cap = safe_max_content_chars
        sections_to_fetch = [
            item["section_id"]
            for index, item in enumerate(results)
            if include_content or index < inlined
        ]
        if sections_to_fetch:
            content_map: Dict[str, str] = {}
            chunk_size = 900
            with self.get_connection() as conn:
                for i in range(0, len(sections_to_fetch), chunk_size):
                    chunk = sections_to_fetch[i : i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    chunk_rows = conn.execute(
                        f"SELECT id, content FROM document_sections WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for r in chunk_rows:
                        content_map[r["id"]] = r["content"] or ""
            for index, item in enumerate(results):
                if not include_content and index >= inlined:
                    continue
                content = content_map.get(item["section_id"], "")
                item["content_total_chars"] = len(content)
                if cap > 0 and len(content) > cap:
                    content = content[:cap]
                    item["content_truncated"] = True
                item["content"] = content
        return results

    def get_document_sections(self, document_id: str, include_content: bool = False) -> List[Dict[str, Any]]:
        """Return the table of contents (paragraph headings) of a document."""
        columns = "id, document_id, heading, level, order_index"
        if include_content:
            columns += ", content"
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT " + columns + " FROM document_sections WHERE document_id = ? ORDER BY order_index ASC",
                (document_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_document_section(self, section_id: str) -> Optional[Dict[str, Any]]:
        """Return one paragraph with its text, ready to be consumed directly.

        Accepts a stable id (`file#heading-slug`) or a legacy positional one
        (`file#N`), so identifiers captured before the stable scheme still resolve.
        """
        query = (
            "SELECT s.id AS section_id, s.document_id, s.heading, s.level, s.order_index, "
            "s.content, d.file_path, d.doc_type, d.title AS document_title "
            "FROM document_sections s JOIN documents d ON d.id = s.document_id WHERE "
        )
        with self.get_connection() as conn:
            row = conn.execute(query + "s.id = ?", (section_id,)).fetchone()
            if row is None:
                legacy = re.match(r"^(?P<document>.+)#(?P<index>[0-9]+)$", section_id)
                if legacy:
                    row = conn.execute(
                        query + "s.document_id = ? AND s.order_index = ?",
                        (legacy.group("document"), int(legacy.group("index"))),
                    ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _balanced_pick(items, key_func, budget_chars, start_used=0):
        """Round-robin selection across groups so no directory monopolizes the map."""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            groups.setdefault(key_func(item["value"]), []).append(item)
        queues: List[List[Dict[str, Any]]] = [list(groups[key]) for key in sorted(groups)]
        picked: List[Any] = []
        used = start_used
        truncated = False
        while queues:
            progressed = False
            for queue in list(queues):
                if not queue:
                    queues.remove(queue)
                    continue
                item = queue.pop(0)
                if used + len(item["line"]) > budget_chars:
                    truncated = True
                    continue
                used += len(item["line"])
                picked.append(item["value"])
                progressed = True
            if not progressed:
                break
        return picked, used, truncated

    def get_project_map(
        self,
        budget_tokens: int = 8000,
        file_pattern: Optional[str] = None,
        answer_format: str = "text",
        max_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return a compact, budgeted map of the indexed project.

        The budget is dynamically balanced between documents and code symbols via
        communicating vessels: unconsumed document budget flows into code budget.
        Selection prioritizes core code and entry points over tests and archives.
        """
        budget_chars = max(800, int(budget_tokens) * 4)
        result: Dict[str, Any] = {
            "budget_tokens": budget_tokens,
            "summary": {},
            "documents": [],
            "files": [],
            "truncated": False,
        }

        import json
        from .rendering import _format_doc_map_entry, _format_file_map_entry

        def cost_of(value: Dict[str, Any], text_form: str) -> str:
            """What one entry weighs ON THE WIRE in the format asked."""
            if answer_format == "json":
                return json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n    ") + ",\n    "
            return text_form + "\n"

        def compute_path_weight(path: str) -> float:
            norm = path.replace("\\", "/").lower()
            parts = norm.split("/")
            if any(part in ("tests", "test", ".archive", "archive") for part in parts) or "/tests/" in "/" + norm:
                return 0.4
            if parts[-1] in ("server.py", "main.py", "program.cs"):
                return 2.0
            if "src" in parts or norm.startswith("src/"):
                return 1.5
            return 1.0

        def compute_doc_weight(path: str) -> float:
            norm = path.replace("\\", "/").lower()
            parts = norm.split("/")
            if any(part in ("tests", "test", ".archive", "archive") for part in parts) or "/tests/" in "/" + norm:
                return 0.4
            if parts[-1] in ("readme.md", "index.md"):
                return 2.0
            if "docs" in parts or norm.startswith("docs/"):
                return 1.5
            return 1.0

        with self.get_connection() as conn:
            # Summary stats (grouped in SQL to avoid loading all symbol/section rows)
            kinds: Dict[str, int] = {}
            symbol_total = 0
            for row in conn.execute(
                "SELECT file_path, kind, COUNT(*) AS cnt FROM symbols GROUP BY file_path, kind"
            ):
                if not self._inside_workspace(row["file_path"]):
                    continue
                cnt = int(row["cnt"])
                symbol_total += cnt
                kinds[row["kind"]] = kinds.get(row["kind"], 0) + cnt

            doc_rows = [
                row
                for row in conn.execute(
                    "SELECT id, file_path, doc_type, title, section_count FROM documents ORDER BY file_path ASC"
                )
                if self._inside_workspace(row["file_path"])
            ]
            section_total = sum(int(row["section_count"] or 0) for row in doc_rows)
            types: Dict[str, int] = {}
            for row in doc_rows:
                types[row["doc_type"]] = types.get(row["doc_type"], 0) + 1
            result["summary"] = {
                "symbols": symbol_total,
                "documents": len(doc_rows),
                "sections": section_total,
                "symbol_kinds": dict(sorted(kinds.items(), key=lambda item: -item[1])),
                "document_types": dict(sorted(types.items(), key=lambda item: -item[1])),
            }

            # Preload top 7 non-empty headings per document in a single query (eliminates N+1)
            doc_headings_map: Dict[str, List[str]] = {}
            for h_row in conn.execute(
                """
                SELECT document_id, heading
                FROM (
                    SELECT document_id, heading,
                           ROW_NUMBER() OVER (PARTITION BY document_id ORDER BY order_index ASC) AS rn
                    FROM document_sections
                    WHERE heading != ''
                )
                WHERE rn <= 7
                ORDER BY document_id, rn
                """
            ):
                doc_headings_map.setdefault(h_row["document_id"], []).append(h_row["heading"])

            # 1. Documents selection with H1 title deduplication
            doc_items: List[Dict[str, Any]] = []
            for row in doc_rows:
                if not path_matches(row["file_path"], file_pattern):
                    continue
                raw_headings = list(doc_headings_map.get(row["id"], []))
                while raw_headings and row["title"] and raw_headings[0].strip().lower() == row["title"].strip().lower():
                    raw_headings.pop(0)
                section_titles = raw_headings[:6]
                doc_value = {
                    "file_path": row["file_path"],
                    "title": row["title"],
                    "sections": row["section_count"],
                    "section_titles": section_titles,
                }
                parts = row["file_path"].replace("\\", "/").strip("/").split("/")
                if max_depth is not None and len(parts) > max_depth:
                    continue
                doc_text = _format_doc_map_entry(parts[-1], doc_value, depth=len(parts))
                doc_weight = compute_doc_weight(row["file_path"])
                doc_score = (1.0 + 0.5 * (row["section_count"] or 0)) * doc_weight
                doc_items.append(
                    {
                        "line": cost_of(doc_value, doc_text),
                        "value": doc_value,
                        "score": doc_score,
                    }
                )

            # 2. Inbound call counts for centrality weighting
            calls_in_map: Dict[str, int] = {}
            for c_row in conn.execute(
                """
                SELECT s.file_path, count(*) as call_count
                FROM symbol_calls sc
                JOIN symbols s ON s.id = sc.callee_symbol_id
                GROUP BY s.file_path
                """
            ):
                calls_in_map[c_row["file_path"]] = c_row["call_count"]

            # 3. Root symbols selection without kind exclusion
            file_syms: Dict[str, List[Dict[str, Any]]] = {}
            for s_row in conn.execute(
                """
                SELECT file_path, name, kind, start_line, end_line 
                FROM symbols 
                WHERE (parent_id IS NULL OR parent_id = '')
                  AND kind NOT IN ('variable', 'imports')
                ORDER BY file_path ASC, start_byte ASC
                """
            ):
                fpath = s_row["file_path"]
                if not self._inside_workspace(fpath):
                    continue
                file_syms.setdefault(fpath, []).append(s_row)

            file_items: List[Dict[str, Any]] = []
            for fpath, s_rows in file_syms.items():
                if not path_matches(fpath, file_pattern):
                    continue
                parts = fpath.replace("\\", "/").strip("/").split("/")
                if max_depth is not None and len(parts) > max_depth:
                    continue
                names = [r["kind"] + " " + r["name"] for r in s_rows]
                sym_count = len(s_rows)
                loc = sum(max(0, r["end_line"] - r["start_line"] + 1) for r in s_rows)
                calls_in = calls_in_map.get(fpath, 0)
                p_weight = compute_path_weight(fpath)
                score = (1.5 * sym_count + 0.1 * loc + 5.0 * calls_in) * p_weight
                file_value = {
                    "file_path": fpath,
                    "symbols": names,
                    "sym_count": sym_count,
                    "loc": loc,
                    "symbol_details": [
                        {
                            "kind": r["kind"],
                            "name": r["name"],
                            "start_line": r["start_line"],
                            "end_line": r["end_line"],
                        }
                        for r in s_rows[:6]
                    ],
                }
                file_text = _format_file_map_entry(parts[-1], file_value, depth=len(parts))
                file_items.append(
                    {
                        "line": cost_of(file_value, file_text),
                        "value": file_value,
                        "score": score,
                    }
                )

            # Fallback when no container/root symbol was extracted: surface indexed non-doc files
            # and/or disk files in the workspace so a script-only or newly created workspace is
            # never answered with a blind "(none declares a container)".
            if not file_items and not file_pattern:
                doc_paths_set = {row["file_path"] for row in doc_rows}
                fallback_paths: List[str] = [
                    r["file_path"]
                    for r in conn.execute("SELECT file_path FROM indexed_files ORDER BY file_path ASC")
                    if self._inside_workspace(r["file_path"]) and r["file_path"] not in doc_paths_set
                ]
                if not fallback_paths and self._root is not None and self._root.is_dir():
                    try:
                        seen_disk: List[str] = []
                        for p in sorted(self._root.rglob("*")):
                            if len(seen_disk) >= 100:
                                break
                            if not p.is_file():
                                continue
                            try:
                                rel = p.relative_to(self._root).as_posix()
                            except ValueError:
                                continue
                            if any(
                                seg.startswith(".") or seg in ("node_modules", "__pycache__", "venv", ".venv", "bin", "obj", "dist", "build", "reports")
                                for seg in rel.split("/")
                            ):
                                continue
                            if rel not in doc_paths_set:
                                seen_disk.append(rel)
                        fallback_paths = seen_disk
                    except Exception:
                        fallback_paths = []
                for fpath in fallback_paths:
                    parts = fpath.replace("\\", "/").strip("/").split("/")
                    if max_depth is not None and len(parts) > max_depth:
                        continue
                    loc = 0
                    if self._root is not None:
                        try:
                            loc = len((self._root / fpath).read_bytes().splitlines())
                        except Exception:
                            loc = 0
                    file_value = {
                        "file_path": fpath,
                        "symbols": [],
                        "sym_count": 0,
                        "loc": loc,
                        "symbol_details": [],
                        "file_text_only": True,
                    }
                    file_text = _format_file_map_entry(parts[-1], file_value, depth=len(parts))
                    file_items.append(
                        {
                            "line": cost_of(file_value, file_text),
                            "value": file_value,
                            "score": 0.5 + 0.05 * loc,
                        }
                    )
                if not doc_items and not file_items and self._root is not None and self._root.is_dir():
                    try:
                        has_any = any(
                            not child.name.startswith(".")
                            for child in self._root.iterdir()
                        )
                        if not has_any:
                            result["workspace_empty"] = True
                    except Exception:
                        pass
        # 4. Budget allocation via communicating vessels
        fixed = (
            len(json.dumps({**result, "documents": [], "files": []}, indent=2, ensure_ascii=False)) + 120
            if answer_format == "json"
            else max(140, int(budget_chars * 0.03))
        )
        usable = max(0, budget_chars - fixed)
        doc_budget = int(usable * 0.40)
        code_budget = usable - doc_budget

        # Pack documents by descending score
        doc_items.sort(key=lambda item: (-item.get("score", 0.0), item["value"]["file_path"]))
        documents: List[Dict[str, Any]] = []
        used_docs = 0
        trunc_docs = False
        for it in doc_items:
            cost = len(it["line"])
            if used_docs + cost <= doc_budget:
                documents.append(it["value"])
                used_docs += cost
            else:
                trunc_docs = True

        # Transfer unconsumed document budget to code budget
        doc_remainder = max(0, doc_budget - used_docs)
        effective_code_budget = code_budget + doc_remainder

        # Pack code files by descending score until remaining budget is saturated
        file_items.sort(key=lambda item: (-item.get("score", 0.0), item["value"]["file_path"]))
        files: List[Dict[str, Any]] = []
        used_files = 0
        trunc_files = False
        for it in file_items:
            cost = len(it["line"])
            if used_files + cost <= effective_code_budget:
                files.append(it["value"])
                used_files += cost
            else:
                trunc_files = True

        result["documents"] = documents
        result["files"] = files
        result["truncated"] = trunc_docs or trunc_files
        result["used_tokens_estimate"] = (used_docs + used_files + fixed) // 4
        if file_pattern:
            result["file_pattern"] = file_pattern
            result["filtered_files"] = len(self.files_matching(file_pattern))
        return result

    def _closest_section_hint(self, section_id: str) -> str:
        """Why an id was not found, and which id is closest to it.

        An id is the path of the section CURRENT headings, so renaming a heading
        renames its id. "section not found" alone left the caller to read the whole
        outline to discover that: measured on a r1 of the base 86872df, one extra
        read_block turn (4.2 kch) to learn a name it had changed itself.
        """
        document_id, separator, _slug = section_id.partition("#")
        if not separator:
            return "section not found"
        rows = self.get_document_sections(document_id)
        if not rows:
            return "section not found: no document has this id."
        candidates = [str(row["id"]) for row in rows]
        close = difflib.get_close_matches(section_id, candidates, n=1, cutoff=0.4)
        if close and close[0] != section_id:
            return (
                "section not found; closest id in this document: '" + close[0] + "'. An id is the "
                "path of the CURRENT headings, so renaming a heading renames its id; "
                "read_block on the file gives the whole outline."
            )
        return (
            "section not found; this document has " + str(len(candidates))
            + " sections and none is close to this id."
        )

    def read_document_sections(
        self,
        section_ids: List[str],
        max_full: int = 3,
        max_content_chars: int = 0,
    ) -> Dict[str, Any]:
        """Read several document paragraphs in one call.

        Full text is returned for the first max_full sections; the remaining ones
        come back as heading + short excerpt, to protect the context budget.

        max_content_chars caps the text of the full sections; 0 (the default)
        returns them whole, because a targeted read should be complete while the
        caller has room for it. A section that *is* cut is flagged with
        content_truncated/content_total_chars and is never presented as complete.
        """
        sections: List[Dict[str, Any]] = []
        errors: Dict[str, str] = {}
        # max_full = 0 means "every requested paragraph", because the caller
        # asked for exactly those ids: an arbitrary ceiling of 3 turns a single
        # batch read into a batch plus a follow-up read. The transport still
        # bounds the reply if the whole batch does not fit the context window.
        full_budget = int(max_full)
        if full_budget <= 0:
            full_budget = len(section_ids)
        limit = int(max_content_chars)
        for index, section_id in enumerate(section_ids):
            section = self.get_document_section(section_id)
            if section is None:
                errors[section_id] = self._closest_section_hint(section_id)
                continue
            content = section.get("content") or ""
            if index < full_budget:
                if limit > 0 and len(content) > limit:
                    section["content"] = content[:limit]
                    section["content_truncated"] = True
                    section["content_total_chars"] = len(content)
                section["view"] = "full"
            else:
                section["snippet"] = content[:240]
                section["content_total_chars"] = len(content)
                section.pop("content", None)
                section["view"] = "excerpt"
            sections.append(section)
        return {"sections": sections, "errors": errors, "max_full": full_budget}

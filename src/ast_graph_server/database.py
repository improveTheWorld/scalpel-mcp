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
        self._init_db()

    def _inside_workspace(self, file_path: Any) -> bool:
        """False for a stored path that resolves outside the workspace root."""
        if self._root is None:
            return True
        key = str(file_path)
        cached = self._inside_workspace_cache.get(key)
        if cached is not None:
            return cached
        try:
            resolved = (self._root / key).resolve()
            resolved.relative_to(self._root)
            ans = True
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

    def upsert_file_symbols(self, file_path: str, symbols: List[SymbolInfo], calls: List[CallInfo]) -> None:
        """Replace all symbols and calls for a given file atomically."""
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
            if path_matches(file_path, file_pattern)
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
        with self.get_connection() as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO indexed_files (file_path, content_hash, indexed_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (file_path, content_hash),
                )

    def delete_file_hash(self, file_path: str) -> None:
        """Forget a file (used when it is deleted)."""
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM indexed_files WHERE file_path = ?", (file_path,))

    def clear_indexed_files(self) -> None:
        """Force a full re-index on the next scan."""
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
        with self.get_connection() as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO search_index (kind, ref_id, file_path, title, body) VALUES (?, ?, ?, ?, ?)",
                    [
                        (e["kind"], e["ref_id"], e["file_path"], e.get("title", ""), e.get("body", ""))
                        for e in entries
                    ],
                )

    def search_index(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """BM25 search over the ontology. Higher score means more relevant."""
        if not query or not query.strip():
            return []
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT kind, ref_id, file_path, bm25(search_index) AS score "
                "FROM search_index WHERE search_index MATCH ? ORDER BY score LIMIT ?",
                (query, limit),
            ).fetchall()
            return [
                {
                    "kind": row["kind"],
                    "ref_id": row["ref_id"],
                    "file_path": row["file_path"],
                    "score": -float(row["score"]),
                }
                for row in rows
            ]

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
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM documents WHERE file_path = ?", (file_path,))
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

    def delete_document(self, file_path: str) -> None:
        """Remove a document and its sections."""
        with self.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM documents WHERE file_path = ?", (file_path,))

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

    def list_documents(self, file_pattern: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """List indexed documents with their title and section count."""
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
                if len(results) >= limit:
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
        terms = query_terms(query)
        columns = ("d.id AS document_id, d.file_path, d.doc_type, d.title, "
                   "s.id AS section_id, s.heading, s.level, s.order_index, "
                   "substr(s.content, 1, 240) AS snippet")
        params: List[Any] = []
        if terms:
            patterns = ["%" + term + "%" for term in terms]
            heading_hits = " + ".join("CASE WHEN s.heading LIKE ? THEN 1 ELSE 0 END" for _ in terms)
            content_hits = " + ".join("CASE WHEN s.content LIKE ? THEN 1 ELSE 0 END" for _ in terms)
            phrase_bonus = "CASE WHEN s.content LIKE ? THEN 20 ELSE 0 END"
            score = "(100 * (" + heading_hits + ") + (" + content_hits + ") + " + phrase_bonus + ")"
            where = "(" + " OR ".join("s.heading LIKE ? OR s.content LIKE ?" for _ in terms) + ")"
            params = patterns + patterns + ["%" + (query or "").strip() + "%"]
            params += [pattern for pattern in patterns for _ in (0, 1)]
        else:
            score = "0"
            where = "1"
        with self.get_connection() as conn:
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
            if len(results) >= int(limit):
                break
        inlined = max(0, int(full_top))
        cap = int(max_content_chars)
        for index, item in enumerate(results):
            if not include_content and index >= inlined:
                continue
            section = self.get_document_section(item["section_id"]) or {}
            content = section.get("content") or ""
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
    ) -> Dict[str, Any]:
        """Return a compact, budgeted map of the indexed project.

        The budget is split between documents and code symbols, and selection is
        round-robin across directories, so a single large subtree (hundreds of
        bug notes, for instance) cannot hide the rest of the repository.
        """
        budget_chars = max(800, int(budget_tokens) * 4)
        doc_budget = int(budget_chars * 0.55)
        file_budget = budget_chars - doc_budget
        result: Dict[str, Any] = {
            "budget_tokens": budget_tokens,
            "summary": {},
            "documents": [],
            "files": [],
            "truncated": False,
        }

        import json

        def cost_of(value: Dict[str, Any], text_form: str) -> str:
            """What one entry weighs ON THE WIRE in the format asked.

            The selection used to count only `path :: title`: the text form adds the section
            count, the JSON form adds its keys and its indentation (measured on this repository:
            a 200-token request came back x1.13 in text and x1.63 in JSON).
            """
            if answer_format == "json":
                return json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n    ") + ",\n    "
            return text_form + "\n"

        with self.get_connection() as conn:
            # Every count is filtered by containment too: a summary that counts a row
            # the listing refuses is a lie about the size of the project.
            kind_rows = [
                row for row in conn.execute("SELECT file_path, kind FROM symbols")
                if self._inside_workspace(row["file_path"])
            ]
            doc_rows = [
                row for row in conn.execute("SELECT file_path, doc_type FROM documents")
                if self._inside_workspace(row["file_path"])
            ]
            section_total = sum(
                1 for row in conn.execute(
                    "SELECT d.file_path FROM document_sections s "
                    "JOIN documents d ON d.id = s.document_id"
                ) if self._inside_workspace(row["file_path"])
            )
            kinds: Dict[str, int] = {}
            for row in kind_rows:
                kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
            types: Dict[str, int] = {}
            for row in doc_rows:
                types[row["doc_type"]] = types.get(row["doc_type"], 0) + 1
            result["summary"] = {
                "symbols": len(kind_rows),
                "documents": len(doc_rows),
                "sections": section_total,
                "symbol_kinds": dict(sorted(kinds.items(), key=lambda item: -item[1])),
                "document_types": dict(sorted(types.items(), key=lambda item: -item[1])),
            }

            doc_items: List[Dict[str, Any]] = []
            for row in conn.execute("SELECT id, file_path, title, section_count FROM documents ORDER BY file_path ASC"):
                if not self._inside_workspace(row["file_path"]):
                    continue
                if not path_matches(row["file_path"], file_pattern):
                    continue
                sec_rows = conn.execute(
                    "SELECT heading FROM document_sections WHERE document_id = ? ORDER BY order_index ASC LIMIT 5",
                    (row["id"],),
                ).fetchall()
                section_titles = [s["heading"] for s in sec_rows if s["heading"]]
                doc_value = {
                    "file_path": row["file_path"],
                    "title": row["title"],
                    "sections": row["section_count"],
                    "section_titles": section_titles,
                }
                sec_desc = ", ".join(section_titles) if section_titles else str(row["section_count"]) + " sections"
                doc_line = "  " + row["file_path"] + " :: " + (row["title"] or "") + " (" + sec_desc + ")"
                doc_items.append(
                    {
                        "line": cost_of(doc_value, doc_line),
                        "value": doc_value,
                    }
                )

            grouped: Dict[str, List[str]] = {}
            for row in conn.execute(
                "SELECT file_path, name, kind FROM symbols "
                "WHERE kind IN ('class','interface','namespace','struct','record','enum') "
                "ORDER BY file_path ASC, start_byte ASC"
            ):
                if not self._inside_workspace(row["file_path"]):
                    continue
                grouped.setdefault(row["file_path"], []).append(row["kind"] + " " + row["name"])
            file_items: List[Dict[str, Any]] = []
            for fpath, names in grouped.items():
                if not path_matches(fpath, file_pattern):
                    continue
                file_value = {"file_path": fpath, "symbols": names}
                file_items.append(
                    {"line": cost_of(file_value, "  " + fpath + " :: " + ", ".join(names)), "value": file_value}
                )

        def group_of(value: Dict[str, Any]) -> str:
            return str(Path(value["file_path"]).parent).replace("\\", "/")

        # The budget bounds what is RETURNED, in the format asked: every entry was weighed as it is
        # rendered (see cost_of), and what surrounds the entries -- the header lines, or the JSON
        # summary -- is paid first, out of the same budget.
        fixed = (
            len(json.dumps({**result, "documents": [], "files": []}, indent=2, ensure_ascii=False)) + 120
            if answer_format == "json"
            else 140
        )
        usable = max(0, budget_chars - fixed)
        doc_budget = int(usable * 0.55)
        file_budget = usable - doc_budget
        documents, used_docs, trunc_docs = self._balanced_pick(doc_items, group_of, doc_budget)
        files, used_files, trunc_files = self._balanced_pick(file_items, group_of, file_budget)
        result["documents"] = documents
        result["files"] = files
        result["truncated"] = trunc_docs or trunc_files
        result["used_tokens_estimate"] = (used_docs + used_files + fixed) // 4
        # An empty section under a filter is not an answer: the map says which files the
        # pattern did select, so a script it cannot list is told apart from a wrong path
        # (declared on mapab2 b r2 of the base c819545: "I could not confirm whether those
        # two scripts exist on disk from this output").
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

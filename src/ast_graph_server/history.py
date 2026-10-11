"""Atomic delta history manager for Scalpel.

Provides SHA-256 deduplicated blob storage, SQLite WAL-based commit journal,
and 2PC transactional guarantees for atomic workspace mutations.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from .indexer import Indexer
from .watcher import SuppressionRegistry


class HistoryManager:
    """Manages atomic delta history, blob content-addressed storage, and crash recovery."""

    def __init__(
        self,
        workspace_root: Path | str,
        history_dir: Path | str,
        suppression_registry: Optional[SuppressionRegistry] = None,
        indexer: Optional[Indexer] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.history_dir = Path(history_dir).resolve()
        self.blobs_dir = self.history_dir / "blobs"
        self.db_path = self.history_dir / "history.db"
        self.suppression = suppression_registry
        self.indexer = indexer
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        """Yield an active SQLite connection and ensure clean closure."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initialize database directory, tables, indexes, and WAL pragma."""
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    timestamp REAL NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    tool_name TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commit_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    before_sha TEXT,
                    after_sha TEXT,
                    FOREIGN KEY (commit_id) REFERENCES commits(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_commits_status ON commits(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_commit_files_commit ON commit_files(commit_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_commit_files_file ON commit_files(file_path)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS branches (
                    name TEXT PRIMARY KEY,
                    head_commit_id TEXT,
                    created_at REAL NOT NULL
                )
            """)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'head'")
            head_row = cursor.fetchone()
            current_head = head_row["value"] if head_row else None
            conn.execute("""
                INSERT OR IGNORE INTO branches (name, head_commit_id, created_at)
                VALUES ('main', ?, ?)
            """, (current_head, time.time()))
            if current_head:
                conn.execute("""
                    UPDATE branches SET head_commit_id = ? WHERE name = 'main' AND head_commit_id IS NULL
                """, (current_head,))
            conn.execute("""
                INSERT OR IGNORE INTO history_meta (key, value)
                VALUES ('active_branch', 'main')
            """)

    def _to_rel_posix(self, path: Path | str) -> str:
        """Convert path to relative POSIX path against workspace_root if possible."""
        p = Path(path)
        if p.is_absolute():
            try:
                return p.resolve().relative_to(self.workspace_root).as_posix()
            except ValueError:
                return p.as_posix()
        return p.as_posix()

    def blob_path_for(self, sha: str) -> Path:
        """Return the physical Path for a given SHA-256 hex string."""
        return self.blobs_dir / sha[:2] / sha[2:]

    def has_blob(self, sha: str) -> bool:
        """Check whether a blob exists in CAS storage."""
        return self.blob_path_for(sha).exists()

    def read_blob(self, sha: str) -> bytes:
        """Read bytes of a stored blob."""
        return self.blob_path_for(sha).read_bytes()

    def save_blob(self, content_bytes: bytes) -> str:
        """Atomically persist content_bytes into CAS storage and return its SHA-256."""
        sha = hashlib.sha256(content_bytes).hexdigest()
        target = self.blob_path_for(sha)
        if target.exists():
            return sha
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = f".tmp_blob_{uuid.uuid4().hex}"
        tmp_path = target.parent / tmp_name
        try:
            tmp_path.write_bytes(content_bytes)
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        return sha

    def count_blob_instances(self, sha: str) -> int:
        """Count physical blob files stored for this sha across blobs directory."""
        target = self.blob_path_for(sha)
        if not target.exists():
            return 0
        sub = sha[2:]
        matches = [f for f in self.blobs_dir.rglob("*") if f.is_file() and f.name in (sha, sub)]
        return len(matches)

    def record_pending_commit(
        self,
        commit_id: str,
        parent_id: Optional[str],
        kind: str,
        tool_name: str,
        files: List[Dict[str, Any]],
    ) -> str:
        """Record a transaction in 'pending' status before physical disk mutation."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO commits (id, parent_id, timestamp, status, kind, tool_name) VALUES (?, ?, ?, 'pending', ?, ?)",
                (commit_id, parent_id, time.time(), kind, tool_name),
            )
            for f in files:
                rel_path = self._to_rel_posix(f["file_path"])
                conn.execute(
                    "INSERT INTO commit_files (commit_id, file_path, before_sha, after_sha) VALUES (?, ?, ?, ?)",
                    (commit_id, rel_path, f.get("before_sha"), f.get("after_sha")),
                )
        return commit_id

    def commit_transaction(self, commit_id: str) -> None:
        """Promote a pending transaction to 'committed' and advance HEAD."""
        with self._get_connection() as conn:
            conn.execute("UPDATE commits SET status = 'committed' WHERE id = ?", (commit_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            conn.execute("UPDATE branches SET head_commit_id = ? WHERE name = ?", (commit_id, active_branch))
            conn.execute(
                "INSERT INTO history_meta (key, value) VALUES ('head', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (commit_id,),
            )

    def abort_transaction(self, commit_id: str) -> None:
        """Mark a pending transaction as 'aborted'."""
        with self._get_connection() as conn:
            conn.execute("UPDATE commits SET status = 'aborted' WHERE id = ?", (commit_id,))

    def get_head_commit_id(self) -> Optional[str]:
        """Return the current HEAD commit id, or None."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            cursor.execute("SELECT head_commit_id FROM branches WHERE name = ?", (active_branch,))
            b_row = cursor.fetchone()
            if b_row and b_row["head_commit_id"]:
                return b_row["head_commit_id"]
            cursor.execute("SELECT value FROM history_meta WHERE key = 'head'")
            h_row = cursor.fetchone()
            if h_row and h_row["value"]:
                return h_row["value"]
            return None

    def get_commit(self, commit_id: str) -> Optional[Dict[str, Any]]:
        """Fetch commit metadata dictionary."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, parent_id, timestamp, status, kind, tool_name FROM commits WHERE id = ?",
                (commit_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "timestamp": row["timestamp"],
                "status": row["status"],
                "kind": row["kind"],
                "tool_name": row["tool_name"],
            }

    def get_commit_files(self, commit_id: str) -> List[Dict[str, Any]]:
        """Fetch list of affected files for a commit."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, before_sha, after_sha FROM commit_files WHERE commit_id = ? ORDER BY id ASC",
                (commit_id,),
            )
            return [
                {
                    "file_path": row["file_path"],
                    "before_sha": row["before_sha"],
                    "after_sha": row["after_sha"],
                }
                for row in cursor.fetchall()
            ]

    def recover_pending(self) -> Dict[str, List[str]]:
        """Reconcile pending transactions at startup (Cas A -> committed, Cas B -> aborted)."""
        committed_list: List[str] = []
        aborted_list: List[str] = []

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM commits WHERE status = 'pending' ORDER BY timestamp ASC")
            pending_ids = [row["id"] for row in cursor.fetchall()]

        for pending_id in pending_ids:
            files = self.get_commit_files(pending_id)
            all_match = True
            if not files:
                all_match = False
            else:
                for f in files:
                    rel_path = f["file_path"]
                    target = self.workspace_root / rel_path
                    expected_after = f["after_sha"]
                    if expected_after is None:
                        if target.exists():
                            all_match = False
                            break
                    else:
                        if not target.exists():
                            all_match = False
                            break
                        file_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                        if file_sha != expected_after:
                            all_match = False
                            break

            if all_match:
                self.commit_transaction(pending_id)
                if self.indexer is not None:
                    for f in files:
                        target = self.workspace_root / f["file_path"]
                        if f["after_sha"] is None:
                            try:
                                self.indexer.remove_file(target)
                            except Exception:
                                pass
                        else:
                            try:
                                self.indexer.index_file(target)
                            except Exception:
                                pass
                committed_list.append(pending_id)
            else:
                self.abort_transaction(pending_id)
                aborted_list.append(pending_id)

        return {"committed": committed_list, "aborted": aborted_list}

    def rollback_head(self) -> Dict[str, Any]:
        """Rollback HEAD commit bit-exactly to its parent state, checking for divergence."""
        head_id = self.get_head_commit_id()
        if head_id is None:
            return {"success": False, "error": "No HEAD commit to rollback"}
        commit = self.get_commit(head_id)
        if commit is None or commit["status"] != "committed":
            return {"success": False, "error": f"HEAD commit '{head_id}' is not in committed status"}

        files = self.get_commit_files(head_id)

        # 1. Vérification préventive anti-divergence
        for f in files:
            file_rel = f["file_path"]
            target = self.workspace_root / file_rel
            expected_after_sha = f["after_sha"]
            if expected_after_sha is None:
                if target.exists():
                    return {
                        "success": False,
                        "error": f"File '{file_rel}' modified since HEAD: expected to be absent but file exists on disk",
                    }
            else:
                if not target.exists():
                    return {
                        "success": False,
                        "error": f"File '{file_rel}' modified since HEAD: expected {expected_after_sha} but file does not exist on disk",
                    }
                current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                if current_sha != expected_after_sha:
                    return {
                        "success": False,
                        "error": f"File '{file_rel}' modified since HEAD: expected {expected_after_sha}, got {current_sha}",
                    }

        # 2. Restauration bit-exact des fichiers
        for f in files:
            file_rel = f["file_path"]
            target = self.workspace_root / file_rel
            if self.suppression:
                self.suppression.suppress(target)

            before_sha = f["before_sha"]
            if before_sha is None:
                # Fichier créé par ce commit -> suppression physique + remove_file dans l'index
                if target.exists():
                    try:
                        target.unlink()
                    except OSError:
                        pass
                if self.indexer is not None:
                    try:
                        self.indexer.remove_file(target)
                    except Exception:
                        pass
            else:
                # Fichier modifié -> restauration du blob
                before_bytes = self.read_blob(before_sha)
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_name = f".tmp_rb_{uuid.uuid4().hex}"
                tmp_path = target.parent / tmp_name
                try:
                    tmp_path.write_bytes(before_bytes)
                    os.replace(tmp_path, target)
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                if self.indexer is not None:
                    try:
                        self.indexer.index_file(target)
                    except Exception:
                        pass

        # 3. Mise à jour de l'historique
        with self._get_connection() as conn:
            conn.execute("UPDATE commits SET status = 'rolled_back' WHERE id = ?", (head_id,))
            new_head = commit["parent_id"]
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            conn.execute(
                "UPDATE branches SET head_commit_id = ? WHERE name = ?",
                (new_head, active_branch),
            )
            if new_head:
                conn.execute(
                    "INSERT INTO history_meta (key, value) VALUES ('head', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (new_head,),
                )
            else:
                conn.execute("DELETE FROM history_meta WHERE key = 'head'")

        return {"success": True, "rolled_back_commit_id": head_id}

    def record_external_modification(self, file_path: str | Path) -> str:
        """Create a foreign commit tracking mutation made outside Scalpel."""
        rel_path = self._to_rel_posix(file_path)
        target = self.workspace_root / rel_path

        after_sha: Optional[str] = None
        if target.exists() and target.is_file():
            content_bytes = target.read_bytes()
            after_sha = self.save_blob(content_bytes)

        before_sha: Optional[str] = None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT cf.after_sha 
                FROM commit_files cf
                JOIN commits c ON cf.commit_id = c.id
                WHERE cf.file_path = ? AND c.status = 'committed'
                ORDER BY c.timestamp DESC, cf.id DESC
                LIMIT 1
            """, (rel_path,))
            row = cursor.fetchone()
            if row:
                before_sha = row["after_sha"]

        commit_id = str(uuid.uuid4())
        parent_id = self.get_head_commit_id()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO commits (id, parent_id, timestamp, status, kind, tool_name) VALUES (?, ?, ?, 'committed', 'foreign', 'external')",
                (commit_id, parent_id, time.time()),
            )
            conn.execute(
                "INSERT INTO commit_files (commit_id, file_path, before_sha, after_sha) VALUES (?, ?, ?, ?)",
                (commit_id, rel_path, before_sha, after_sha),
            )
            conn.execute(
                "INSERT INTO history_meta (key, value) VALUES ('head', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (commit_id,),
            )
        return commit_id

    def _three_way_merge(self, base_text: str, a_text: str, c_text: str) -> Tuple[bool, str]:
        """
        Perform a 3-way inverse merge applying variation (base -> a) onto current text c.
        Returns (success, merged_text_or_conflict_reason).
        """
        base_lines = base_text.splitlines(keepends=True)
        a_lines = a_text.splitlines(keepends=True)
        c_lines = c_text.splitlines(keepends=True)

        sm_a = difflib.SequenceMatcher(None, base_lines, a_lines)
        sm_c = difflib.SequenceMatcher(None, base_lines, c_lines)

        ops_a = [op for op in sm_a.get_opcodes() if op[0] != "equal"]
        ops_c = [op for op in sm_c.get_opcodes() if op[0] != "equal"]

        # Conflict detection between operations from A and C against base
        for tag_a, ia1, ia2, ja1, ja2 in ops_a:
            rep_a = a_lines[ja1:ja2]
            for tag_c, ic1, ic2, kc1, kc2 in ops_c:
                rep_c = c_lines[kc1:kc2]
                overlap = False
                if ia1 == ia2 and ic1 == ic2:
                    if ia1 == ic1:
                        overlap = True
                elif ia1 == ia2:
                    if ic1 <= ia1 <= ic2:
                        overlap = True
                elif ic1 == ic2:
                    if ia1 <= ic1 <= ia2:
                        overlap = True
                else:
                    if max(ia1, ic1) < min(ia2, ic2):
                        overlap = True

                if overlap:
                    if ia1 == ic1 and ia2 == ic2 and rep_a == rep_c:
                        continue
                    return False, f"Direct conflict at base lines [{min(ia1, ic1)+1}:{max(ia2, ic2)+1}]"

        # No conflicts: combine changes in base coordinates
        all_changes = []
        for tag_a, ia1, ia2, ja1, ja2 in ops_a:
            all_changes.append((ia1, ia2, a_lines[ja1:ja2]))
        for tag_c, ic1, ic2, kc1, kc2 in ops_c:
            rep_c = c_lines[kc1:kc2]
            duplicate = False
            for ia1, ia2, rep_a in all_changes:
                if ia1 == ic1 and ia2 == ic2 and rep_a == rep_c:
                    duplicate = True
                    break
            if not duplicate:
                all_changes.append((ic1, ic2, rep_c))

        all_changes.sort(key=lambda x: (x[0], x[1]))

        merged_lines = []
        pos = 0
        for i1, i2, rep in all_changes:
            if pos < i1:
                merged_lines.extend(base_lines[pos:i1])
            merged_lines.extend(rep)
            pos = max(pos, i2)

        if pos < len(base_lines):
            merged_lines.extend(base_lines[pos:])

        return True, "".join(merged_lines)

    def revert_commit(self, commit_id: str) -> Dict[str, Any]:
        """
        Revert a specific committed transaction (head or historical ancestor).
        Performs 3-way inverse merge if needed, verifies against conflicts/modifications,
        and atomically records a new 'revert' commit at HEAD on success.
        """
        target_commit = self.get_commit(commit_id)
        if target_commit is None:
            return {"success": False, "error": f"Commit '{commit_id}' not found"}
        if target_commit["status"] != "committed":
            return {"success": False, "error": f"Commit '{commit_id}' is not in committed status"}

        files = self.get_commit_files(commit_id)
        if not files:
            return {"success": False, "error": f"No files recorded for commit '{commit_id}'"}

        # Phase 1: DRY RUN and 3-way merge validation (all-or-nothing check before touching disk)
        prepared_actions: List[Dict[str, Any]] = []
        revert_files: List[Dict[str, Any]] = []

        for f in files:
            file_rel = f["file_path"]
            target = self.workspace_root / file_rel
            before_sha = f["before_sha"]
            after_sha = f["after_sha"]

            # Cas 1: Revert d'une création historique (before_sha is None)
            if before_sha is None:
                if not target.exists():
                    return {"success": False, "error": f"File '{file_rel}' does not exist on disk"}
                current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                if current_sha != after_sha:
                    return {
                        "success": False,
                        "error": f"File '{file_rel}' was modified since creation",
                    }
                prepared_actions.append({
                    "action": "delete",
                    "target": target,
                    "rel_path": file_rel,
                })
                revert_files.append({
                    "file_path": file_rel,
                    "before_sha": current_sha,
                    "after_sha": None,
                })

            # Cas 2: Revert d'une suppression historique (after_sha is None)
            elif after_sha is None:
                if target.exists():
                    return {
                        "success": False,
                        "error": f"Cannot revert deletion of '{file_rel}': file already exists on disk",
                    }
                if not self.has_blob(before_sha):
                    return {"success": False, "error": f"Missing blob '{before_sha}' for file '{file_rel}'"}
                content_bytes = self.read_blob(before_sha)
                prepared_actions.append({
                    "action": "create",
                    "target": target,
                    "rel_path": file_rel,
                    "content_bytes": content_bytes,
                })
                revert_files.append({
                    "file_path": file_rel,
                    "before_sha": None,
                    "after_sha": before_sha,
                })

            # Cas 3: Revert d'une modification historique (before_sha et after_sha non nuls)
            else:
                if not target.exists():
                    return {"success": False, "error": f"File '{file_rel}' does not exist on disk"}
                if not self.has_blob(before_sha) or not self.has_blob(after_sha):
                    return {"success": False, "error": f"Missing historical blobs for file '{file_rel}'"}

                blob_a = self.read_blob(before_sha)
                blob_b = self.read_blob(after_sha)
                current_bytes = target.read_bytes()
                current_sha = hashlib.sha256(current_bytes).hexdigest()

                # Sous-cas 3A: Fichier non touché depuis le commit cible
                if current_sha == after_sha or current_bytes == blob_b:
                    new_bytes = blob_a
                    new_sha = before_sha
                # Sous-cas 3B: Fichier touché par des commits ultérieurs -> 3-way merge
                else:
                    try:
                        b_text = blob_b.decode("utf-8")
                        a_text = blob_a.decode("utf-8")
                        c_text = current_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        return {
                            "success": False,
                            "error": f"Conflict: Cannot perform 3-way merge on binary file '{file_rel}'",
                        }

                    merge_ok, merge_result = self._three_way_merge(b_text, a_text, c_text)
                    if not merge_ok:
                        return {
                            "success": False,
                            "error": f"Conflict: {merge_result} in file '{file_rel}'",
                        }
                    new_bytes = merge_result.encode("utf-8")
                    new_sha = self.save_blob(new_bytes)

                prepared_actions.append({
                    "action": "write",
                    "target": target,
                    "rel_path": file_rel,
                    "content_bytes": new_bytes,
                })
                revert_files.append({
                    "file_path": file_rel,
                    "before_sha": current_sha,
                    "after_sha": new_sha,
                })

        # Phase 2: Application physique et traçabilité ACID
        # 1. Suppressions préventives d'événements fs
        for act in prepared_actions:
            if self.suppression:
                self.suppression.suppress(act["target"])

        # 2. Écritures physiques atomiques
        for act in prepared_actions:
            target_file: Path = act["target"]
            if act["action"] == "delete":
                if target_file.exists():
                    try:
                        target_file.unlink()
                    except OSError:
                        pass
                if self.indexer is not None:
                    try:
                        self.indexer.remove_file(target_file)
                    except Exception:
                        pass
            elif act["action"] in ("create", "write"):
                target_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_name = f".tmp_rev_{uuid.uuid4().hex}"
                tmp_path = target_file.parent / tmp_name
                try:
                    tmp_path.write_bytes(act["content_bytes"])
                    os.replace(tmp_path, target_file)
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                if self.indexer is not None:
                    try:
                        self.indexer.index_file(target_file)
                    except Exception:
                        pass

        # 3. Enregistrement du nouveau commit 'revert' au sommet de HEAD
        revert_commit_id = str(uuid.uuid4())
        current_head = self.get_head_commit_id()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO commits (id, parent_id, timestamp, status, kind, tool_name) VALUES (?, ?, ?, 'committed', 'revert', 'revert_commit')",
                (revert_commit_id, current_head, time.time()),
            )
            for rf in revert_files:
                conn.execute(
                    "INSERT INTO commit_files (commit_id, file_path, before_sha, after_sha) VALUES (?, ?, ?, ?)",
                    (revert_commit_id, rf["file_path"], rf["before_sha"], rf["after_sha"]),
                )
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            conn.execute(
                "UPDATE branches SET head_commit_id = ? WHERE name = ?",
                (revert_commit_id, active_branch),
            )
            conn.execute(
                "INSERT INTO history_meta (key, value) VALUES ('head', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (revert_commit_id,),
            )

        return {
            "success": True,
            "revert_commit_id": revert_commit_id,
            "reverted_commit_id": commit_id,
            "files": [rf["file_path"] for rf in revert_files],
        }

    def create_branch(self, name: str) -> Dict[str, Any]:
        """Create a new branch pointing to the current HEAD commit."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM branches WHERE name = ?", (name,))
            if cursor.fetchone():
                return {"success": False, "error": f"Branch '{name}' already exists"}
            current_head = self.get_head_commit_id()
            conn.execute(
                "INSERT INTO branches (name, head_commit_id, created_at) VALUES (?, ?, ?)",
                (name, current_head, time.time()),
            )
        return {"success": True, "branch": name, "head_commit_id": current_head}

    def list_branches(self) -> List[Dict[str, Any]]:
        """List all branches sorted by name, marking the active branch."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            cursor.execute("SELECT name, head_commit_id, created_at FROM branches ORDER BY name ASC")
            branches = []
            for b in cursor.fetchall():
                branches.append({
                    "name": b["name"],
                    "head_commit_id": b["head_commit_id"],
                    "created_at": b["created_at"],
                    "active": (b["name"] == active_branch),
                })
            return branches

    def delete_branch(self, name: str) -> Dict[str, Any]:
        """Delete a branch by name. Refuses to delete the currently active branch."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            if name == active_branch:
                return {"success": False, "error": "Cannot delete active branch"}
            cursor.execute("SELECT name FROM branches WHERE name = ?", (name,))
            if not cursor.fetchone():
                return {"success": False, "error": f"Branch '{name}' does not exist"}
            conn.execute("DELETE FROM branches WHERE name = ?", (name,))
        return {"success": True, "branch": name}

    def _get_file_map_at_commit(self, target_commit_id: Optional[str]) -> Dict[str, str]:
        """Return {file_path: sha} representing the exact workspace state at target_commit_id."""
        if not target_commit_id:
            return {}

        file_map: Dict[str, str] = {}
        visited_files = set()
        curr_id: Optional[str] = target_commit_id

        while curr_id:
            files = self.get_commit_files(curr_id)
            for f in files:
                path = f["file_path"]
                if path not in visited_files:
                    visited_files.add(path)
                    if f["after_sha"] is not None:
                        file_map[path] = f["after_sha"]
            commit = self.get_commit(curr_id)
            curr_id = commit.get("parent_id") if commit else None

        return file_map

    def _is_workspace_clean(self) -> Tuple[bool, Optional[str]]:
        """Verify that workspace files on disk match the expected state at current HEAD."""
        current_head = self.get_head_commit_id()
        expected_map = self._get_file_map_at_commit(current_head)
        for rel_path, expected_sha in expected_map.items():
            target = self.workspace_root / rel_path
            if not target.exists():
                return False, f"File '{rel_path}' has uncommitted dirty changes (missing on disk)"
            try:
                curr_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError as e:
                return False, f"File '{rel_path}' cannot be read: {e}"
            if curr_sha != expected_sha:
                return False, f"File '{rel_path}' has uncommitted dirty changes"
        return True, None

    def _apply_file_map(self, target_map: Dict[str, str]) -> None:
        """Atomically transition disk files and index to target_map {file_path: sha}."""
        current_head = self.get_head_commit_id()
        current_map = self._get_file_map_at_commit(current_head)

        files_to_delete = [p for p in current_map if p not in target_map]
        files_to_write: List[Tuple[str, str]] = []
        for rel_path, target_sha in target_map.items():
            target_file = self.workspace_root / rel_path
            if not target_file.exists():
                files_to_write.append((rel_path, target_sha))
            else:
                try:
                    curr_sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
                except OSError:
                    curr_sha = ""
                if curr_sha != target_sha:
                    files_to_write.append((rel_path, target_sha))

        # 1. Suppress fs watcher events
        for rel_path in files_to_delete:
            target_file = self.workspace_root / rel_path
            if self.suppression:
                self.suppression.suppress(target_file)
        for rel_path, _ in files_to_write:
            target_file = self.workspace_root / rel_path
            if self.suppression:
                self.suppression.suppress(target_file)

        # 2. Delete superfluous files
        for rel_path in files_to_delete:
            target_file = self.workspace_root / rel_path
            if target_file.exists():
                try:
                    target_file.unlink()
                except OSError:
                    pass
            if self.indexer is not None:
                try:
                    self.indexer.remove_file(target_file)
                except Exception:
                    pass

        # 3. Write target files
        for rel_path, target_sha in files_to_write:
            target_file = self.workspace_root / rel_path
            content_bytes = self.read_blob(target_sha)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_name = f".tmp_apply_{uuid.uuid4().hex}"
            tmp_path = target_file.parent / tmp_name
            try:
                tmp_path.write_bytes(content_bytes)
                os.replace(tmp_path, target_file)
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
            if self.indexer is not None:
                try:
                    self.indexer.index_file(target_file)
                except Exception:
                    pass

    def checkout_commit(self, commit_id: str) -> Dict[str, Any]:
        """Move workspace state and HEAD to a past commit."""
        target_commit = self.get_commit(commit_id)
        if not target_commit:
            return {"success": False, "error": f"Commit '{commit_id}' not found"}

        is_clean, err_msg = self._is_workspace_clean()
        if not is_clean:
            return {"success": False, "error": err_msg or "Workspace is dirty"}

        target_map = self._get_file_map_at_commit(commit_id)
        self._apply_file_map(target_map)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM history_meta WHERE key = 'active_branch'")
            row = cursor.fetchone()
            active_branch = row["value"] if row and row["value"] else "main"
            conn.execute(
                "UPDATE branches SET head_commit_id = ? WHERE name = ?",
                (commit_id, active_branch),
            )
            conn.execute(
                "INSERT INTO history_meta (key, value) VALUES ('head', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (commit_id,),
            )

        return {"success": True, "head_commit_id": commit_id}

    def switch_branch(self, name: str) -> Dict[str, Any]:
        """Switch active branch and swap workspace files to branch HEAD."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT head_commit_id FROM branches WHERE name = ?", (name,))
            b_row = cursor.fetchone()
            if not b_row:
                return {"success": False, "error": f"Branch '{name}' does not exist"}
            target_head = b_row["head_commit_id"]

        is_clean, err_msg = self._is_workspace_clean()
        if not is_clean:
            return {"success": False, "error": err_msg or "Workspace is dirty"}

        target_map = self._get_file_map_at_commit(target_head)
        self._apply_file_map(target_map)

        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO history_meta (key, value) VALUES ('active_branch', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (name,),
            )
            if target_head:
                conn.execute(
                    "INSERT INTO history_meta (key, value) VALUES ('head', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (target_head,),
                )
            else:
                conn.execute("DELETE FROM history_meta WHERE key = 'head'")

        return {"success": True, "branch": name, "head_commit_id": target_head}
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

repo_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(repo_root / "src"))
sys.path.insert(0, str(repo_root))

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer

tmp_a = Path(tempfile.mkdtemp(prefix="witness_a_"))
def run_measurement():
    tmp_a = Path(tempfile.mkdtemp(prefix="witness_a_"))
    tmp_b = Path(tempfile.mkdtemp(prefix="witness_b_"))
    try:
        (tmp_a / "src").mkdir(parents=True)
        (tmp_a / "src" / "sample.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        (tmp_b / "src").mkdir(parents=True)
        (tmp_b / "src" / "sample.py").write_text("def foo():\n    pass\n", encoding="utf-8")

        # Scénario (a) : runner.py avec read_block
        cmd = [
            sys.executable,
            str(repo_root / "packages/dsh-tool-scalpel/lib/runner.py"),
            str(tmp_a),
            "read_block",
            json.dumps({"path": "src/sample.py"}),
        ]
        res_a = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(repo_root / "src")),
        )

        # Scénario (b) : server.initialize(enable_watcher=False)
        cfg_b = ServerConfig.from_env(workspace_arg=str(tmp_b))
        server_b = ASTGraphServer(cfg_b)
        server_b.initialize(enable_watcher=False)

        def inspect_db(db_path):
            if not db_path.exists():
                return "DB not found"
            conn = sqlite3.connect(str(db_path))
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            summary = {"tables": tables}
            for t in tables:
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
                rows = conn.execute(f"SELECT * FROM {t}").fetchall()
                summary[t] = {"cols": cols, "count": len(rows), "rows": rows}
            conn.close()
            return summary

        db_a = tmp_a / ".agent_graph.db"
        db_b = tmp_b / ".agent_graph.db"

        # Mesure de la vraie base de l'utilisateur
        live_db = repo_root / ".agent_graph.db"
        live_stats = {}
        if live_db.exists():
            conn = sqlite3.connect(str(live_db))
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM symbols WHERE file_path LIKE 'C:/%' OR file_path LIKE 'c:/%'")
            abs_sym = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM symbols WHERE NOT (file_path LIKE 'C:/%' OR file_path LIKE 'c:/%')")
            rel_sym = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM indexed_files WHERE file_path LIKE 'C:/%' OR file_path LIKE 'c:/%'")
            abs_files = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM indexed_files WHERE NOT (file_path LIKE 'C:/%' OR file_path LIKE 'c:/%')")
            rel_files = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM search_index WHERE file_path LIKE 'C:/%' OR file_path LIKE 'c:/%'")
            abs_search = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM search_index WHERE NOT (file_path LIKE 'C:/%' OR file_path LIKE 'c:/%')")
            rel_search = c.fetchone()[0]
            c.execute("SELECT file_path FROM indexed_files WHERE file_path LIKE 'C:/%' LIMIT 10")
            sample_abs = [r[0] for r in c.fetchall()]

            # Recherchons les vrais doublons (même fichier présent en absolu et en relatif)
            c.execute("SELECT DISTINCT file_path FROM indexed_files")
            all_files = [r[0] for r in c.fetchall()]
            rel_set = {f for f in all_files if not (f.startswith('C:/') or f.startswith('c:/'))}
            abs_set = {f for f in all_files if f.startswith('C:/') or f.startswith('c:/')}
            matching_pairs = []
            for a in abs_set:
                for r in rel_set:
                    if a.endswith('/' + r) or a.endswith(r):
                        matching_pairs.append({'abs': a, 'rel': r})

            c.execute("SELECT MIN(indexed_at), MAX(indexed_at), COUNT(*) FROM indexed_files WHERE file_path LIKE 'C:/%'")
            abs_dates = c.fetchone()
            c.execute("SELECT MIN(indexed_at), MAX(indexed_at), COUNT(*) FROM indexed_files WHERE NOT (file_path LIKE 'C:/%')")
            rel_dates = c.fetchone()

            # Regardons qui a inséré les chemins absolus : comment un chemin absolu a pu être passé ?
            # Est-ce que server.initialize() a indexé en absolu un jour ?
            # Vérifions index_meta
            c.execute("SELECT * FROM index_meta")
            meta = c.fetchall()

            live_stats = {
                "symbols_abs": abs_sym,
                "symbols_rel": rel_sym,
                "indexed_files_abs": abs_files,
                "indexed_files_rel": rel_files,
                "search_index_abs": abs_search,
                "search_index_rel": rel_search,
                "abs_dates": abs_dates,
                "rel_dates": rel_dates,
                "index_meta": meta,
                "sample_abs_files": sample_abs,
                "duplicate_files_count": len(matching_pairs),
                "sample_duplicates": matching_pairs[:5]
            }
            conn.close()

        out = {
            "scenario_a_runner": inspect_db(db_a),
            "scenario_b_initialize": inspect_db(db_b),
            "live_database_user": live_stats,
        }
        print(json.dumps(out, indent=2, default=str))
    finally:
        shutil.rmtree(tmp_a, ignore_errors=True)
        shutil.rmtree(tmp_b, ignore_errors=True)

if __name__ == "__main__":
    run_measurement()
    sys.exit(0)

try:
    (tmp_a / "src").mkdir(parents=True)
    (tmp_a / "src" / "sample.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_b / "src").mkdir(parents=True)
    (tmp_b / "src" / "sample.py").write_text("def foo():\n    pass\n", encoding="utf-8")

    # Scénario (a) : runner.py avec read_block
    cmd = [
        sys.executable,
        str(repo_root / "packages/dsh-tool-scalpel/lib/runner.py"),
        str(tmp_a),
        "read_block",
        json.dumps({"path": "src/sample.py"}),
    ]
    res_a = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(repo_root / "src")),
    )

    # Scénario (b) : server.initialize(enable_watcher=False)
    cfg_b = ServerConfig.from_env(workspace_arg=str(tmp_b))
    server_b = ASTGraphServer(cfg_b)
    asyncio.run(server_b.initialize(enable_watcher=False))

    def inspect_db(db_path):
        if not db_path.exists():
            return "DB not found"
        conn = sqlite3.connect(str(db_path))
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        summary = {"tables": tables}
        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            summary[t] = {"cols": cols, "count": len(rows), "rows": rows}
        conn.close()
        return summary

    db_a = tmp_a / ".agent_graph.db"
    db_b = tmp_b / ".agent_graph.db"

    out = {
        "scenario_a_runner": inspect_db(db_a),
        "scenario_b_initialize": inspect_db(db_b),
    }
    print(json.dumps(out, indent=2, default=str))

finally:
    shutil.rmtree(tmp_a, ignore_errors=True)
    shutil.rmtree(tmp_b, ignore_errors=True)
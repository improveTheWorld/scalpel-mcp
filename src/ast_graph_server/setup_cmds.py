"""`init` and `doctor`: install-time commands, kept out of the server's hot path.

`init` writes the MCP client configuration with ABSOLUTE paths (the interpreter that is
running right now and the workspace), because two relative forms failed in practice:
a bare `python` resolved to a different interpreter (the Windows Store stub), and the
`${workspaceFolder}` placeholder was passed literally by the client -- an empty index and
no error.

`doctor` answers "will the server see my project?" without starting it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

SERVER_NAME = "ast-code-graph"
GITIGNORE_LINE = ".agent_graph.db*"


def build_server_entry(workspace: Path) -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "ast_graph_server", "--workspace", str(workspace)],
        "env": {"PYTHONUNBUFFERED": "1"},
    }


def run_init(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="ast-graph-server init")
    parser.add_argument("--workspace", default=".", help="Project to index (default: current directory)")
    parser.add_argument("--force", action="store_true", help=f"Replace an existing '{SERVER_NAME}' entry")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"error: workspace '{workspace}' is not a directory", file=sys.stderr)
        return 2

    config_path = workspace / ".agents" / "mcp_config.json"
    config: dict = {}
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                config = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"error: {config_path} is not valid JSON ({exc}); not touching it", file=sys.stderr)
                return 2
    servers = config.setdefault("mcpServers", {})
    if SERVER_NAME in servers and not args.force:
        print(f"'{SERVER_NAME}' is already configured in {config_path} (use --force to replace it)")
        return 1
    servers[SERVER_NAME] = build_server_entry(workspace)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {config_path}")
    print(f"  interpreter: {sys.executable}")
    print(f"  workspace:   {workspace}")

    gitignore = workspace / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if GITIGNORE_LINE not in existing.splitlines():
        with gitignore.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(GITIGNORE_LINE + "\n")
        print(f"added '{GITIGNORE_LINE}' to {gitignore}")
    print("restart your MCP client, then run: ast-graph-server doctor --workspace", workspace)
    return 0


def run_doctor(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="ast-graph-server doctor")
    parser.add_argument("--workspace", default=".", help="Project to check (default: current directory)")
    args = parser.parse_args(argv)

    problems = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal problems
        if not ok:
            problems += 1
        print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))

    report(sys.version_info >= (3, 10), "python >= 3.10", f"{sys.version.split()[0]} at {sys.executable}")

    workspace = Path(args.workspace).resolve()
    if "${" in args.workspace:
        report(False, "workspace", f"'{args.workspace}' is an unexpanded placeholder")
        return 1
    report(workspace.is_dir(), "workspace exists", str(workspace))
    if not workspace.is_dir():
        return 1

    try:
        from .config import ServerConfig
        from .database import DatabaseManager
        from .indexer import Indexer
    except Exception as exc:  # a missing dependency is exactly what this command reports
        report(False, "server modules import", repr(exc))
        return 1
    report(True, "server modules import")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config = ServerConfig(workspace_root=workspace, db_path=Path(tmp) / "doctor.db")
        indexer = Indexer(config, DatabaseManager(config.db_path, workspace_root=workspace))
        by_ext: dict = {}
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [
                d for d in dirs
                if not indexer.should_ignore_relative((Path(root) / d).relative_to(workspace).as_posix())
            ]
            for name in files:
                rel = (Path(root) / name).relative_to(workspace).as_posix()
                if indexer.should_ignore_relative(rel) or not indexer._is_supported(rel):
                    continue
                ext = Path(name).suffix.lower() or name
                by_ext[ext] = by_ext.get(ext, 0) + 1

    total = sum(by_ext.values())
    summary = ", ".join(f"{ext}:{n}" for ext, n in sorted(by_ext.items(), key=lambda kv: -kv[1])[:8])
    report(total > 0, "indexable files", f"{total} ({summary})" if total else "none found -- wrong workspace?")
    if total > 5000:
        report(False, "workspace size", f"{total} files: is a build/benchmark folder missing from the ignore list?")

    config_path = workspace / ".agents" / "mcp_config.json"
    if config_path.exists():
        try:
            entry = json.loads(config_path.read_text(encoding="utf-8-sig")).get("mcpServers", {}).get(SERVER_NAME)
        except json.JSONDecodeError:
            entry = None
            report(False, "mcp_config.json", "not valid JSON")
        if entry is not None:
            args_list = entry.get("args", [])
            placeholder = any("${" in str(a) for a in args_list)
            report(not placeholder, "mcp_config.json workspace", "contains an unexpanded ${...} placeholder" if placeholder else "")
            command = str(entry.get("command", ""))
            report(
                os.path.isabs(command) and Path(command).exists(),
                "mcp_config.json interpreter",
                command if os.path.isabs(command) else f"'{command}' is not an absolute path (run: ast-graph-server init --force)",
            )
    else:
        print(f"[--] no {config_path} (run: ast-graph-server init)")

    print("healthy" if problems == 0 else f"{problems} problem(s)")
    return 0 if problems == 0 else 1


def dispatch(argv: List[str]) -> Optional[int]:
    """Handle `init` / `doctor`; None means 'not ours, run the server'."""
    if argv and argv[0] == "init":
        return run_init(argv[1:])
    if argv and argv[0] == "doctor":
        return run_doctor(argv[1:])
    return None

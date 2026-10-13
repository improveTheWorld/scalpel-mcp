"""Configuration management for AST Code-Graph MCP Server."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import List


@dataclass
class ServerConfig:
    workspace_root: Path
    db_path: Path
    max_implementation_batch: int = 3
    max_document_bytes: int = 5_000_000
    embedder: str = "none"
    embedding_cache_dir: str | None = None
    context_budget_tokens: int = 8000
    #: How a LISTING is rendered -- the complete list of a block's children read by
    #: read_block: a file's symbols, a folder's entries, a project file's properties.
    #: 'text' (the default) prints the block's own address, then its children indented
    #: under it, each with its kind and its declaration; 'json' is the object tree the
    #: machine contracts are written against. Measured on the benchmark repository:
    #: 2 203 -> 666 characters for SparkConnectOptions.cs, and the text carries what the
    #: envelope never printed (the kind of every member, its signature, its return type).
    #: A single call can override it with read_block(format=...). Nothing else on the
    #: surface is affected: the block TEXT (numbered lines + content hash), the
    #: refusals and the write replies keep their shape in both modes -- so an A/B of the
    #: two renders varies the listing and nothing else.
    answer_format: str = "text"
    ignore_patterns: List[str] = field(
        default_factory=lambda: [
            ".git",
            ".agent_graph.db*",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".venv",
            "venv",
            "env",
            "node_modules",
            "bin",
            "obj",
            "dist",
            "build",
            ".egg-info",
            "*.pyc",
            "*.pyo",
            ".work*",
            ".benchmark-work*",
            "*.zip",
            "reports",
            "reports/*",
            "transcripts",
            "transcripts/*",
            "benchmarks/results",
            "benchmarks/results/*",
            # Secret material: never indexed, so it can never reach a Dossier.
            # Measured on the benchmark repository: DataLinq.Enterprise/.secrets.md
            # and snowflake/secret.md were indexed and listed in the project map,
            # i.e. in the system prompt sent to the model.
            ".env",
            ".env.*",
            ".secrets*",
            "secret*.md",
            "*.pem",
            "*.key",
            "*.pfx",
            "*.p12",
            "id_rsa*",
            "credentials*.yml",
            "credentials*.yaml",
            "credentials*.json",
        ]
    )

    @classmethod
    def from_env(cls, workspace_arg: str | None = None, db_arg: str | None = None) -> "ServerConfig":
        """Instantiate configuration from arguments and environment variables."""
        if workspace_arg and workspace_arg != "${workspaceFolder}":
            ws = Path(workspace_arg).resolve()
        elif "WORKSPACE_FOLDER" in os.environ and os.environ["WORKSPACE_FOLDER"] != "${workspaceFolder}":
            ws = Path(os.environ["WORKSPACE_FOLDER"]).resolve()
        else:
            ws = Path.cwd().resolve()

        if db_arg and db_arg != "${workspaceFolder}":
            db = Path(db_arg).resolve()
        elif "GRAPH_DB_PATH" in os.environ:
            db_env = os.environ["GRAPH_DB_PATH"]
            if "${workspaceFolder}" in db_env:
                db_env = db_env.replace("${workspaceFolder}", str(ws))
            db = Path(db_env).resolve()
        else:
            db = ws / ".agent_graph.db"

        max_batch = int(os.environ.get("MAX_IMPLEMENTATION_BATCH", "3"))

        return cls(
            workspace_root=ws,
            db_path=db,
            max_implementation_batch=max_batch,
            embedder=os.environ.get("AST_GRAPH_EMBEDDER", "none"),
            embedding_cache_dir=os.environ.get("AST_GRAPH_EMBEDDING_CACHE"),
            context_budget_tokens=int(os.environ.get("AST_GRAPH_CONTEXT_BUDGET", "8000")),
            answer_format=os.environ.get("AST_GRAPH_ANSWER_FORMAT", "text"),
        )

    def to_relative_posix(self, path: Path | str) -> str:
        """Convert a path to a workspace-relative POSIX path (with forward slashes)."""
        p = Path(path).resolve()
        try:
            rel = p.relative_to(self.workspace_root)
            return rel.as_posix()
        except ValueError:
            return p.as_posix()

    def to_absolute_path(self, rel_posix: str) -> Path:
        """Convert a workspace-relative path back to an absolute system Path.

        The resolved path is guaranteed to live inside the workspace root: any
        path escaping it (parent traversal, absolute path, drive letter) raises
        ValueError so callers can refuse the operation instead of touching files
        outside the indexed workspace.
        """
        root = self.workspace_root.resolve()
        resolved = (root / rel_posix).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Path '{rel_posix}' resolves outside the workspace root '{root}'."
            ) from exc
        return resolved

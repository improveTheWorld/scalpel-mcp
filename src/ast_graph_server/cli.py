"""Command-line interface for running the AST Code-Graph MCP Server."""

import argparse
import asyncio
import logging
import sys

from .config import ServerConfig
from .server import ASTGraphServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("ast_graph_server")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AST Code-Graph MCP Server for Agentic Coding",
        epilog="Install-time commands: 'init' writes the MCP client config, 'doctor' diagnoses a workspace "
        "(ast-graph-server init|doctor --help).",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Root path of the workspace to index and manage",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database (defaults to <workspace>/.agent_graph.db)",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Disable background file-system watcher",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Index workspace and exit without running MCP server",
    )
    parser.add_argument(
        "--context-budget",
        type=int,
        default=None,
        help="Default token budget for build_context (default: 8000).",
    )
    parser.add_argument(
        "--embedder",
        choices=["none", "minilm", "multilingual"],
        default=None,
        help="Local embedding model for the vector relevance backend (default: none).",
    )
    parser.add_argument(
        "--embedding-cache",
        default=None,
        help="Directory used as the Hugging Face cache for the embedding model.",
    )
    parser.add_argument(
        "--answer-format",
        choices=["json", "text"],
        default=None,
        help=(
            "How read_block renders a LISTING (a file's symbols, a folder's entries, a project "
            "file's properties): 'text' (default) the block's address followed by its children "
            "indented, with each symbol's kind and declaration; 'json' the object tree."
        ),
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Ignore the persisted index and re-parse every file from scratch",
    )
    return parser.parse_args()


def main() -> None:
    from .setup_cmds import dispatch

    handled = dispatch(sys.argv[1:])
    if handled is not None:
        sys.exit(handled)
    args = parse_args()
    config = ServerConfig.from_env(workspace_arg=args.workspace, db_arg=args.db_path)
    if args.embedder:
        config.embedder = args.embedder
    if args.embedding_cache:
        config.embedding_cache_dir = args.embedding_cache
    if args.context_budget:
        config.context_budget_tokens = args.context_budget
    if args.answer_format:
        config.answer_format = args.answer_format
    logger.info("Initializing AST Graph Server on workspace: %s", config.workspace_root)
    logger.info("Embedder: %s", config.embedder)
    logger.info("Using SQLite graph database at: %s", config.db_path)

    server = ASTGraphServer(config)
    indexed_count = server.initialize(
        enable_watcher=not args.no_watch and not args.index_only,
        reindex=args.reindex,
    )
    logger.info("Indexed %d source files into graph database.", indexed_count)
    if indexed_count == 0 and not server.db.get_file_hashes():
        logger.warning(
            "NOTHING is indexed in %s -- wrong --workspace? (a literal '${workspaceFolder}' or an "
            "empty folder both give this). Run: ast-graph-server doctor --workspace <path>",
            config.workspace_root,
        )

    if args.index_only:
        sys.exit(0)

    try:
        asyncio.run(server.run_stdio())
    except KeyboardInterrupt:
        logger.info("Server terminated by user.")
    finally:
        server.watcher.stop()


if __name__ == "__main__":
    main()

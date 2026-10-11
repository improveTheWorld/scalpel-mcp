# Getting Started with Scalpel

Scalpel is a high-performance **Model Context Protocol (MCP)** server that exposes any codebase as a structured, queryable AST (Abstract Syntax Tree) graph.

---

## 1. Prerequisites

- **Python 3.10+** (tested on 3.10, 3.11, 3.12, 3.13)
- Windows, macOS, or Linux

---

## 2. Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/your-org/scalpel-mcp.git
cd scalpel-mcp

# Install in editable mode
pip install -e .
```

---

## 3. Connecting to MCP Clients

Scalpel communicates over standard input/output (`stdio`) using the MCP protocol.

### Configuration for Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "scalpel": {
      "command": "python",
      "args": ["-m", "ast_graph_server.server", "--workspace", "/path/to/your/project"]
    }
  }
}
```

### Configuration for Cursor / Windsurf / DSH

```json
{
  "scalpel": {
    "command": "python",
    "args": [
      "-m", "ast_graph_server.server",
      "--workspace", "${workspaceFolder}"
    ]
  }
}
```

---

## 4. The 8 Core Tools at a Glance

Once connected, your AI coding agent gains access to 8 deterministic surgical tools:

| Tool | Action | Why it beats flat-file tools |
| :--- | :--- | :--- |
| `read_block` | Reads a symbol, a Markdown paragraph, or file outline. | Relative 1-based lines, zero noise, returns `content_hash`. |
| `edit_block` | Surgical line operations (`replace`, `delete`, `insert_after`). | Protected by `expected_hash` & syntax barrier. No line drift. |
| `insert_block` | Adds a method to a class, a Markdown section, or a file. | Automatically matches AST indentation depth. |
| `delete_block` | Removes a single AST symbol or Markdown section. | Surgical removal without leaving orphan braces. |
| `search_symbols` | Finds classes, methods, or functions. | Inlines signatures, callers, and docstrings in a single turn. |
| `search_documents` | Semantic and term-by-term doc search. | Inlines the best matching paragraphs directly. |
| `get_project_map` | Budgeted, balanced overview of the project. | Prevents deep subtrees from starving project awareness. |
| `build_context` | Token-budgeted relevant extraction. | Scores symbols and paragraphs for any complex task. |

---

## 5. Built-in Safety & Time-Travel

Scalpel includes a native, git-independent **Delta History Engine**:
- **2-Phase Commits**: Every write is staged with before/after SHA-256 blobs and validated for syntax before replacing disk files.
- **Rollback & Revert**: Undo mistakes with zero dependency on the host's Git repository.
- **Branching**: Experiment in isolated internal branches with instant AST re-indexing.

# Scalpel

> **The surgical AST engine for AI coding agents.**

*Replace blind line-diffs and context-draining file rewrites with deterministic, content-hashed syntax operations and atomic time-travel.*

[![Tests](https://img.shields.io/badge/tests-975%20passed-brightgreen.svg)]()
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)]()
[![Protocol](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-orange.svg)]()
[![Release](https://img.shields.io/badge/release-v0.2-purple.svg)]()

---

## 🎯 Why Scalpel?

Traditional AI coding assistants interact with codebases like text editors from the 1970s: reading entire 1,000-line files, attempting fuzzy unified diffs, or guessing line numbers. This leads to **three fatal defects**:
1. **Blind Collisions & Line Drift**: Inserting 5 lines at the top of a file invalidates every subsequent line number, leading to hallucinations and corrupted files.
2. **Context Window Starvation**: Re-reading whole files consumes thousands of wasted tokens on every turn.
3. **Broken Builds & Syntax Corruption**: Flat-file replace tools cannot tell if an edit leaves an unclosed brace or misaligns class indentation.

**Scalpel solves this by turning any repository into a queryable AST Graph.**

---

## ⚡ The 4 Pillars

- 🎯 **Surgical Precision**: Address code by semantic identity (`module.py::ClassName::method_name`). Line numbers are 1-based and relative to that symbol only.
- 🛡️ **Zero Blind Collisions (`content_hash`)**: Every block returns an optimistic version stamp. Any concurrent change or stale read is refused cleanly before a single byte moves.
- 📉 **40% to 70% Token Savings**: Inspect complete AST outlines without source bytes; retrieve contracts and signatures in a single turn.
- ⏳ **Native Time-Travel & Delta History**: Built-in, git-independent 2-Phase Commit (2PC) engine with Content-Addressable Storage (CAS SHA-256), non-head 3-way inverse revert, and branch switching.

---

## 🛠️ The 8 MCP Tools

| Tool | Action & Contract |
| :--- | :--- |
| **`read_block`** | Returns a file's complete AST symbol tree, or the numbered source and `content_hash` of a specific block. |
| **`edit_block`** | Atomic line operations (`replace [a,b]`, `delete [a,b]`, `insert_after`) guarded by `expected_hash` and syntax validation. |
| **`insert_block`** | Inserts a new method into a class, a section into a document, or creates a file/folder. Matches AST depth automatically. |
| **`delete_block`** | Removes one symbol or Markdown section without disturbing surrounding code. |
| **`search_symbols`** | Finds classes and methods across workspace and NuGet/pip packages, with inline signature/callers view. |
| **`search_documents`** | Term-by-term search across Markdown documentation with automatic inlining of best matches. |
| **`get_project_map`** | Token-budgeted overview of the repository balanced round-robin across directories. |
| **`build_context`** | Extracts relevance-ranked, token-budgeted project context for complex multi-step tasks. |

---

## 🚀 Quick Start

### 1. Installation

```bash
git clone https://github.com/your-org/scalpel-mcp.git
cd scalpel-mcp
pip install -e .
```

### 2. Configure Your AI Agent (Claude Desktop, Cursor, Roo, DSH)

Add to your MCP configuration file:

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

---

## 📚 Documentation

- 🚀 **[Getting Started Guide](docs/GETTING_STARTED.md)** : Installation, client configuration, and quick walk-through.
- 📖 **[Tools Reference](docs/INTERFACE_OUTILS.md)** : Comprehensive specifications, parameter contracts, and JSON envelopes.
- 🏗️ **[Technical Architecture](docs/TECHNICAL_DOCUMENTATION.md)** : AST graph pipelines, SQLite WAL indexing, and lock mechanics.
- ⏳ **[Delta History & Time-Travel](docs/HISTORIQUE_DELTA.md)** : The 2PC transactional engine, CAS blobs, and rollback algorithms.
- 🧪 **[Benchmark & Research Lab](benchmarks/)** : The scientific evaluation harness, statistical power analysis, and methodology.

---

## 📄 License

MIT License. Designed for high-reliability autonomous software engineering.

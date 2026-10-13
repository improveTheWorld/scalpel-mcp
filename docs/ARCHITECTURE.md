# Scalpel Architecture

Scalpel is an embedded Language Model context server that exposes local codebases as a queryable Abstract Syntax Tree (AST) graph through the **Model Context Protocol (MCP)** over `stdio`.

By operating on granular syntax nodes (methods, classes, functions, and docstrings) rather than flat files, Scalpel decouples AI coding agents from whole-file context dumping and fragile full-text rewrites.

```
┌────────────────────────────────────────────────────────┐
│               AI Coding Agent (Client)                 │
└──────────────────────────┬─────────────────────────────┘
                           │ JSON-RPC 2.0 (stdio)
┌──────────────────────────▼─────────────────────────────┐
│                 MCP Protocol Layer                     │
├────────────────────────────────────────────────────────┤
│  read_block    │  edit_block     │  insert_block       │
│  delete_block  │  search_symbols │  search_documents   │
│  build_context │  get_project_map│                     │
├────────────────────────────────────────────────────────┤
│           AST Graph Engine & Query Router              │
├──────────────────────────┬─────────────────────────────┤
│   Tree-sitter Parsers    │    SQLite WAL Database      │
│  (Py, TS/JS, C#, JSON,   │  (Symbols, Calls, Sections, │
│         YAML)            │      FTS5 Search Index)     │
├──────────────────────────┴─────────────────────────────┤
│           Surgical AST Patching Engine                 │
├────────────────────────────────────────────────────────┤
│  Indentation Rebaser     │  Syntax Barrier (Pre-check) │
│  Byte-Range Slicer       │  OS Lock Orchestrator       │
└──────────────────────────┬─────────────────────────────┘
                           │ Atomic I/O (.tmp -> os.replace)
┌──────────────────────────▼─────────────────────────────┐
│               Local Source Workspace                   │
└────────────────────────────────────────────────────────┘
```

---

## 1. Tree-sitter AST Engine

Scalpel executes language grammars via native C Tree-sitter bindings, constructing Concrete Syntax Trees (CST) with high throughput and low memory overhead.

### 1.1 Supported Grammars & File Types

| Grammar | Extensions | Key Extracted Nodes |
| :--- | :--- | :--- |
| **Python** (`>=0.25.0`) | `.py`, `.pyi` | Classes, functions, async methods, decorated definitions, docstrings, call expressions (`self.x`, `obj.method`). |
| **TypeScript / JS** (`>=0.23.2`) | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` | Interfaces, classes, methods, exported functions, arrow functions, JSDoc blocks, overloads, import statements. |
| **C#** (`>=0.23.0`) | `.cs` | Namespaces, classes, structs, records, enums, interfaces, methods, constructors, properties, XML docstrings (`///`), `using` blocks. |
| **Data Formats** | `.json`, `.yaml`, `.yml` | Key-value hierarchies, sequences, and mappings with structured child node navigation. |
| **Documentation** | `.md`, `.docx`, `.pdf` | Structured sections, headings (#, ##), paragraphs, and preamble text. |

### 1.2 Syntax Node Extraction Invariants
1. **Decorators & Leading Comments**: A symbol's `start_byte` boundary encloses preceding decorators (e.g., `@dataclass`, `[Authorize]`) and initial indentation, guaranteeing that sliced code can be relocated or deleted cleanly.
2. **Call Graph Harvesting**: Bodies of executable nodes are traversed to map invocation expressions (`symbol_calls`), capturing target member names, call strings, and resolving links to workspace symbol IDs.
3. **Preamble and Import Nodes**: Files with non-symbol top-level content (such as C# `using` blocks or module-level docstrings) expose them as addressable pseudo-symbols (e.g., `file.cs::using`), granting uniform editability.

---

## 2. Storage & Persistence Engine

Metadata and symbol graphs reside in `.agent_graph.db` at the workspace root.

### 2.1 Relational Schema

```sql
-- Extracted AST symbols
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,               -- e.g. "src/auth.py::AuthService::verify"
    file_path TEXT NOT NULL,           -- Canonical relative POSIX path
    parent_id TEXT,                    -- Enclosing symbol ID or NULL
    name TEXT NOT NULL,                -- Identifier ("verify")
    kind TEXT NOT NULL,                -- 'function' | 'method' | 'class' | 'interface' ...
    signature TEXT NOT NULL,           -- Normalized declaration signature
    docstring TEXT,                    -- Extracted docstring text
    start_byte INTEGER NOT NULL,       -- UTF-8 start byte offset
    end_byte INTEGER NOT NULL,         -- UTF-8 end byte offset
    start_line INTEGER NOT NULL,       -- 1-based start line
    end_line INTEGER NOT NULL,         -- 1-based end line
    content_hash TEXT NOT NULL,        -- SHA-256 fingerprint of node source
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Inter-symbol dependency graph
CREATE TABLE IF NOT EXISTS symbol_calls (
    caller_id TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    call_expr TEXT NOT NULL,
    callee_symbol_id TEXT,
    FOREIGN KEY(caller_id) REFERENCES symbols(id) ON DELETE CASCADE
);

-- Indexed files cache (guards against redundant parsing on startup)
CREATE TABLE IF NOT EXISTS indexed_files (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Full-text search index for documents and symbol docstrings
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, file_path UNINDEXED, title, body,
    tokenize = 'unicode61'
);
```

### 2.2 Concurrency & WAL Protocol
* **Write-Ahead Logging**: SQLite is initialized with `PRAGMA journal_mode = WAL` and `PRAGMA busy_timeout = 5000`. Readers never block writers, and writers never block readers.
* **Incremental Startup**: Startup routines inspect `indexed_files`. Files with unchanged disk hashes are skipped, completing workspace readiness in milliseconds.

---

## 3. Concurrency, Locks & Isolation

Scalpel guarantees thread-safe and multi-agent concurrency through a multi-tier lock model.

```
[Agent Mutation Request]
           │
           ▼
1. Optimistic Hash Check ────► Reject if block content_hash != expected_hash
           │
           ▼
2. Alphabetical OS Filelock ─► Acquire locks in normalized path order [a, b, c]
           │
           ▼
3. In-Memory Patch & AST Guard ─► Verify CST has no ERROR / MISSING nodes
           │
           ▼
4. Atomic Disk Replacement ──► Write temporary buffer -> os.replace() -> Release lock
```

### 3.1 Multi-File Lock Ordering (`filelock.py`)
To prevent deadlocks when an agent modifies multiple files in a single turn (`edits=[...]`), file locks are acquired strictly in **lexicographical order** of normalized paths. If any lock acquisition times out, all held locks are released, and the transaction is aborted.

### 3.2 Optimistic Concurrency Control
Every block carries a SHA-256 `content_hash`. Mutation operations accept an `expected_hash`. If a concurrent turn altered the target block since the agent read it, the mutation is rejected with `Stale read`, returning the current numbered block and new hash.

---

## 4. Surgical AST Patching Pipeline

Scalpel modifies source code through byte-range replacement, prohibiting AST re-generation (`ast.unparse`).

### 4.1 Indentation Re-basing (`IndentationManager`)
* **Spacing Detection**: The source file is inspected to infer indentation style (tabs vs. 2, 4, or 8 spaces).
* **Target Alignment**: When an agent inserts code at column 0 (a frequent LLM failure mode), Scalpel computes the expected nesting column from parent AST nodes and shifts the payload accordingly.
* **String Literal Protection**: Multi-line string literals and template strings (in Python and TypeScript) are protected: their internal formatting is left unaltered during indentation re-basing.

### 4.2 Complete-File Syntax Barrier
Before any payload touches the physical disk:
1. The proposed modification is applied in memory: `file_bytes[:start] + new_bytes + file_bytes[end:]`.
2. The entire reconstructed file is parsed through Tree-sitter.
3. If the resulting CST contains `ERROR` or `MISSING` nodes, the transaction is rejected. The disk remains pristine.

### 4.3 Atomic File I/O
Writes are serialized to `.tmp_<uuid>` files within the target directory before executing an atomic `os.replace` rename. This guarantees zero file truncation or corruption under unexpected power loss or process kill.

---

## 5. Context Projection Engine

The context projection subsystem (`ContextBuilder` and `relevance.py`) produces structured context packages tuned to LLM context windows:

1. **Relevance Scoring**: Combines BM25 lexical relevance against `search_index` with optional dense vector embeddings (via reciprocal rank fusion).
2. **Tiered Expansion**: Elements expand dynamically across four detail levels:
   - **Level 0**: Name and file path.
   - **Level 1**: Declaration signature, type, or section heading.
   - **Level 2**: Docstring, parameter list, or section hierarchy.
   - **Level 3**: Complete source implementation or section text.
3. **Breadth-First Budgeting**: Allocates a configurable portion of the token allowance (`breadth_ratio`, default 0.4) to enumerate many candidate symbols before deepening high-relevance items to Level 3.

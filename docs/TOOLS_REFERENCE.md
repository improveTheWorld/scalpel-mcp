# Tools Reference

Scalpel exposes the codebase as a queryable, mutable Abstract Syntax Tree (AST) graph over the Model Context Protocol (MCP). Rather than exchanging raw, monolithic files, language models query and modify individual syntax nodes—functions, methods, classes, and document sections.

The server registers **8 core MCP tools**:

| Category | Tool | Primary Purpose |
| :--- | :--- | :--- |
| **Inspection** | `read_block` | Read symbol/paragraph content with line numbers, or list container children. |
| **Mutation** | `edit_block` | Apply atomic line-based operations (`replace`, `delete`, `insert_after`) to a block. |
| | `insert_block` | Insert a new symbol, document section, or file under a parent container. |
| | `delete_block` | Remove a symbol or document section; re-parent orphan children. |
| **Search & Discovery** | `search_symbols` | Query symbols across workspace and external dependencies with optional inline source. |
| | `search_documents` | Full-text search over indexed markdown/office documents, or enumerate files. |
| **Context Projection** | `build_context` | Generate a token-budgeted, relevance-ranked context dossier for a specific query. |
| | `get_project_map` | Retrieve an up-to-date, budgeted structural map of project files and top-level symbols. |

---

## 1. Unified Addressing Scheme

Every target in Scalpel is addressed by a canonical, case-folded path using the `::` delimiter:

```
directory/file.cs                         # File or directory container
file.cs::Namespace::Class::Method         # Code symbol
document.md::Parent Section::Paragraph    # Document section / paragraph
file.cs::using                            # C# using directives block
```

* **Symbol resolution**: Unique short names resolve automatically (e.g., `Method` resolves to `file.cs::Class::Method` if unambiguous within the file). If ambiguous, the server rejects the call and enumerates all candidates.
* **Normalization**: Case, whitespace, accents, and punctuation are normalized during lookup.
* **Safety**: Bare file paths do not open code blocks; attempting to read or edit code using a raw file path returns the available symbols within that file.

---

## 2. Tool Specifications

### 2.1 `read_block`

Reads the content of an individual block or lists the children of a container.

#### Parameters
* `path` *(string, optional)*: Target block or container path.
* `paths` *(array of strings, optional)*: Batch query of multiple symbol or paragraph paths.
* `format` *(string, default: `"text"`)*: Output rendering mode (`"text"` or `"json"`).

#### Behavior
1. **Container paths** (directory, code file, document root): Returns the complete, unpaginated listing of immediate children with their declaration, line span, and byte size.
2. **Block paths** (symbol or paragraph): Returns a 1-line header (`address [kind] [h:content_hash] line_count lines`) followed by 1-based line-numbered text with original indentation preserved.
3. **Batch queries (`paths=[...]`)**: Returns the numbered text and content hash for each block in a single round trip, avoiding sequential $N+1$ turn latency.

#### Example Output (`format="text"`)
```text
src/billing.py::BillingService::charge [method] [h:7a8b9c...] 6 lines
1:     def charge(self, user_id: str, amount: float) -> Receipt:
2:         """Execute credit card charge."""
3:         self.validate_user(user_id)
4:         receipt = self.gateway.post_charge(user_id, amount)
5:         self.audit_log.record(receipt)
6:         return receipt
```

---

### 2.2 `edit_block`

Applies surgical, line-based operations to an existing block without invoking full-file AST re-serialization (`ast.unparse` is prohibited).

#### Parameters
* `path` *(string, optional)*: Symbol or paragraph address (required for single-block edits).
* `ops` *(array of objects, optional)*: Ordered list of line operations.
* `expected_hash` *(string, optional)*: Optimistic locking version stamp obtained from `read_block` or `search_symbols`.
* `edits` *(array of objects, optional)*: Batch operations over multiple blocks: `[{path, ops, expected_hash}, ...]`.

#### Operations Grammar
* `{"replace": [a, b], "content": "..."}`: Replaces lines `a` through `b` inclusive. Content must not duplicate lines `a-1` or `b+1`.
* `{"delete": [a, b]}`: Deletes lines `a` through `b` inclusive.
* `{"insert_after": n, "content": "..."}`: Inserts content immediately after line `n` (`n=0` inserts at the beginning of the block).

#### Guarantees and Rules
* **Bottom-Up Application**: Operations are applied in descending line order so line numbers remain stable during execution.
* **Markdown Line Coordinates & `insert_after: 0` vs `1`**: In Markdown, **line 1 is the heading** (`# Heading`). `insert_after: 0` lands *before* line 1 (before the title), which places the content above the section. To insert content at the very start of the section's body (under its title), use `insert_after: 1`.
* **Outline Invariance & Sibling Sections**: `edit_block` strictly modifies *inside* the block. Inserting a heading of equal or higher hierarchy is refused; to add sibling sections or paragraphs, use `insert_block(parent, anchor)` directly.
* **Sibling Creation in Code**: Appending content after the final line of a code block writes outside the block as a sibling declaration.
* **Inclusive Replace Ranges**: `replace: [a, b]` consumes every line from `a` to `b` inclusive. Ensure adjacent bullets or lines intended to be kept are not included in `[a, b]`.
* **Batch Atomicity**: A syntax validation failure on any entry in `edits=[...]` rejects the entire batch; no file writes occur, and existing hashes remain valid.

---

### 2.3 `insert_block`

Creates a new child block under an existing container.

#### Parameters
* `parent` *(string, required)*: Container path (type container, document, paragraph, or directory).
* `anchor` *(string, optional)*: Sibling child identifier after which the new block is inserted. If omitted, inserts as the first child (or at the end of class body for type containers).
* `text` *(string, required)*: Content to insert.
  - **Code container parent**: Source code of the function, method, property, or nested class.
  - **Document parent**: First line is the heading; subsequent lines form the body.
  - **Directory parent**: First line is the filename (or directory name if ending with `/`); remaining lines provide initial content.

#### Indentation & Level Inference
* Code indentation is computed automatically using the parent container's indentation and the file's detected spacing step (spaces/tabs).
* Document heading levels are inferred directly from the parent ($level_{parent} + 1$). Explicit `#` prefixes are normalized.

---

### 2.4 `delete_block`

Deletes a code symbol or document section. Whole files and directories cannot be removed through this tool.

#### Parameters
* `path` *(string, required)*: Canonical address of the block to delete.
* `expected_heading` *(string, optional)*: Markdown guard verifying the heading matches expected text before executing deletion.

#### Behavior
* **Code symbols**: Deletes the node, decorators, docstring, and all enclosed child members.
* **Document sections**: Deletes the heading and body text. Any descendant subsections are elevated to the parent section, preserving document hierarchy.

---

### 2.5 `search_symbols`

Performs regex or substring search across AST symbols in the workspace and referenced external package dependencies.

#### Parameters
* `query` *(string, required)*: Symbol name or substring to match.
* `kind` *(string, default: `"all"`)*: Filter by symbol type (`function`, `method`, `class`, `interface`, `namespace`, `struct`, `record`, `enum`, `property`, `constructor`, `destructor`, `operator`, `all`).
* `path` *(string, optional)*: Glob pattern restricting search paths (e.g., `src/**/*.py`).
* `limit` *(integer, default: `50`)*: Maximum results returned.
* `view` *(string, default: `"none"`)*: Detail level for matching symbols:
  - `none`: Compact 1-line signature and location.
  - `contract`: Signature, docstring, callers, and call expressions.
  - `implementation`: Full source code with 1-based line numbers and `content_hash` ready for direct `edit_block` consumption.

---

### 2.6 `search_documents`

Searches document text or enumerates indexed files.

#### Parameters
* `query` *(string, default: `""`)*: Term-by-term full-text search string (FTS5 BM25). An empty query (`query=""`) returns the complete catalogue of indexed documents.
* `path` *(string, optional)*: Glob filter for document paths.
* `limit` *(integer, default: `8`)*: Maximum matching sections returned.
* `full_top` *(integer, default: `2`)*: Number of highest-ranked results inlined with complete content.
* `include_content` *(boolean, default: `false`)*: When `true`, inlines full text for every match.

---

### 2.7 `build_context`

Generates a relevance-ranked, token-budgeted project context dossier for a task query using breadth-first enumeration followed by depth ranking.

#### Parameters
* `query` *(string, required)*: Task description, keywords, or error message.
* `budget_tokens` *(integer, default: `8000`)*: Maximum token budget for the returned payload.
* `max_level` *(integer, default: `3`)*: Maximum depth detail (0: name, 1: signature/title, 2: docstring, 3: implementation/body).
* `breadth_ratio` *(number, default: `0.4`)*: Fraction of token budget reserved for listing multiple elements before expanding top items.
* `map` *(string, default: `"none"`)*: Structural map attached at the top of context (`none`, `names`, `structure`).

---

### 2.8 `get_project_map`

Generates a budgeted overview of the workspace structure.

#### Parameters
* `budget_tokens` *(integer, default: `8000`)*: Maximum token allowance.
* `path` *(string, optional)*: Glob pattern restricting the mapped tree.
* `format` *(string, default: `"text"`)*: Compact tree text or structured JSON.

---

## 3. Error Handling and Actionable Refusals

When a tool rejects an operation, the response delivers an actionable payload designed for automated agent recovery:

```json
{
  "status": "error",
  "error": "Stale read: block content has changed since hash 3a9f... was issued.",
  "actionable_hint": "Re-read the block using read_block('src/billing.py::charge') to obtain fresh line numbers and content_hash.",
  "path": "src/billing.py::charge"
}
```

### Common Refusals and Recovery Paths
1. **Stale Hash (`expected_hash` mismatch)**: Block modified by a concurrent turn. The server returns the fresh numbered block and new hash immediately.
2. **Boundary Overlap**: Line operations overlap or re-emit unconsumed adjacent lines. Merge adjacent edits into a single `replace` range.
3. **Syntax Barrier Failure**: Edited or inserted code violates language grammar (syntax error or open bracket). The disk remains unmodified.

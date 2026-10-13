# Atomic Delta History & Time-Travel Engine

Scalpel incorporates an embedded, content-addressed version control and time-travel engine. Designed to provide transactional safety during autonomous agent refactoring sessions, it guarantees deterministic crash recovery, non-head reverts via 3-way inverse merges, and instant workspace branch switching without relying on or interfering with the host project's Git repository.

---

## 1. System Architecture

All versioning artifacts are physically isolated within the workspace under `.scalpel/history/`, completely decoupled from the transient AST graph database:

```
<workspace_root>/
  ├── .agent_graph.db          # Ephemeral AST graph & FTS5 search index (reconstructible)
  └── .scalpel/
      └── history/             # Persistent transactional versioning engine
          ├── history.db       # SQLite WAL database (commits, file maps, branches)
          └── blobs/           # Content-Addressed Storage (CAS) keyed by SHA-256
              ├── 3a/
              │   └── 3ab4f2...# Immutable raw byte payload
              └── e7/
                  └── e78c90...
```

### Core Design Principles
1. **Byte-Exact Immutability**: Historical snapshots preserve full physical file contents in Content-Addressed Storage (CAS), eliminating dependency on fragile or lossy text diffs.
2. **Physical Isolation**: The AST graph database (`.agent_graph.db`) can be deleted or rebuilt at any time without impacting history or corrupting tracked snapshots.
3. **Zero Host Git Intrusion**: The `.scalpel/` hierarchy is excluded from host version control, ensuring agent micro-commits do not pollute developer commit logs.

---

## 2. Two-Phase Commit (2PC) & Crash Resilience

To prevent inconsistent states caused by unexpected process termination during file writes or database transactions, Scalpel employs a two-phase commit protocol.

### 2.1 Commit Lifecycle

```
[Write Initiated]
        │
        ▼
1. Save CAS Blobs ────────► Write immutable bytes to .scalpel/history/blobs/xx/
        │
        ▼
2. Phase PENDING (DB) ────► Register transaction in history.db with status = 'pending'
        │
        ▼
3. Disk Mutation ─────────► Suppress watcher -> write .tmp_<uuid> -> os.replace()
        │
        ▼
4. Phase COMMITTED (DB) ──► Update status = 'committed' and advance HEAD pointer
        │
        ▼
[Transaction Settled]
```

1. **Blob Persist**: Source buffers are saved to CAS storage using temporary files and atomic `os.replace` renames.
2. **Pending Transaction**: A record is written to `history.db` with `status = 'pending'`, binding the targeted files, `before_sha`, and `after_sha`.
3. **Physical Write**: The internal file watcher is notified via `suppression.suppress(path)` to prevent echo loops, followed by atomic filesystem replacement.
4. **Commit Finalization**: The transaction is marked `committed`, and the active branch `HEAD` is updated.

### 2.2 Startup Crash Recovery (`recover_pending`)

On engine initialization, Scalpel scans for orphaned `pending` transactions:
* **Automatic Promotion**: If all files listed in the transaction match their respective `after_sha` on disk, the mutation succeeded prior to crash; the transaction is promoted to `committed`.
* **Automatic Rollback / Abort**: If files match their `before_sha`, the transaction failed; the record is marked `aborted`.
* **Divergent State**: If disk content matches neither hash, manual resolution is flagged to prevent silent data corruption.

### 2.3 Head Rollback (`rollback_head`)

Reverts the single most recent commit at the top of the current branch:
* Validates that current workspace files have not been modified since `HEAD`.
* Restores modified files from original `before_sha` CAS blobs.
* Deletes files created by the target commit (`before_sha IS NULL`).
* Re-synchronizes the AST index and moves `HEAD` to the parent commit.

---

## 3. Non-Head Reverts & 3-Way Inverse Merge

The `revert_commit(commit_id)` API reverts an arbitrary historical commit $C_k$ while preserving intermediate modifications committed subsequently on the active branch.

### 3.1 Reconciliation Algorithm

Given historical base commit $A$ (`before_sha`), historical modified commit $B$ (`after_sha`), and current workspace state $C$:

1. **Historical File Creation**: If the file was introduced in $C_k$ and remains unmodified in $C$ ($C == B$), the file is deleted. If modified since ($C \neq B$), the revert is rejected.
2. **Historical File Deletion**: If the file was removed in $C_k$, it is re-materialized from blob $A$. If a file with that path was re-created in $C$, the revert is rejected.
3. **Historical File Modification**:
   - **Trivial Revert ($C == B$)**: The file on disk is bit-identical to $B$. Restores $A$ directly.
   - **Non-Trivial Revert ($C \neq B$)**: Evaluates a **3-way inverse merge** using `difflib.SequenceMatcher` to compute the inverse diff $(B \to A)$ and apply it onto $C$.
   - **Conflict Handling**: Any overlapping or conflicting hunk is treated as a hard conflict. The revert operation is aborted immediately without modifying disk files (strict all-or-nothing guarantee).
4. **Audit Trail**: Successful reverts record a new commit of type `kind = 'revert'` at `HEAD`.

---

## 4. Internal Branches & Time-Travel

### 4.1 Branch Management
* **Branch Table**: `branches(name TEXT PRIMARY KEY, head_commit_id TEXT, created_at REAL NOT NULL)`
* **Active Branch**: Tracked in `history_meta` (defaults to `'main'`).
* **Operations**:
  - `create_branch(name)`: Pins a new branch pointer to current `HEAD`.
  - `list_branches()`: Lists all branches, commit hashes, and marks the active branch.
  - `delete_branch(name)`: Removes a branch reference (active branch cannot be deleted).

### 4.2 Time-Travel Checkout

Scalpel enables seamless workspace state checkout across any arbitrary commit or branch:

```
[Checkout Requested: target_commit]
                 │
                 ▼
1. Verify Workspace ──────► Abort if uncommitted modifications exist (_is_workspace_clean)
                 │
                 ▼
2. Build Target Tree ─────► Traverse parent ancestry to construct {path: sha} file map
                 │
                 ▼
3. Reconcile Files ───────► Atomic replacement from CAS blobs + delete extraneous files
                 │
                 ▼
4. Synchronize AST ───────► Notify watcher suppression and update AST indexer
                 │
                 ▼
[Target Commit Active]
```

* `checkout_commit(commit_id)`: Transitions workspace to any historical snapshot.
* `switch_branch(name)`: Fast-forwards or rewinds workspace files to the head of the selected branch and marks it active.

---

## 5. Security & Guarantee Matrix

| Feature | Mechanism | Invariant / Failure Guarantee |
| :--- | :--- | :--- |
| **Atomic Commits** | Two-Phase Commit (2PC) | No partial writes; startup reconciliation via `recover_pending()`. |
| **Storage Integrity** | SHA-256 CAS Blobs | Write-idempotent, deduplicated, immutable disk blocks. |
| **HEAD Rollback** | Bit-exact restoration | Refuses to execute if workspace diverged from `HEAD`. |
| **Historical Revert** | 3-way inverse merge | All-or-nothing: zero disk modification upon merge conflict. |
| **Time-Travel** | Map reconciliation | Refuses branch switch or checkout on dirty workspace. |

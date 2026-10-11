"""Budgeted projection of the ontology into an LLM context.

Deliberately separate from indexing: the indexer builds and persists the ontology
(symbols, documents, paragraphs and the search index), while this module decides
*what to show and at which level of detail* under a given token budget.

Levels:
  0  name / path only
  1  + kind, signature or title
  2  + docstring or heading level
  3  + full content (implementation or paragraph text)
"""

from dataclasses import dataclass
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import ServerConfig
from .context_config import ContextTieringConfig
from .database import DatabaseManager
from .relevance import RelevanceBackend, build_backend
from .rendering import render_project_tree


@dataclass
class ContextEntry:
    kind: str
    ref_id: str
    level: int
    score: float
    text: str
    #: The stamp of the block whose text is exposed, ONLY when that text is complete: it is the
    #: same pair read_block returns, so the entry can be acted on without a second read.
    content_hash: Optional[str] = None
    line_count: Optional[int] = None


class ContextBuilder:
    """Selects ontology elements and detail levels to fit a token budget."""

    MAX_CONTENT_CHARS = 2000

    # Measured with the model's own tokenizer on this repository's real content
    # (French prose, Markdown tables, C# signatures): ~3.2-3.4 characters per
    # token. The previous value of 4 understated a rendered payload by ~20%,
    # so an "8000 token" budget actually produced up to ~12k tokens on the wire.
    CHARS_PER_TOKEN = 3.3

    #: Default budgets for the agent-facing detail levels: 1 = a quick look,
    #: 5 = deep. The agent asks for a level; the tool owns the token price.
    DETAIL_LEVEL_BUDGETS = {1: 4000, 2: 8000, 3: 16000, 4: 24000, 5: 32000}

    #: The relevance floor is NOT an agent-facing knob any more (08/10/2026). It used to be, twice:
    #: as `min_score` (the prompt dictated 10, which dropped SparkConnectOptions.cs, scored 6.64,
    #: and cost b6_s05 eight turns of blind search) and as a `precision` level {high: 25, normal,
    #: low: 0} (b8_s01/s02 passed 'low' on every technical call, because the description said
    #: "use it to reach the CODE" -- which silently bypassed context_config.json; 'high' would have
    #: dropped almost every match). A model cannot know what a BM25 score means, so the floor is
    #: read from context_config.json and nowhere else: see `configured_floor`.

    #: Map budgets behind the agent-facing map levels, taken from the two campaigns that measured
    #: them: 7 000 tokens carried the tree alone, 32 000 carries the tree PLUS the mini-AST of the
    #: best-ranked files (HANDOFF, section 0nonies).
    MAP_LEVEL_TOKENS = {"names": 7000, "structure": 32000}

    def __init__(
        self,
        config: ServerConfig,
        db: DatabaseManager,
        backend: Optional[RelevanceBackend] = None,
        embedder: Optional[Any] = None,
        tiering_config: Optional[ContextTieringConfig] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.backend = backend or build_backend(db, embedder=embedder)
        # The ADDRESS printers, injected by the server (ASTGraphServer.__init__). None keeps this
        # class usable on its own -- a Dossier rendered without them falls back to the storage ids.
        self.addresses: Optional[Any] = None
        self.scope: str = "subsequent"
        self.tiering_config: Optional[ContextTieringConfig] = tiering_config

    def set_scope(self, scope: str) -> None:
        """Set active scope: 'initial' (Initial Dossier) or 'subsequent' (agent technical context)."""
        self.scope = scope

    def configured_floor(self, scope: Optional[str] = None) -> float:
        """The relevance floor of the active scope, read from context_config.json ONLY.

        This is what a build_context call without an explicit operator `min_score` applies. The
        agent has no say in it: neither `min_score` nor a level is on its surface.
        """
        cfg = self.tiering_config or ContextTieringConfig.load(workspace_root=self.config.workspace_root)
        effective_scope = scope or self.scope
        tiering = cfg.initial if effective_scope == "initial" else cfg.subsequent
        return float(tiering.min_score)


    # ------------------------------------------------------------------
    # Rendering one element at a given level
    # ------------------------------------------------------------------
    # Rendering one element at a given level
    # ------------------------------------------------------------------
    def _bounded_content(self, text: str, max_chars: Optional[int] = None) -> str:
        """Return level-3 content, explicitly marked when the display budget cuts it.

        A Dossier is a ranked selection under a token budget, so bounding one
        entry is legitimate; hiding the cut is not.
        """
        cap = max_chars or self.MAX_CONTENT_CHARS
        if len(text) <= cap:
            return text
        omitted = len(text) - cap
        return text[: cap] + "\n[... " + str(omitted) + " chars omitted from this entry ...]"

    def _symbol_addresses(self, file_path: str) -> Dict[str, str]:
        """{stored symbol id -> printed address}, or {} when the patcher is not wired.

        The Dossier must hand out addresses the tools ACCEPT, in the one grammar the surface prints:
        an entry whose key is a storage id costs the caller a call to rebuild it.
        """
        if self.addresses is None:
            return {}
        return self.addresses.symbol_addresses(file_path)

    def _document_addresses(self, document_id: str) -> Dict[str, str]:
        if self.addresses is None or not document_id:
            return {}
        return self.addresses.document_addresses(document_id)

    def _stamped_entry(
        self, address: str, block_ref: str, max_chars: Optional[int] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """The text of a level-3 entry: the block's OWN numbered lines PLUS its stamp.

        A Dossier entry that exposes content must expose the same pair a read gives -- the numbering
        AND the hash that describes it. Measured on base f2a9649: the section entry showed the body
        only (heading line absent) while read_block numbers the heading as line 1, so a caller
        counting from the Dossier aimed two lines off. With the stamp attached but the numbering
        missing, that call would have PASSED the edit guard and written to other lines.
        When the content does not fit the entry bound the text is marked incomplete and NO hash is
        given: a stamp must never stand for text the caller has not seen.
        """
        metrics = self.addresses.block_metrics(block_ref) if self.addresses is not None else None
        if metrics is None:
            return None
        numbered = str(metrics.get("numbered_text") or "")
        stamp = str(metrics.get("content_hash") or "")
        cap = max_chars or self.MAX_CONTENT_CHARS
        if len(numbered) > cap:
            omitted = len(numbered) - cap
            return (
                address + "\n" + numbered[: cap]
                + "\n[... " + str(omitted) + " chars omitted from this entry: the text is NOT "
                "complete, so no hash is given ...]",
                {},
            )
        return (
            address + "  hash " + stamp + "\n" + numbered,
            {"content_hash": stamp, "line_count": metrics.get("line_count")},
        )

    def _printed_ref(self, ref_id: str) -> str:
        """The address the entry is NAMED by: the printed path, never the storage id."""
        if self.addresses is None:
            return ref_id
        return self.addresses.displayed_id(ref_id)

    def _symbol_text(self, symbol: Any, level: int, max_chars: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
        if level <= 0:
            return symbol.name, {}
        printed = self._symbol_addresses(str(symbol.file_path)).get(symbol.id, str(symbol.id))
        if level >= 3:
            stamped = self._stamped_entry(printed, str(symbol.id), max_chars=max_chars)
            if stamped is not None:
                return stamped
        base = symbol.kind + " " + printed + "  " + symbol.signature
        if level == 1:
            return base, {}
        if symbol.docstring:
            base += " -- " + symbol.docstring.strip().splitlines()[0][:160]
        if level == 2:
            return base, {}
        try:
            code = self.config.to_absolute_path(symbol.file_path).read_bytes()[symbol.start_byte : symbol.end_byte]
            return base + "\n" + self._bounded_content(code.decode("utf-8", errors="replace"), max_chars=max_chars), {}
        except Exception:
            return base, {}

    def _section_text(self, section: Dict[str, Any], level: int, max_chars: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
        # The identifier must be the one `get_document_section` accepts, i.e.
        # `<file_path>#<order_index>` (the row id), not a decorative rendering of
        # the heading. A Dossier whose keys the tools reject forces the agent to
        # pay a full `list_document_sections` call per document just to rebuild
        # them, which measured 23% of a run's tool-result volume.
        raw_id = section.get("section_id") or section.get("id") or section.get("document_id", "")
        document_id = str(section.get("document_id") or "")
        section_id = self._document_addresses(document_id).get(str(raw_id), str(raw_id))
        if level <= 0:
            return section_id, {}
        if level >= 3:
            stamped = self._stamped_entry(section_id, str(raw_id), max_chars=max_chars)
            if stamped is not None:
                return stamped
        base = section_id + " :: " + (section.get("heading") or "")
        if level <= 2:
            return base, {}
        return base + "\n" + self._bounded_content(section.get("content") or "", max_chars=max_chars), {}

    def _document_text(self, document: Dict[str, Any], level: int, max_chars: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
        path = document["file_path"]
        # Each heading is paired with the identifier needed to read that section, so the agent never
        # has to call list_document_sections to rebuild them.
        outline = []
        printed = self._document_addresses(path)
        for item in self.db.get_document_sections(path)[:40]:
            raw_id = str(item.get("id") or item.get("section_id") or "")
            heading = (item.get("heading") or "").strip() or "(untitled)"
            outline.append(printed.get(raw_id, raw_id) + " " + heading)
        if level <= 0:
            return path, {}
        if level >= 3:
            # read_block answers a document with its OWN numbered text plus its stamp, and lists the
            # sections it holds: the entry gives the same three things.
            stamped = self._stamped_entry(path, path, max_chars=max_chars)
            if stamped is not None:
                text, extra = stamped
                return (text + ("\n  " + "\n  ".join(outline) if outline else "")), extra
        base = path + " :: " + (document.get("title") or "")
        if level == 1:
            return base, {}
        base += " (" + str(document.get("section_count", 0)) + " paragraphs)"
        if level == 2:
            return base, {}
        return base + "\n  " + "\n  ".join(outline), {}

    def render(self, kind: str, ref_id: str, level: int, max_chars: Optional[int] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        if kind == "symbol":
            symbol = self.db.get_symbol(ref_id)
            return self._symbol_text(symbol, level, max_chars=max_chars) if symbol else None
        if kind == "section":
            section = self.db.get_document_section(ref_id)
            return self._section_text(section, level, max_chars=max_chars) if section else None
        if kind == "document":
            document = self.db.get_document(ref_id)
            return self._document_text(document, level, max_chars=max_chars) if document else None
        return None

    def _project_map_text(self, budget_chars: int) -> str:
        """A query-independent structural map, rendered from the indexed files."""
        try:
            paths = self.db.get_indexed_paths()
        except Exception:
            return ""
        if not paths:
            return ""
        return render_project_tree(paths, budget_chars=budget_chars)

    def _file_structure(self, file_path: str) -> Optional[str]:
        """One file's mini-AST: the SAME facts read_block returns for that path.

        A code file yields its symbols (id, kind, span), a .md file its sections (id,
        heading, level). A card that only named the files was not telling the arm with the
        card what the other arm discovers by reading them, so the A/B could not say
        anything about the card.
        """
        document = self.db.get_document(file_path)
        if document is not None:
            sections = self.db.get_document_sections(file_path)
            if not sections:
                return None
            printed = self._document_addresses(file_path)
            lines = [file_path + "  (" + str(len(sections)) + " sections)"]
            for item in sections:
                heading = (item.get("heading") or "").strip() or "(untitled)"
                level = max(0, int(item.get("level") or 1))
                raw_id = str(item.get("id") or "")
                lines.append("    " * level + heading + " [" + printed.get(raw_id, raw_id) + "]")
            return "\n".join(lines)
        rows = self.db.get_symbols_for_file(file_path)
        if not rows:
            return None
        printed_symbols = self._symbol_addresses(file_path)
        depth: Dict[str, int] = {}
        for row in rows:
            depth[row["id"]] = depth.get(row.get("parent_id"), -1) + 1
        lines = [file_path + "  (" + str(len(rows)) + " symbols)"]
        for row in rows:
            printed = printed_symbols.get(str(row["id"]), "")
            name = printed.split("::", 1)[-1] if printed else str(row["id"]).split("::", 1)[-1]
            span = "%d-%d" % (row.get("start_line") or 0, row.get("end_line") or 0)
            ref = printed or str(row["id"])
            lines.append("    " * (depth.get(row["id"], 0) + 1) + str(row["kind"]) + " " + name + " :" + span + " [" + ref + "]")
        return "\n".join(lines)

    def _structure_map_text(self, budget_chars: int, query: str, limit: int):
        """The WHOLE tree, then the mini-AST of the most relevant files.

        The tree comes first and stays complete, because a partial tree is not a map (a
        caller must be able to see which files exist). The structures are spent on the
        files the query actually ranks, which is what an agent would have gone and read.
        """
        try:
            paths = self.db.get_indexed_paths()
        except Exception:
            return "", {}
        if not paths:
            return "", {}
        tree = render_project_tree(paths, budget_chars=budget_chars)
        stats: Dict[str, Any] = {"files": 0, "truncated": False}
        remaining = budget_chars - len(tree) - 1
        if remaining <= 0:
            stats["truncated"] = True
            return tree, stats
        ranked: List[str] = []
        seen = set()
        for item in self.backend.search(query, limit=limit):
            ref = str(item.get("ref_id") or "")
            if not ref:
                continue
            if "::" in ref:
                file_path = ref.split("::", 1)[0]
            elif "#" in ref:
                file_path = ref.split("#", 1)[0]
            else:
                file_path = ref
            if file_path not in seen:
                seen.add(file_path)
                ranked.append(file_path)
        parts = ["", "--- mini-AST des fichiers les plus pertinents (memes faits que read_block) ---"]
        used = len(parts[1]) + 1
        for file_path in ranked:
            block = self._file_structure(file_path)
            if block is None:
                continue
            cost = len(block) + 1
            if used + cost > remaining:
                stats["truncated"] = True
                break
            used += cost
            parts.append(block)
            stats["files"] += 1
        if stats["files"] == 0:
            return tree, stats
        if stats["truncated"]:
            parts.append(
                "[mini-AST arrete au budget : " + str(len(ranked) - stats["files"])
                + " fichier(s) pertinent(s) sans structure]"
            )
        return tree + "\n".join(parts), stats

    # ------------------------------------------------------------------
    # Budget packing: breadth first, then progressively deepen
    # ------------------------------------------------------------------
    def build(
        self,
        query: str,
        budget_tokens: int = 8000,
        max_level: int = 3,
        breadth_ratio: float = 0.4,
        limit: int = 200,
        map_ratio: float = 0.0,
        min_score: Optional[float] = None,
        map_tokens: Optional[int] = None,
        map_detail: str = "names",
        max_entries_per_file: Optional[int] = None,
        max_content_chars: Optional[int] = None,
        scope: Optional[str] = None,
        tiering_config: Optional[ContextTieringConfig] = None,
    ) -> Dict[str, Any]:
        # Resolve tiering configuration dynamically (allows live updates from context_config.json)
        cfg = tiering_config or self.tiering_config or ContextTieringConfig.load(workspace_root=self.config.workspace_root)
        effective_scope = scope or self.scope
        tiering = cfg.initial if effective_scope == "initial" else cfg.subsequent

        # Determine effective min_score
        resolved_min_score = min_score
        if resolved_min_score is None and tiering.enabled:
            resolved_min_score = tiering.min_score

        # The budget is expressed in tokens but the packer measures characters,
        # so the conversion factor decides whether the budget is real. At 4 the
        # payload overshot by ~1.5x in tokens; 3.3 is the measured ratio.
        budget_chars = max(400, int(budget_tokens * self.CHARS_PER_TOKEN))
        # An optional project map: a query-independent structural header (folders,
        # sub-folders, file names) placed at the top of the context.
        #
        # Two ways to size it, and they are NOT equivalent:
        #   * map_ratio carves the map out of the section budget (historical form).
        #     An A/B on map_ratio therefore compares the map against a SMALLER
        #     selection, not against none -- measured on 25/09: the arm with the map
        #     had a *shorter* prompt, because the map displaced sections.
        #   * map_tokens gives the map ITS OWN budget: the sections keep the full
        #     budget_tokens. That is the only form in which the arm with the map and
        #     the arm without it differ by the map alone.
        map_text = ""
        map_budget_chars = 0
        map_stats: Dict[str, Any] = {}
        if map_tokens is not None and map_tokens > 0:
            map_budget_chars = max(0, int(map_tokens * self.CHARS_PER_TOKEN))
        elif map_ratio > 0:
            map_budget_chars = int(budget_chars * min(1.0, map_ratio))
        if map_budget_chars > 0:
            if map_detail == "structure":
                map_text, map_stats = self._structure_map_text(map_budget_chars, query, limit)
            else:
                map_text = self._project_map_text(map_budget_chars)
            if map_text and (map_tokens is None or map_tokens <= 0):
                # Historical form: the map is taken off the top of the section budget.
                budget_chars = max(400, budget_chars - len(map_text) - 1)
        total_budget_chars = budget_chars + map_budget_chars
        # Widened candidate search: when max_entries_per_file is active, top-scoring files
        # with many sections would starve other files if search_limit is too small.
        # SQLite FTS5 search over 1000 items takes < 5ms.
        search_limit = max(limit * 5, 1000) if max_entries_per_file else limit
        scored = self.backend.search(query, limit=search_limit)
        # A relevance floor. Measured on the real repository: a *technical* query
        # at >= 10 keeps 20 entries -- the code to change, its tests and the
        # public doc -- instead of hundreds of low-score neighbours.
        # On task wording, a calibrated floor (default 5.0) preserves key source
        # and test symbols (scoring ~5.75 on French prompts) while dropping noise.
        if resolved_min_score is not None:
            floor = float(resolved_min_score)
            scored = [item for item in scored if float(item.get("score", 0.0)) >= floor]

        # Diversity cap: prevent a single document with dozens of sections from
        # monopolizing the initial context budget at the expense of code/test symbols.
        if max_entries_per_file is not None and max_entries_per_file > 0:
            file_counts: Dict[str, int] = {}
            filtered: List[Dict[str, Any]] = []
            for item in scored:
                fp = item.get("file_path") or item.get("ref_id", "").split("::")[0].split("#")[0]
                if file_counts.get(fp, 0) < max_entries_per_file:
                    file_counts[fp] = file_counts.get(fp, 0) + 1
                    filtered.append(item)
                    if len(filtered) >= limit:
                        break
            scored = filtered

        entries: Dict[Any, ContextEntry] = {}
        order: List[Any] = []
        used = 0
        breadth_chars = int(budget_chars * max(0.0, min(1.0, breadth_ratio)))

        # Phase 1: cover as many relevant elements as possible, cheaply (level 0).
        for item in scored:
            key = (item.get("kind", ""), item["ref_id"])
            if key in entries:
                continue
            rendered_entry = self.render(key[0], key[1], 0)
            if rendered_entry is None:
                continue
            text, _extra = rendered_entry
            cost = len(text) + 1
            if used + cost > breadth_chars:
                break
            used += cost
            entries[key] = ContextEntry(key[0], key[1], 0, float(item.get("score", 0.0)), text)
            order.append(key)

        # Phase 2: deepen the highest-ranked elements while the budget allows.
        for level in range(1, max_level + 1):
            for key in order:
                entry = entries[key]
                if entry.level >= level:
                    continue
                if tiering.enabled and entry.score < tiering.deepen_min_score and level > tiering.low_tier_max_level:
                    continue
                rendered_entry = self.render(entry.kind, entry.ref_id, level, max_chars=max_content_chars)
                if rendered_entry is None:
                    continue
                text, extra = rendered_entry
                delta = len(text) - len(entry.text)
                if delta <= 0:
                    entry.level = level
                    entry.text = text
                    entry.content_hash = extra.get("content_hash")
                    entry.line_count = extra.get("line_count")
                    continue
                if used + delta > budget_chars:
                    continue
                used += delta
                entry.level = level
                entry.text = text
                entry.content_hash = extra.get("content_hash")
                entry.line_count = extra.get("line_count")

        ordered = sorted(entries.values(), key=lambda entry: entry.score, reverse=True)
        rendered: List[Dict[str, Any]] = []
        for entry in ordered:
            item: Dict[str, Any] = {
                "kind": entry.kind,
                # The address the caller can USE, not the storage id: one grammar, everywhere.
                "ref_id": self._printed_ref(entry.ref_id),
                "level": entry.level,
                "score": round(entry.score, 6),
                "text": entry.text,
            }
            if entry.content_hash:
                # The stamp travels WITH the numbering it describes, and only when the text is whole.
                item["content_hash"] = entry.content_hash
                item["line_count"] = entry.line_count
            rendered.append(item)
        entry_block = self._to_text(rendered)
        text_block = (map_text + "\n" + entry_block) if map_text else entry_block
        # What the caller actually receives: on the wire the model sees either
        # this `text` block (the default) or the `entries` array. Both are
        # reported so the real context cost is never hidden by a text-only
        # estimate; JSON costs ~29% more tokens than the identical text.
        wire_chars = len(json.dumps({"entries": rendered}, ensure_ascii=False)) if rendered else 0
        text_chars = len(text_block)
        deepest = sum(1 for entry in rendered if entry["level"] >= 3)
        return {
            "query": query,
            "budget_tokens": budget_tokens,
            # `used_tokens_estimate` describes the default (text) payload; the
            # JSON variant of the same content is reported separately because it
            # costs ~29% more tokens for identical information.
            "used_tokens_estimate": int(text_chars / self.CHARS_PER_TOKEN),
            "json_tokens_estimate": int(wire_chars / self.CHARS_PER_TOKEN),
            "budget_fill_ratio": round(text_chars / total_budget_chars, 4) if total_budget_chars else 0.0,
            "entry_count": len(rendered),
            "full_content_count": deepest,
            "map_chars": len(map_text),
            "map_budget_chars": map_budget_chars,
            "map_tokens": int(map_budget_chars / self.CHARS_PER_TOKEN),
            # A partial tree is not a map: the reader cannot tell what was kept
            # or dropped. Exposed so a caller can refuse to run on one.
            "map_truncated": "[carte tronquee" in map_text,
            # A structure map is bounded by its budget BY DESIGN (a full mini-AST of the
            # repository is ~209k tokens), so its own truncation is reported apart from
            # the tree's: only a truncated TREE makes the map unusable for navigation.
            "map_structure_files": map_stats.get("files", 0),
            "map_structure_truncated": bool(map_stats.get("truncated", False)),
            "min_score": resolved_min_score,
            "max_entries_per_file": max_entries_per_file,
            "max_content_chars": max_content_chars,
            "tiering": {
                "scope": effective_scope,
                "enabled": tiering.enabled,
                "min_score": resolved_min_score,
                "deepen_min_score": tiering.deepen_min_score if tiering.enabled else None,
                "low_tier_max_level": tiering.low_tier_max_level if tiering.enabled else None,
            },
            "truncated_entry_count": sum(1 for entry in rendered if "chars omitted from this entry" in entry["text"]),
            "entries": rendered,
            "text": text_block,
        }

    @staticmethod
    def _to_text(entries: Sequence[Dict[str, Any]]) -> str:
        groups: Dict[str, List[Tuple[Dict[str, Any], str]]] = {}
        for entry in entries:
            ref_id = entry.get("ref_id", "")
            if "::" in ref_id:
                file_path, _, member = ref_id.partition("::")
                entry_member = "::" + member
            elif "#" in ref_id:
                file_path, _, member = ref_id.partition("#")
                entry_member = "#" + member
            else:
                file_path = ref_id
                entry_member = ""
            groups.setdefault(file_path, []).append((entry, entry_member))

        lines: List[str] = []
        for file_path, items in groups.items():
            lines.append("File: " + file_path)
            for entry, member in items:
                label = "code" if entry.get("kind") == "symbol" else "doc"
                level = str(entry.get("level", 0))
                tag = "[" + label + " L" + level + "]"
                stamp = entry.get("content_hash")
                h = (" [h:" + str(stamp)[:8] + "]") if stamp else ""
                text = entry.get("text", "")
                if "\n" in text:
                    first_line, _, body = text.partition("\n")
                    lines.append("  " + tag + " " + (member or file_path) + h)
                    lines.append(body)
                else:
                    item_text = text
                    if member and text.startswith(file_path + member):
                        item_text = text[len(file_path + member):].lstrip(" :")
                    elif text.startswith(file_path):
                        item_text = text[len(file_path):].lstrip(" :")
                    detail = (" " + item_text) if item_text else ""
                    lines.append("  " + tag + " " + (member or file_path) + h + detail)
        return "\n".join(lines)

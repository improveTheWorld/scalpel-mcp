"""Rebasing the line numbers of a grouped edit against the writes that preceded it.

read_block prints 1-based line numbers; edit_block takes operations on them. In a
grouped call (edit_block(edits=[...])) every entry is written against the ONE version
the caller read, yet the first entry that writes moves the lines the following entries
address. The tool made those moves, so the tool is the only party that can undo them:
this module turns "line n of the version you read" into "line m of the file now".

The rule the module is built on -- and the one measurement that decided its design:

  a line's identity is its 1-based number in the version the caller READ; that number
  follows the LINE, never the position the line currently occupies.

  "the text of the aimed lines is enough" is FALSE. Measured in benchmarks/.work/
  CAS_ONE_SHOT.md case H, on the block "### 8. Block / - dup / - dup / - tail": entry 0
  inserts one more "- dup" above, entry 1 then aims at line 3. Text comparison sends the
  write to the copy entry 0 just inserted; identity sends it to the second "- dup" the
  caller actually read, which is now the fourth line. Identity wins.

Two admissible failures only, and nothing else is refused to be safe:

  1. the aimed identity no longer exists -- a previous entry of the same call deleted it;
  2. the text on that identity changed since the read -- a previous entry of the same call
     wrote on it, or a writer outside the tool.

Everything else is translated. insert_after n is anchored to the line the caller read ON
THAT NUMBER, not to a current number a previous entry may have moved. insert_after 0
addresses no existing line at all (the reading is measured and written up in
Patcher._lines_the_ops_touch, src/ast_graph_server/patcher.py:2702-2739): its translation
is empty and always safe.

WHERE THE REGIONS COME FROM, and why the alignment is only a fallback. translate()
records the exact operations it handed back, so when the write is observed the module
already knows which lines it consumed and which it produced. That knowledge is the only
way to place an insertion among IDENTICAL lines: after "insert_after 2, content '- dup'",
both candidate alignments are defensible, and difflib picks the left one -- which puts the
new copy below the aimed line and sends the next entry to the wrong "- dup". The recorded
plan is exact, so it wins (case H). The alignment is used only when no plan stands behind
the write -- a change the caller observed without translating it, i.e. a third party --
and then it is the best anyone has.

Two ways to record a write, because a caller may hold either shape:

  * observe_lines(before_lines, current_lines) -- the caller holds the file before and
    after the write. Idempotent on an unchanged state.
  * note_written(entry_index, first, before_lines, after_lines) -- the caller knows only
    the extent of what the entry wrote; the plan of its last translate() is the exact
    span, and the three numbers are the fallback when there is no plan.

This module knows nothing about files, locks or blocks: it is a pure, synchronous number
rebaser over two line lists.
"""

import difflib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# One change region: the half-open range of positions this write consumed, and the
# half-open range it produced in their place. Both in 0-based positions of the state
# BEFORE the write.
Region = Tuple[int, int, int, int]


@dataclass
class OpTranslation:
    """The operations of ONE entry, rebased onto the block's current state."""

    ops: List[Dict[str, Any]]
    touched: Set[int]
    rebased: List[int]
    by_entry: Dict[int, List[int]]


class LineTranslationError(Exception):
    """The translation is IMPOSSIBLE.

    Two causes, and only two: an aimed line that no longer exists (a previous entry of
    the same call deleted it), or an aimed line whose text changed since the read (a
    previous entry of the same call wrote on it, or a writer outside the tool). Both
    name the line and, when the tool knows it, the entry that caused it. An operation
    this module cannot read (not an object, no replace/delete/insert_after, a malformed
    span, a number outside the version the caller read) is reported the same way: the
    module states it does not know how to translate it, it never invents a business
    refusal of its own.
    """


class LineTranslation:
    """One grouped call: the version the caller read, and the file as it now stands.

    before is frozen at construction. current advances with every observe() /
    observe_lines() / note_written(). While nothing has moved the two lists have the same
    length; afterwards before keeps its lines and current moves.
    """

    def __init__(self, before: str, current: str) -> None:
        # The text of each identity, frozen at construction: this is the reference every
        # "its text changed" verdict is taken against.
        self._text: List[str] = before.splitlines()
        # current 0-based position -> the identity standing there, or None when the line
        # there is not one the caller read (a line this call produced). POSITION-indexed
        # is the only form that survives an insert: an identity-indexed list needs a hole
        # per dead identity, and compacting those holes renumbers everything below --
        # measured: an insert above line 2 left line 2 on the position of the copy above
        # it. The two readings meet in exactly two helpers, _identity_at(position) and
        # _slot(identity); everywhere else carries one or the other, never both.
        self._states: List[Optional[int]] = list(range(1, len(self._text) + 1))
        self._current: List[str] = current.splitlines()
        # The snapshot is the version the caller READ, not the one the file holds now: when
        # the two differ at construction, a writer changed the block between the read and
        # this call, and _absorb_unobserved_change has to align before -> current from the
        # first translate. Starting the snapshot at the current text would hide that change
        # and read every aimed line at the wrong position.
        self._snapshot: List[str] = list(self._text)
        # Identities an entry of this call wrote on (a rewrite OR a delete) and deleted.
        self._rewritten: Set[int] = set()
        self._deleted: Set[int] = set()
        self._writers: Dict[int, int] = {}
        # (entry, the identities that were still alive when it moved them), in application
        # order. Only ENTRIES appear here: an entry index is a real number the caller
        # names, and a second meaning in the same list would make it unreadable.
        self._shift_records: List[Tuple[int, Tuple[int, ...]]] = []
        # The identities a writer OUTSIDE this call moved. A separate answer, separate
        # question: shifts_of answers in entry indices, moved_outside in caller lines.
        self._outside_moved: Set[int] = set()
        # entry -> the operations translate() handed back, so the write can be placed
        # exactly. Keyed by the entry index the caller holds.
        self._plans: Dict[int, List[Tuple[str, int, int, int]]] = {}
        # The number of entries translated so far, and the entry whose write is the next
        # state to observe. Nothing can write between translate() and the observation --
        # the per-file lock of this same chantier holds.
        self._entry: int = 0
        self._writing_entry: Optional[int] = None
        self._recorded: Set[int] = set()
        #: The LAST text each identity carries, when an entry has already rewritten it. The
        #: text check below must compare a line against what it holds NOW, not against the
        #: version the caller read: an entry that writes a line is entitled to come back to
        #: that line, and comparing it with the text it replaced refused the third entry of a
        #: three-entry call (measured: insert above, then two replaces on shifted lines).
        self._last_text: Dict[int, str] = {}
        # A block already moved when the call opened is absorbed here, exactly as a change
        # observed later would be: same alignment, same moved_outside() answer.
        self._absorb_unobserved_change()

    # ------------------------------------------------------------------------ observing

    def observe(self, current: str) -> None:
        """The state of the WHOLE FILE after a write. Never raises."""
        try:
            lines = current.splitlines()
        except AttributeError:
            return
        self.observe_lines(self._snapshot, lines)

    def observe_lines(self, before_lines: Sequence[str], current_lines: Sequence[str]) -> None:
        """Same, over line LISTS: before the first write of the call, then after each one.

        THIS is the method that builds the correspondence. Idempotent on an unchanged
        state: equal lists align line for line and every identity keeps its position.
        """
        try:
            before = [str(line) for line in (before_lines or [])]
            after = [str(line) for line in (current_lines or [])]
        except TypeError:
            # Documented to never raise: an unreadable state leaves the map as it was,
            # rather than taking the server down inside an observation.
            return
        if before == after:
            if len(self._states) != len(before):
                # An unchanged state of a different length is not this call's state:
                # rebuild the identity list rather than trust a stale one.
                self._states = list(range(1, len(before) + 1))
            self._current = list(after)
            self._snapshot = list(after)
            return
        if len(before) != len(self._states):
            # The baseline explicitly given wins over the one tracked so far: a write
            # nobody observed cannot leave identity pinned to the wrong line.
            self._states = list(range(1, len(before) + 1))
        entry = self._writing_entry
        plan = self._plans.pop(entry) if entry is not None and entry in self._plans else None
        regions = self._spans_of_plan(plan) if plan else []
        if regions and len(self._states) + sum(
            (new_end - new_start) - (old_end - old_start)
            for old_start, old_end, new_start, new_end in regions
        ) != len(after):
            # The plan and the observed state disagree: something wrote besides this entry,
            # so the plan no longer describes the file. Drop it and say so by attributing
            # the move to nobody -- an entry named without a proof is a false accusation,
            # and the text check refuses on its own account.
            regions = []
        if not regions and before != after:
            # No plan stands behind this write: a change the caller observed without
            # translating it, so a third party wrote. The alignment is all there is.
            regions = self._align(before, after)
            entry = None
        self._claim_writers(regions, entry)
        moved = self._rewrite(regions, entry)
        self._record_shifts(moved)
        self._writing_entry = None
        self._current = list(after)
        self._snapshot = list(after)

    # ------------------------------------------------------------------------ recording

    def note_written(self, entry_index: int, first: int, before_lines: int, after_lines: int) -> None:
        """Record the write the entry just made, IN THE CALLER'S OWN INDICES.

        first is the 0-based index, in the state BEFORE this write, of the first line the
        write produced (for a pure delete: of the line the deletion left in its place);
        before_lines is the number of lines the file carried before the write, after_lines
        the number it carries after. Never raises.
        """
        try:
            entry = int(entry_index)
            first = int(first)
            before_lines = int(before_lines)
            after_lines = int(after_lines)
        except (TypeError, ValueError):
            # Documented to never raise: an unreadable extent records nothing, and the next
            # translate will answer from the text it can still compare.
            return
        if entry in self._recorded:
            # A second identical call must not move the state a second time.
            return
        plan = self._plans.pop(entry, None)
        if plan:
            regions = self._spans_of_plan(plan)
        else:
            regions = self._spans_of_counts(first, before_lines, after_lines)
        self._recorded.add(entry)
        self._claim_writers(regions, entry)
        moved = self._rewrite(regions, entry)
        self._record_shifts(moved)
        if self._current == self._snapshot and regions:
            # The caller described the write without handing over the new lines: the
            # snapshot follows the regions so a later observe() has a baseline.
            kept = list(self._snapshot)
            for old_start, old_end, new_start, new_end in regions:
                del kept[old_start:old_end]
                kept[old_start:old_start] = [""] * (new_end - new_start)
            self._current = kept
            self._snapshot = list(kept)

    def writer_of(self, caller_line: int) -> Optional[int]:
        """The entry that wrote on this line (1-based, in the version the caller read).

        A DELETE is a write on that line: the entry that removed it is named here too, and
        the reason for its absence is told apart by the refusal's own wording.
        """
        entry = self._writers.get(int(caller_line))
        return None if entry is None else int(entry)

    def shifts_of(self, caller_line: int) -> List[int]:
        """The ENTRIES that moved this line without writing on it, ascending.

        Strictly entry indices: a line moved by somebody outside the call is not an entry
        of it, and is answered by moved_outside() instead.
        """
        target = int(caller_line)
        if target < 1 or target > len(self._text):
            return []
        seen: List[int] = []
        for entry, keys in self._shift_records:
            if target in keys and entry not in seen:
                seen.append(entry)
        return sorted(seen)

    def moved_outside(self) -> List[int]:
        """The caller's lines a writer OUTSIDE this call moved, ascending.

        The second admissible failure needs its own answer: nobody can translate a change
        this call did not make, and the caller has to be able to tell that cause from an
        entry of its own batch.
        """
        return sorted(self._outside_moved)

    # ------------------------------------------------------------------------ translation

    def translate(self, ops: Any) -> OpTranslation:
        """Rebase the operations of ONE entry onto the current state.

        Raises LineTranslationError when an aimed line no longer exists or its text
        changed since the read, and when an operation cannot be read at all.
        """
        self._absorb_unobserved_change()
        if ops is None:
            items: List[Any] = []
        elif isinstance(ops, (list, tuple)):
            items = list(ops)
        elif isinstance(ops, dict):
            items = [ops]
        else:
            raise LineTranslationError(
                "ops: expected a list of operations, got " + type(ops).__name__ + "."
            )

        total = len(self._text)
        touched: Set[int] = set()
        plan: List[Tuple[str, int, int, int]] = []
        out: List[Dict[str, Any]] = []

        for position, op in enumerate(items):
            if not isinstance(op, dict):
                raise LineTranslationError(
                    "op " + str(position) + ": expected an object, got " + type(op).__name__ + "."
                )
            if "replace" in op or "delete" in op:
                is_delete = "delete" in op
                table = op["delete"] if is_delete else op["replace"]
                first, last = self._read_span(position, table, total)
                for identity in range(first, last + 1):
                    self._require_intact(identity, position)
                out.append(self._rebase_range(op, first, last))
                produced = 0 if is_delete else len(str(op.get("content", "")).splitlines())
                plan.append(("range", first, last - first + 1, produced))
                touched.update(range(first, last + 1))
                continue
            if "insert_after" in op:
                anchor, _same = self._read_span(position, op["insert_after"], total, anchor=True)
                produced = len(str(op.get("content", "")).splitlines())
                if anchor >= 1:
                    # AN ANCHOR NEED ONLY EXIST. `insert_after n` writes no line: it only
                    # has to stand next to the line the caller read, so a previous entry
                    # having REWRITTEN that line is not an obstacle -- measured, the same
                    # pair of operations in the reverse order was served, and refusing one
                    # order of a pair whose intent is unambiguous is a refusal the tool
                    # caused itself. `_require_intact` stays for the range case, where the
                    # entry really does write on the lines it names.
                    self._require_exists(anchor, position)
                    out.append(self._rebase_anchor(op, anchor))
                    touched.add(anchor)
                else:
                    # The head boundary carries no identity: nothing to rebase, always
                    # safe -- insert_after 0 addresses no existing line.
                    out.append(dict(op))
                plan.append(("after", anchor, 0, produced))
                continue
            raise LineTranslationError(
                "op " + str(position) + ": expected replace, delete or insert_after."
            )

        self._entry += 1
        self._plans[self._entry - 1] = plan
        self._writing_entry = self._entry - 1

        by_entry: Dict[int, List[int]] = {}
        for identity in sorted(touched):
            for mover in self._movers_of(identity):
                by_entry.setdefault(mover, []).append(identity)
        return OpTranslation(
            ops=out,
            touched=set(touched),
            rebased=sorted(by_entry),
            by_entry=by_entry,
        )

    # ------------------------------------------------------------------------ internals

    def _identity_at(self, position: int) -> Optional[int]:
        """The identity (1-based) standing at this position, or None for a line we did not read."""
        if 0 <= position < len(self._states):
            return self._states[position]
        return None

    def _slot(self, identity: int) -> Optional[int]:
        """The 0-based position this identity stands at now, or None when it is gone."""
        if identity < 1:
            return None
        for position, standing in enumerate(self._states):
            if standing == identity:
                return position
        return None

    def _slot_of(self, identity: int) -> int:
        """The 1-based number this identity occupies now -- the number the file answers to."""
        slot = self._slot(identity)
        if slot is None:
            raise LineTranslationError("line " + str(identity) + " no longer exists.")
        return slot + 1

    def _rebase_range(self, op: Dict[str, Any], first: int, last: int) -> Dict[str, Any]:
        out = dict(op)
        if "delete" in op:
            # A RANGE IS A PAIR [first, last], NEVER A LIST OF LINES. Emitting one slot per
            # identity made a single-line delete `[4]` instead of `[4, 4]`, and lineops reads
            # a range expecting two elements: `op 0: malformed range or content`, measured by
            # the orchestrator on case G of benchmarks/.work/CAS_ONE_SHOT.md. The last identity
            # is the end of the range whichever way the lines have moved.
            out["delete"] = [self._slot_of(first), self._slot_of(last)]
        else:
            out["replace"] = [self._slot_of(first), self._slot_of(last)]
            # The content is the caller's, unchanged: it is not the tool's to rewrite.
            out["content"] = str(op.get("content", ""))
        return out

    def _rebase_anchor(self, op: Dict[str, Any], anchor: int) -> Dict[str, Any]:
        out = dict(op)
        out["insert_after"] = self._slot_of(anchor)
        return out

    def _require_exists(self, identity: int, op_index: int) -> None:
        """Refuse only a line that is GONE -- for a line an operation merely stands next to.

        `insert_after n` addresses the boundary after line n, so the line itself does not
        have to still hold the text the caller read: an earlier entry may have rewritten it,
        and the insertion is still exactly where it was asked for. A line a previous entry
        DELETED is a different matter -- the boundary no longer exists and nothing can be
        translated, so that refusal stands.
        """
        detail = " (op " + str(op_index) + ")"
        if identity in self._deleted:
            raise LineTranslationError(
                "line " + str(identity) + " no longer exists" + detail
                + self._written_by(identity) + "."
            )
        if identity < 1 or identity > len(self._text) or self._slot(identity) is None:
            raise LineTranslationError(
                "line " + str(identity) + " is outside the version you read" + detail + "."
            )

    def _require_intact(self, identity: int, op_index: int) -> None:
        """Refuse -- and only refuse -- a line that is gone or whose text changed."""
        detail = " (op " + str(op_index) + ")"
        owner = self._written_by(identity)
        if identity in self._deleted:
            raise LineTranslationError(
                "line " + str(identity) + " no longer exists" + detail + owner + "."
            )
        if identity < 1 or identity > len(self._text):
            raise LineTranslationError(
                "line " + str(identity) + " is outside the version you read" + detail + "."
            )
        slot = self._slot(identity)
        if slot is None:
            reason = "its text changed since you read it" if identity in self._rewritten else "no longer exists"
            raise LineTranslationError(
                "line " + str(identity) + detail + ": " + reason + owner + "."
            )
        observed = self._current[slot] if 0 <= slot < len(self._current) else None
        if observed != self._text[identity - 1] or identity in self._rewritten:
            # A rewrite that happened to restore the very same text is still a write by
            # someone else on this line: the caller decides, this module does not guess.
            raise LineTranslationError(
                "line " + str(identity) + detail + ": its text changed since you read it"
                + owner + "."
            )

    def _written_by(self, identity: int) -> str:
        entry = self._writers.get(int(identity))
        if entry is None:
            return ""
        if identity in self._deleted:
            return " -- entry " + str(entry) + " of this call deleted it"
        return " -- entry " + str(entry) + " of this call wrote on it"

    def _read_span(
        self,
        op_index: int,
        table: Any,
        total: int,
        anchor: bool = False,
    ) -> Tuple[int, int]:
        """Read a range or an anchor against THE VERSION THE CALLER READ. Raises on junk."""
        if anchor:
            try:
                value = int(table)
            except (TypeError, ValueError):
                raise LineTranslationError(
                    "op " + str(op_index) + ": insert_after is not a number."
                )
            if value < 0 or value > total:
                raise LineTranslationError(
                    "op " + str(op_index) + ": insert_after must be inside 0.." + str(total) + "."
                )
            return value, value
        if not isinstance(table, (list, tuple)) or len(table) != 2:
            raise LineTranslationError("op " + str(op_index) + ": malformed range.")
        try:
            first, last = int(table[0]), int(table[1])
        except (TypeError, ValueError):
            raise LineTranslationError("op " + str(op_index) + ": malformed range.")
        if first < 1 or last < first or last > total:
            raise LineTranslationError(
                "op " + str(op_index) + ": range must be inside 1.." + str(total) + "."
            )
        return first, last

    def _spans_of_plan(
        self, plan: Sequence[Tuple[str, int, int, int]]
    ) -> List[Region]:
        """The exact regions of the operations translate() handed back.

        The operations are resolved against ONE version and applied bottom-up (the rule of
        lineops.apply_ops, src/ast_graph_server/lineops.py:13-15), so the regions are
        returned in descending position: applying them in that order keeps every position
        valid, and the map never sees a half-applied batch.
        """
        regions: List[Region] = []
        for kind, first, consumed, produced in plan:
            if consumed < 1 and produced < 1:
                continue
            if kind == "after":
                anchor = None if first < 1 else self._slot(first)
                if first >= 1 and anchor is None:
                    continue
                position = 0 if anchor is None else anchor + 1
                # AN INSERTION CONSUMES NOTHING, so it names NO WRITER. The region of
                # an insert_after n must retire the EMPTY range that starts after line
                # n: the identity of n stands just before that point and stays there.
                # This was the defect the orchestrator measured on the seven cases of
                # CAS_ONE_SHOT: with the anchor itself inside [old_start, old_end),
                # `_claim_writers` recorded the inserting entry as the WRITER of the
                # anchor line, and the next entry -- which replaces exactly that line,
                # as cases A, B, C and H all do -- was then refused for a conflict with
                # an entry that never wrote on it. An insertion moves a line; it does
                # not write on one, and only a replace or a delete names a writer.
                regions.append((position, position, position, position + produced))
            else:
                position = self._slot(first)
                if position is None:
                    continue
                regions.append((position, position + consumed, position, position + produced))
        return sorted(regions, key=lambda region: region[0], reverse=True)

    @staticmethod
    def _spans_of_counts(first: int, before_lines: int, after_lines: int) -> List[Region]:
        """A region derived from the three numbers alone -- the no-plan fallback.

        Only the DELTA is known here, so the whole tail from the first index is taken to be
        what the write rewrote. It is exact for the shape it exists for (one operation that
        runs to the end of the block); a caller that holds the plan gets the exact regions.
        """
        consumed = max(0, before_lines - first)
        produced = max(0, after_lines - before_lines)
        return [(first, first + consumed, first, first + produced)]

    def _absorb_unobserved_change(self) -> None:
        """Absorb a change nobody observed, so the identity map is never read stale."""
        if self._current == self._snapshot:
            return
        before = list(self._snapshot)
        after = list(self._current)
        regions = self._align(before, after)
        moved = self._rewrite(regions, None)
        self._record_shifts(moved)
        self._snapshot = list(after)

    def _align(self, before: Sequence[str], after: Sequence[str]) -> List[Region]:
        """The change regions, as (old_start, old_end, new_start, new_end), descending.

        Used only for a write no translate() recorded: the alignment cannot tell two
        identical lines apart, and picking the left one is wrong for a duplicated block
        (case H). The recorded plan is exact; this is the fallback for a third party.
        """
        matcher = difflib.SequenceMatcher(None, list(before), list(after), autojunk=False)
        regions = [
            (old_start, old_end, new_start, new_end)
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes()
            if tag != "equal"
        ]
        return sorted(regions, key=lambda region: region[0], reverse=True)

    def _claim_writers(self, regions: Sequence[Region], entry: Optional[int]) -> None:
        """Name, per line, the entry that wrote on it -- including a delete.

        An insert retires nothing and names nobody: the lines below it only moved, and
        writer_of must keep answering None for them.
        """
        if entry is None:
            return
        for old_start, old_end, _new_start, _new_end in regions:
            for position in range(old_start, old_end):
                identity = self._identity_at(position)
                if identity is not None:
                    self._writers[int(identity)] = entry

    def _rewrite(
        self,
        regions: Sequence[Region],
        entry: Optional[int],
    ) -> List[Tuple[Optional[int], Tuple[int, ...]]]:
        """Apply the change regions to the map, and SAY which identities moved.

        Head and tail carry IDENTITIES, and a region's gain is expressed by what sits
        BETWEEN them, never by adding it to a value: an identity does not change when its
        line moves, only the position it is stored at does. Adding the gain to the values
        renumbers the lines twice -- measured: an insert above line 2 left line 2 nowhere
        and put line 3 on its position.
        """
        moved: List[Tuple[Optional[int], Tuple[int, ...]]] = []
        for old_start, old_end, new_start, new_end in regions:
            retired = [
                identity
                for identity in (self._identity_at(position) for position in range(old_start, old_end))
                if identity is not None
            ]
            deleted = new_end - new_start == 0
            for identity in retired:
                if deleted:
                    self._deleted.add(int(identity))
                else:
                    self._rewritten.add(int(identity))
            shift = (new_end - new_start) - (old_end - old_start)
            # THE SLOTS OUTSIDE THE REGION ARE KEPT AS THEY ARE -- POSITION FOR POSITION.
            # They used to be rebuilt as "every identity that is not None", which DROPPED the
            # placeholders an earlier insertion had put there: `[1, None, 2, 3, 4]` came back
            # as `[1, 2, 4]` after one replace, so identity 4 stood at index 2 -- compared
            # against the text of "- a" -- and the third entry of a call was refused for "its
            # text changed since you read it" while the line it aimed at was intact in the
            # file. Found by the independent verifier, which named this line as the cause;
            # its fix is this one: slice the list, do not filter it.
            head = list(self._states[:old_start])
            # THE TAIL IS KEPT AS IT STANDS TOO -- placeholders INCLUDED, position for
            # position. Filtering it was the MIRROR HALF of the same defect, and the same
            # verifier found it: with an insertion ABOVE and a replace BELOW, `[1, 2, 3, None,
            # 4]` came back as `[1, None, 3, 4]`, identity 4 was read at index 3 ("- INS"), and
            # the third entry was refused for a line that was intact in the file. Fixing one
            # half of a filter leaves the defect in the other half; the honest statement is
            # that the slots outside the region are NONE OF THIS FUNCTION BUSINESS, and it must
            # return them exactly as it received them.
            tail = list(self._states[old_end:])
            shifted: List[int] = []
            for standing in tail:
                if standing is None:
                    continue
                shifted.append(int(standing))
            if shifted and shift != 0:
                moved.append((entry, tuple(shifted)))
            # A REWRITTEN LINE KEEPS ITS IDENTITY. It was measured that retiring it left the
            # map unable to answer for the very line the entry had just written: the file held
            # "### 8. Alpha / - a / - b / - C9" and the rebaser, asked for line 4 -- the line
            # read -- answered "line 4 is outside the version you read", so the next entry of
            # the same call was refused for a block it had read correctly. A replace rewrites
            # lines that STILL EXIST: each retired identity gets the produced line that took
            # its place, one for one, and `_rewritten` is what tells the text changed. Only
            # when fewer lines come out than went in does an identity lose its place, and only
            # a DELETE retires them all (new_end == new_start -> nothing is produced).
            replacements: List[Optional[int]] = [None] * (new_end - new_start)
            if not deleted:
                for offset, identity in enumerate(retired):
                    if offset < len(replacements):
                        replacements[offset] = int(identity)
                        position = new_start + offset
                        if 0 <= position < len(self._current):
                            self._last_text[int(identity)] = str(self._current[position])
            self._states = head + replacements + tail
        return moved

    def _record_shifts(self, moved: Sequence[Tuple[Optional[int], Tuple[int, ...]]]) -> None:
        """Two questions, two answers: entry moves go to shifts_of, outside ones elsewhere."""
        for entry, keys in moved:
            if entry is None:
                self._outside_moved.update(keys)
            else:
                self._shift_records.append((entry, keys))

    def _movers_of(self, identity: int) -> List[int]:
        """The entries BEFORE the one being translated that moved this line, ascending."""
        found: List[int] = []
        for entry, keys in self._shift_records:
            if entry >= self._entry:
                continue
            if identity in keys and entry not in found:
                found.append(entry)
        return sorted(found)

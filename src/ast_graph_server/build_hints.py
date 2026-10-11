"""Compiler 'help:' notes for an unknown C# type name: CS0246, CS0426, CS0234.

Why this exists (measured, 03/10/2026): in 3 runs of 10 of the latest series the agent wrote C#
naming the Spark builder under an invented name -- ``SparkSessionBuilder`` (CS0246) or
``SparkSession.Builder`` used as a type (CS0426) -- while the real type is
``Microsoft.Spark.Sql.Builder`` (NuGet package Microsoft.Spark 2.3.0). The compiler says the name
does not exist; it does not say which name does.

What this module adds is what rustc ('help: a struct with a similar name exists'), clang ('did you
mean') and Roslyn's own spell-check add: AFTER the compiler output, which is never modified, one
line per faulty name, of the form

    help: 'SparkSessionBuilder' (CS0246, SparkConnectOptions.cs:85) -- did you mean
    'Microsoft.Spark.Sql.Builder' (external, package Microsoft.Spark 2.3.0)? Returned by
    SparkSession.Builder().

Rules (deterministic, no model involved):

* the names searched are the TYPES declared in the workspace's .cs files and the types and members
  of the NuGet packages the FAILING project references (its <PackageReference>s, and those of the
  projects it references through <ProjectReference>, since SDK projects flow them transitively);
* candidates, best first: (1) for ``A.B`` (CS0426) where A has a METHOD or PROPERTY B: B is a
  member, not a type, and its return type when the index knows it; (2) for a compound name
  ``AB`` where type A has a member B returning type T: T ("Returned by A.B()"); (3) the exact
  simple name in another namespace ("add using N"); (4) similarity: one name contains the other on
  a CamelCase word boundary (candidate name of 4+ characters), or a small edit distance;
* at most 3 candidates per faulty name, one line per faulty name (the compiler repeats each error),
  and NO line at all when nothing credible was found: a note that guesses is noise.

Cost: the package XML documentation is parsed once per (folder, size, mtime) for the life of the
process, and a workspace .cs file is re-read only when its size or mtime changed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .externals import (
    find_doc_xmls,
    infer_return_types,
    nuget_packages_root,
    parse_doc_xml,
    parse_package_references,
    resolve_package_dir,
)

#: Every note line starts with this: the harness keeps these lines when it filters the compiler
#: output down to its error lines (``benchmarks/agent.py``).
HINT_PREFIX = "help: "

HINT_CODES = ("CS0246", "CS0426", "CS0234")
MAX_CANDIDATES = 3
#: A candidate CONTAINED in the faulty name must be at least this long: 'Row' inside
#: 'ArrowRowBatch' is a coincidence, not a suggestion.
MIN_CONTAINED_LENGTH = 4

#: `path(line,col): error CSxxxx: <localised sentence> [project]`. The sentence is localised
#: (French on the reference machine): only the code, the quoted names and the bracket are parsed.
_ERROR_LINE = re.compile(
    r"^\s*(?P<file>.+?)\((?P<line>\d+)(?:,\d+)?\)\s*:\s*error\s+(?P<code>CS\d{4})\s*:\s*(?P<message>.*?)"
    r"(?:\s*\[(?P<project>[^\]]+\.(?:cs|fs|vb)proj)\])?\s*$"
)
#: Quoted names: ASCII quotes, typographic quotes, and the French guillemets.
_QUOTED = re.compile(r"['\u2018\u2019\u00ab\"]\s*([A-Za-z_@][\w.`<>@]*?)\s*['\u2018\u2019\u00bb\"]")
_SKIPPED_DIRS = {"bin", "obj", "node_modules", "packages"}


# ----------------------------------------------------------------------------------------
# Parsing the compiler output
# ----------------------------------------------------------------------------------------


@dataclass
class UnknownName:
    code: str
    name: str  # the faulty simple name ('SparkSessionBuilder', 'Builder')
    container: str = ""  # CS0426: the type; CS0234: the namespace
    file: str = ""
    line: int = 0
    project: str = ""
    occurrences: int = 1
    places: Set[Tuple[str, int]] = field(default_factory=set)

    @property
    def written(self) -> str:
        return self.container + "." + self.name if self.container else self.name


def parse_unknown_names(output: str) -> List[UnknownName]:
    """One entry per faulty name, in order of first appearance; duplicates are folded."""
    found: Dict[Tuple[str, str, str], UnknownName] = {}
    for raw in (output or "").splitlines():
        match = _ERROR_LINE.match(raw)
        if not match or match.group("code") not in HINT_CODES:
            continue
        names = _QUOTED.findall(match.group("message"))
        if not names:
            continue
        code = match.group("code")
        name = _strip_generic(names[0])
        container = _strip_generic(names[1]) if code in ("CS0426", "CS0234") and len(names) > 1 else ""
        if "." in name and not container:
            container, _, name = name.rpartition(".")
        if not name:
            continue
        key = (code, container, name)
        place = (match.group("file").strip(), int(match.group("line")))
        entry = found.get(key)
        if entry is None:
            entry = UnknownName(
                code=code,
                name=name,
                container=container,
                file=place[0],
                line=place[1],
                project=(match.group("project") or "").strip(),
            )
            entry.places.add(place)
            found[key] = entry
        elif place not in entry.places:
            entry.places.add(place)
            entry.occurrences += 1
    return list(found.values())


def _strip_generic(name: str) -> str:
    name = name.strip().lstrip("@")
    name = re.sub(r"<.*>$", "", name)
    return re.sub(r"`\d+$", "", name)


# ----------------------------------------------------------------------------------------
# The names that exist: workspace types, package types and members
# ----------------------------------------------------------------------------------------


@dataclass
class KnownType:
    full_name: str
    origin: str  # 'external, package Microsoft.Spark 2.3.0' / 'workspace, App/Foo.cs'

    @property
    def simple(self) -> str:
        return self.full_name.rsplit(".", 1)[-1]

    @property
    def namespace(self) -> str:
        return self.full_name.rsplit(".", 1)[0] if "." in self.full_name else ""


@dataclass
class KnownMember:
    owner: str  # full name of the declaring type
    name: str
    kind: str  # 'method' | 'property'
    returns_type: str  # full name, or ''
    origin: str


@dataclass
class NameIndex:
    types: List[KnownType] = field(default_factory=list)
    members: List[KnownMember] = field(default_factory=list)

    def extend(self, other: "NameIndex") -> None:
        self.types.extend(other.types)
        self.members.extend(other.members)


#: (package folder) -> (stamp, NameIndex). Process-wide: a package does not change during a run.
_PACKAGE_CACHE: Dict[str, Tuple[str, NameIndex]] = {}
#: (file path) -> ((size, mtime_ns), NameIndex)
_SOURCE_CACHE: Dict[str, Tuple[Tuple[int, int], NameIndex]] = {}


def _package_index(package: str, folder: Path) -> NameIndex:
    xmls = find_doc_xmls(folder)
    try:
        stamp = ";".join(p.name + ":" + str(p.stat().st_size) + ":" + str(p.stat().st_mtime_ns) for p in xmls)
    except OSError:
        stamp = ""
    cached = _PACKAGE_CACHE.get(str(folder))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    symbols = []
    for xml in xmls:
        symbols.extend(parse_doc_xml(xml))
    infer_return_types(symbols)
    origin = "external, package " + package + " " + folder.name
    index = NameIndex()
    for symbol in symbols:
        if symbol.kind == "external_type":
            index.types.append(KnownType(symbol.full_name, origin))
        elif symbol.kind in ("external_method", "external_property") and symbol.parent:
            index.members.append(
                KnownMember(
                    owner=symbol.parent,
                    name=symbol.name,
                    kind="method" if symbol.kind == "external_method" else "property",
                    returns_type=symbol.returns_type or "",
                    origin=origin,
                )
            )
    _PACKAGE_CACHE[str(folder)] = (stamp, index)
    return index


def _project_closure(project: Path, limit: int = 64) -> List[Path]:
    """The project and every project it references, transitively (bounded)."""
    from .externals import _child_text, _local, _read_xml  # module-private helpers, same package

    seen: List[Path] = []
    pending = [project]
    while pending and len(seen) < limit:
        current = pending.pop()
        try:
            current = current.resolve()
        except OSError:
            continue
        if current in seen or not current.is_file():
            continue
        seen.append(current)
        root = _read_xml(current)
        if root is None:
            continue
        for element in root.iter():
            if _local(element.tag) != "ProjectReference":
                continue
            include = (element.get("Include") or "").strip()
            # An analyzer or build-order reference (ReferenceOutputAssembly="false") gives the
            # referencing project's CODE nothing: its packages must not become candidates.
            # Measured on the target: the analyzer reference brought Microsoft.CodeAnalysis.CSharp in.
            reference_output = (element.get("ReferenceOutputAssembly") or _child_text(element, "ReferenceOutputAssembly") or "")
            if reference_output.strip().lower() == "false":
                continue
            if include and "$(" not in include:
                pending.append(current.parent / include.replace("\\", os.sep))
    return seen


def external_index_for(projects: Sequence[Path], workspace_root: Optional[Path] = None) -> NameIndex:
    """Types and members of the NuGet packages the projects reference (and their project refs)."""
    nuget = nuget_packages_root()
    index = NameIndex()
    done: Set[str] = set()
    for project in projects:
        for csproj in _project_closure(project):
            for package, version in parse_package_references(csproj, workspace_root):
                folder = resolve_package_dir(nuget, package, version)
                if folder is None or str(folder).lower() in done:
                    continue
                done.add(str(folder).lower())
                index.extend(_package_index(package, folder))
    return index


_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.MULTILINE)
_TYPE_DECL = re.compile(
    r"\b(?:class|struct|interface|enum|record(?:\s+struct|\s+class)?)\s+(@?[A-Za-z_]\w*)"
)
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r'@?"(?:[^"\\\n]|\\.)*"')


def _source_index(path: Path, relative: str) -> NameIndex:
    try:
        stat = path.stat()
    except OSError:
        return NameIndex()
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _SOURCE_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return NameIndex()
    text = _STRING.sub('""', _COMMENT.sub(" ", text))
    namespace_match = _NAMESPACE.search(text)
    namespace = namespace_match.group(1) if namespace_match else ""
    origin = "workspace, " + relative
    index = NameIndex()
    for match in _TYPE_DECL.finditer(text):
        name = match.group(1).lstrip("@")
        full = namespace + "." + name if namespace else name
        index.types.append(KnownType(full, origin))
        index.members.extend(_workspace_members(text, match.end(), full, origin))
    _SOURCE_CACHE[str(path)] = (key, index)
    return index


_MEMBER_DECL = re.compile(
    r"(?:public|internal|protected|private|static|virtual|override|abstract|sealed|async|new|\s)+"
    r"(?P<type>[A-Za-z_][\w.<>,\[\]? ]*?)\s+(?P<name>[A-Za-z_]\w*)\s*(?P<open>\(|\{|=>)"
)


def _workspace_members(text: str, start: int, owner: str, origin: str) -> List[KnownMember]:
    """Methods and properties declared at the first brace level of the type body after `start`."""
    body_start = text.find("{", start)
    if body_start < 0:
        return []
    depth = 0
    top_level: List[str] = []
    segment_start = body_start + 1
    for index in range(body_start, len(text)):
        char = text[index]
        if char == "{":
            if depth == 1:
                top_level.append(text[segment_start:index + 1])
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 1:
                segment_start = index + 1
            if depth == 0:
                top_level.append(text[segment_start:index])
                break
        elif char == ";" and depth == 1:
            top_level.append(text[segment_start:index + 1])
            segment_start = index + 1
    members: List[KnownMember] = []
    for segment in top_level:
        match = _MEMBER_DECL.search(segment)
        if not match:
            continue
        declared = match.group("type").strip()
        if declared in ("class", "struct", "interface", "enum", "record", "return", "new", "using"):
            continue
        kind = "method" if match.group("open") == "(" else "property"
        members.append(KnownMember(owner, match.group("name"), kind, declared, origin))
    return members


def workspace_index(workspace: Path, limit: int = 20000) -> NameIndex:
    index = NameIndex()
    workspace = Path(workspace)
    count = 0
    for directory, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIPPED_DIRS and not d.startswith(".")]
        for filename in filenames:
            if not filename.endswith(".cs"):
                continue
            count += 1
            if count > limit:
                return index
            path = Path(directory) / filename
            index.extend(_source_index(path, path.relative_to(workspace).as_posix()))
    return index


# ----------------------------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------------------------


def _words(name: str) -> List[str]:
    return re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+", name)


def _contains_on_boundary(outer: str, inner: str) -> bool:
    """'SparkSessionBuilder' contains 'Builder' and 'SparkSession', not 'ess' nor 'Session' halves."""
    if len(inner) >= len(outer):
        return False
    outer_words, inner_words = _words(outer), _words(inner)
    size = len(inner_words)
    if not size:
        return False
    return any(outer_words[i:i + size] == inner_words for i in range(len(outer_words) - size + 1))


def _edit_distance(a: str, b: str, bound: int) -> int:
    """Optimal string alignment distance, abandoned beyond `bound`."""
    if abs(len(a) - len(b)) > bound:
        return bound + 1
    a, b = a.lower(), b.lower()
    previous2: List[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > bound:
            return bound + 1
        previous2, previous = previous, current
    return previous[-1]


@dataclass
class Candidate:
    full_name: str
    origin: str
    note: str = ""
    rank: Tuple = ()


def _type_origin(index: NameIndex, full_name: str) -> Optional[str]:
    for known in index.types:
        if known.full_name == full_name:
            return known.origin
    return None


def _resolve_type(index: NameIndex, written: str) -> List[KnownType]:
    """The types a written name (simple or dotted) designates."""
    exact = [t for t in index.types if t.full_name == written]
    if exact:
        return exact
    simple = written.rsplit(".", 1)[-1]
    return [t for t in index.types if t.simple == simple and (t.full_name.endswith("." + written) or "." not in written)]


def _short(full_name: str) -> str:
    return full_name.rsplit(".", 1)[-1]


def _member_call(member: KnownMember) -> str:
    return _short(member.owner) + "." + member.name + ("()" if member.kind == "method" else "")


def suggest(unknown: UnknownName, index: NameIndex) -> Tuple[str, List[Candidate]]:
    """(lead sentence, candidates). The lead is set when the name is a MEMBER, not a type."""
    name = unknown.name
    lead = ""
    candidates: Dict[str, Candidate] = {}

    lead_type = ""

    def add(full_name: str, origin: str, note: str, rank: Tuple) -> None:
        current = candidates.get(full_name)
        if current is None or rank < current.rank:
            candidates[full_name] = Candidate(full_name, origin, note, rank)

    # (1) CS0426 'A.B': B is a member of A. Its return type, when known, IS the answer, and the
    # lead names it once ('... call SparkSession.Builder(), which returns 'X' (origin)').
    if unknown.code == "CS0426" and unknown.container:
        owners = {t.full_name for t in _resolve_type(index, unknown.container)}
        for member in index.members:
            if member.owner in owners and member.name == name:
                what = "a method" if member.kind == "method" else "a property"
                lead = "'" + unknown.written + "' is " + what + ", not a type"
                if member.kind == "method":
                    lead += ": call " + _member_call(member)
                if member.returns_type:
                    origin = _type_origin(index, member.returns_type) or member.origin
                    lead += (", which returns" if member.kind == "method" else ", of type")
                    lead += " '" + member.returns_type + "' (" + origin + ")"
                    lead_type = member.returns_type
                break

    if lead_type:
        return lead, []

    # (2) compound 'AB' where type A has a member B returning T.
    if unknown.code == "CS0246" or not lead:
        for member in index.members:
            if not member.returns_type or len(member.name) < 2:
                continue
            owner_simple = _short(member.owner)
            if name == owner_simple + member.name:
                origin = _type_origin(index, member.returns_type) or member.origin
                add(member.returns_type, origin, "Returned by " + _member_call(member) + ".", (1,))

    # (3) the exact simple name in another namespace.
    for known in index.types:
        if known.simple == name and known.full_name != unknown.written:
            note = "add 'using " + known.namespace + ";'" if known.namespace else ""
            add(known.full_name, known.origin, note, (2,))

    # (4) similarity.
    bound = 1 if len(name) <= 6 else 2
    for known in index.types:
        simple = known.simple
        if simple == name:
            continue
        if len(simple) >= MIN_CONTAINED_LENGTH and _contains_on_boundary(name, simple):
            suffix = name.endswith(simple)
            add(known.full_name, known.origin, "", (3, 0 if suffix else 1, -len(simple), known.full_name))
        elif len(name) >= MIN_CONTAINED_LENGTH and _contains_on_boundary(simple, name):
            add(known.full_name, known.origin, "", (4, len(simple), known.full_name))
        elif len(name) >= 4:
            distance = _edit_distance(name, simple, bound)
            if distance <= bound:
                add(known.full_name, known.origin, "", (5, distance, known.full_name))

    ordered = sorted(candidates.values(), key=lambda c: (c.rank, c.full_name))
    # A STRONG candidate (a member that returns the type, or the exact simple name elsewhere)
    # silences the similarity ones: measured on the target, 'SparkSessionBuilder' otherwise drew
    # 'SparkSession' and a workspace class 'Spark' after the right answer -- noise behind a hit.
    if ordered and ordered[0].rank[0] <= 2:
        ordered = [c for c in ordered if c.rank[0] <= 2]
    return lead, ordered[:MAX_CANDIDATES]


def render_hint(unknown: UnknownName, lead: str, candidates: List[Candidate]) -> str:
    place = Path(unknown.file.replace("\\", "/")).name + ":" + str(unknown.line)
    if unknown.occurrences > 1:
        place += ", +" + str(unknown.occurrences - 1) + " more"
    head = HINT_PREFIX + "'" + unknown.written + "' (" + unknown.code + ", " + place + ") -- "
    # One question per candidate, its note right after it: "did you mean 'A' (origin)? Returned
    # by X.Y(). Or 'B' (origin)? Add 'using N;'."
    parts: List[str] = []
    for candidate in candidates:
        text = "'" + candidate.full_name + "' (" + candidate.origin + ")?"
        note = candidate.note
        if note:
            note = note[0].upper() + note[1:]
            text += " " + note + ("" if note.endswith(".") else ".")
        parts.append(text)
    if lead and not parts:
        return head + lead + "."
    body = "did you mean " + " Or ".join(parts)
    return head + (lead + "; " if lead else "") + body


def build_hints(output: str, workspace: Optional[Path] = None) -> List[str]:
    """The 'help:' lines for the CS0246/CS0426/CS0234 errors of a compiler output; [] when none.

    Each faulty name is looked up in the workspace types and in the packages of ITS project (the
    bracket that ends the compiler line). A line without a bracket falls back to every .csproj
    of the workspace.
    """
    unknowns = parse_unknown_names(output)
    if not unknowns:
        return []
    workspace = Path(workspace) if workspace is not None else None
    local = workspace_index(workspace) if workspace is not None else NameIndex()
    fallback: Optional[List[Path]] = None
    per_project: Dict[str, NameIndex] = {}
    lines: List[str] = []
    for unknown in unknowns:
        if unknown.project:
            candidate = Path(unknown.project)
            if not candidate.is_absolute() and workspace is not None:
                candidate = workspace / candidate
            projects = [candidate]
        else:
            if fallback is None:
                fallback = (
                    sorted(
                        p for p in workspace.rglob("*.csproj")
                        if not (_SKIPPED_DIRS & {x.lower() for x in p.parts})
                    )[:64]
                    if workspace is not None
                    else []
                )
            projects = fallback
        key = "|".join(str(p) for p in projects)
        if key not in per_project:
            index = external_index_for(projects, workspace)
            index.extend(local)
            per_project[key] = index
        lead, candidates = suggest(unknown, per_project[key])
        if not candidates and not lead:
            continue
        lines.append(render_hint(unknown, lead, candidates))
    return lines

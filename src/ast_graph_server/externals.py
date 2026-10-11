"""External symbols: the CONTRACT of the NuGet packages a workspace's .csproj files reference.

Why this exists (measured, 03/10/2026): in 10 runs out of 55 the agent wrote C# naming the
Spark builder type under an invented name -- ``SparkSession.Builder`` (CS0426) or
``SparkSessionBuilder`` (CS0246) -- while the real type is ``Microsoft.Spark.Sql.Builder``.
The workspace never spells that name (``var builder = SparkSession.Builder()``), the type lives
in the NuGet package, and search_symbols answered nothing without saying the symbol was
external. Median cost: 12 repair turns per affected run.

What is indexed, and what is NOT:

* only the packages a ``<PackageReference Include=.. Version=..>`` of an indexed .csproj names
  (with ``Directory.Packages.props`` for central versions) -- never the .NET framework, never the
  transitive dependencies of a package;
* only what the package's XML documentation carries: types (``T:``), methods (``M:``) and
  properties (``P:``), with their ``<summary>``, their parameters and their ``<returns>`` text.
  The XML documentation carries NO return type and does not tell a class from an interface: a
  return type is shown only when the ``<returns>`` text names a type of the same package, and it
  is then labelled as inferred;
* the result is CONTRACT ONLY: there is no source, nothing here can be edited.

The rows live in tables of their own (``external_packages``, ``external_symbols``), never in
``symbols`` nor in the BM25 ``search_index``: every existing path (listing, Dossier, project map,
edit) keeps reading workspace rows only, and an external symbol reaches the agent through the
paths that were written for it. A package is re-parsed only when its XML files change (cache
keyed by package + version + the size and mtime of its XML files).
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("ast_graph_server.externals")

#: Kinds an external row can carry. They are NOT workspace kinds on purpose: a caller who
#: reads ``external_type`` cannot mistake it for a class it may open and edit.
EXTERNAL_KINDS = ("external_type", "external_method", "external_constructor", "external_property")

#: How many external matches one search may add after the workspace matches. The workspace
#: stays first; the externals are a complement, never a flood.
MAX_EXTERNAL_MATCHES = 5

#: Bounds that keep a pathological package from costing the index pass.
MAX_XML_BYTES = 40 * 1024 * 1024
MAX_MEMBERS_PER_PACKAGE = 60000

#: Target frameworks tried in this order when a package ships several.
_TFM_PREFERENCE = (
    "netstandard2.0",
    "netstandard2.1",
    "net8.0",
    "net9.0",
    "net7.0",
    "net6.0",
    "net5.0",
    "netcoreapp3.1",
    "netstandard1.6",
    "netstandard1.3",
    "net48",
    "net472",
    "net462",
    "net461",
    "net45",
)

_ALIASES = {
    "System.String": "string",
    "System.Int32": "int",
    "System.Int64": "long",
    "System.Int16": "short",
    "System.UInt32": "uint",
    "System.UInt64": "ulong",
    "System.UInt16": "ushort",
    "System.Byte": "byte",
    "System.SByte": "sbyte",
    "System.Boolean": "bool",
    "System.Double": "double",
    "System.Single": "float",
    "System.Decimal": "decimal",
    "System.Char": "char",
    "System.Object": "object",
    "System.Void": "void",
}

#: Namespaces whose prefix is dropped in a rendered parameter type (IEnumerable<string>
#: reads better than System.Collections.Generic.IEnumerable<System.String>). Every other
#: namespace is kept: it is the name the caller has to write, or import.
_SHORTENED_NAMESPACES = (
    "System.Collections.Generic.",
    "System.Threading.Tasks.",
    "System.Collections.",
    "System.",
)


def external_symbols_enabled() -> bool:
    """SCALPEL_EXTERNAL_SYMBOLS=0 turns the whole feature off (default: on)."""
    return os.environ.get("SCALPEL_EXTERNAL_SYMBOLS", "1").strip().lower() not in ("0", "false", "no", "off")


def nuget_packages_root() -> Path:
    """The global packages folder: %NUGET_PACKAGES%, else ~/.nuget/packages."""
    configured = os.environ.get("NUGET_PACKAGES")
    if configured:
        return Path(configured)
    return Path.home() / ".nuget" / "packages"


# ----------------------------------------------------------------------------------------
# .csproj reading
# ----------------------------------------------------------------------------------------


def _local(tag: str) -> str:
    """'{http://schemas.microsoft.com/developer/msbuild/2003}PackageReference' -> 'PackageReference'."""
    return tag.rsplit("}", 1)[-1]


def _read_xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError, ValueError):
        return None


def _child_text(element: ET.Element, name: str) -> Optional[str]:
    for child in element:
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def central_package_versions(csproj: Path, stop_at: Optional[Path] = None) -> Dict[str, str]:
    """Versions a Directory.Packages.props above the project declares (central management).

    The nearest file wins, as in MSBuild; the search stops at ``stop_at`` (the workspace root).
    Keys are lower-cased package ids.
    """
    versions: Dict[str, str] = {}
    current = csproj.parent
    stop = stop_at.resolve() if stop_at is not None else None
    while True:
        props = current / "Directory.Packages.props"
        if props.is_file():
            root = _read_xml(props)
            if root is not None:
                for element in root.iter():
                    if _local(element.tag) != "PackageVersion":
                        continue
                    name = element.get("Include") or element.get("Update")
                    version = element.get("Version") or _child_text(element, "Version")
                    if name and version:
                        versions.setdefault(name.strip().lower(), version.strip())
            break
        if stop is not None and current.resolve() == stop:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return versions


def parse_package_references(csproj: Path, stop_at: Optional[Path] = None) -> List[Tuple[str, Optional[str]]]:
    """(package id, version or None) for every <PackageReference Include=...> of a project.

    ``Update=`` items modify a reference declared elsewhere and are not references themselves.
    A version written as an MSBuild property ($(SparkVersion)) cannot be resolved here and is
    returned as None -- the caller reports it as unresolved rather than guessing.
    """
    root = _read_xml(csproj)
    if root is None:
        return []
    central: Optional[Dict[str, str]] = None
    references: List[Tuple[str, Optional[str]]] = []
    for element in root.iter():
        if _local(element.tag) != "PackageReference":
            continue
        name = (element.get("Include") or "").strip()
        if not name or "$(" in name:
            continue
        version = (
            element.get("Version")
            or element.get("VersionOverride")
            or _child_text(element, "Version")
            or _child_text(element, "VersionOverride")
        )
        if not version:
            if central is None:
                central = central_package_versions(csproj, stop_at)
            version = central.get(name.lower())
        if version and "$(" in version:
            version = None
        references.append((name, version.strip() if version else None))
    return references


# ----------------------------------------------------------------------------------------
# Package location
# ----------------------------------------------------------------------------------------


def _version_key(text: str) -> Tuple:
    """A sortable key for '2.3.0', '10.0.3', '1.0.0-preview.1' (a pre-release sorts first)."""
    core, _, pre = text.partition("-")
    numbers: List[int] = []
    for part in core.split("."):
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(-1)
    while len(numbers) < 4:
        numbers.append(0)
    return (tuple(numbers), 0 if pre else 1, pre)


def resolve_package_dir(root: Path, package: str, version: Optional[str]) -> Optional[Path]:
    """The installed folder of package/version in the global packages folder, or None.

    An exact version folder wins. Otherwise the NuGet rule for a bare version (a MINIMUM) is
    applied to what is installed: the lowest installed version >= it. A floating version
    ('2.*') takes the highest installed version with that prefix, and an exact range '[2.3.0]'
    is read as its bound. Nothing is downloaded: a package that was never restored is absent.
    """
    base = root / package.lower()
    if not base.is_dir():
        return None
    if version:
        cleaned = version.strip()
        exact = cleaned.strip("[]() ")
        if "," not in exact and "*" not in exact:
            for candidate in (exact, exact.lower()):
                if (base / candidate).is_dir():
                    return base / candidate
    try:
        installed = sorted((child.name for child in base.iterdir() if child.is_dir()), key=_version_key)
    except OSError:
        return None
    if not installed:
        return None
    if not version:
        return base / installed[-1]
    cleaned = version.strip()
    if "*" in cleaned:
        prefix = cleaned.split("*", 1)[0].lower()
        matching = [name for name in installed if name.lower().startswith(prefix)]
        return base / matching[-1] if matching else None
    lower = cleaned.strip("[]() ").split(",", 1)[0].strip()
    if not lower:
        return base / installed[-1]
    floor = _version_key(lower)
    for name in installed:
        if _version_key(name) >= floor:
            return base / name
    return None


def find_doc_xmls(package_dir: Path) -> List[Path]:
    """The XML documentation files of ONE target framework of an installed package."""
    for folder in ("lib", "ref"):
        container = package_dir / folder
        if not container.is_dir():
            continue
        try:
            frameworks = {child.name.lower(): child for child in container.iterdir() if child.is_dir()}
        except OSError:
            continue
        ordered = [frameworks[name] for name in _TFM_PREFERENCE if name in frameworks]
        ordered += [frameworks[name] for name in sorted(frameworks) if frameworks[name] not in ordered]
        for framework in ordered:
            try:
                xmls = sorted(path for path in framework.glob("*.xml") if path.is_file())
            except OSError:
                continue
            if xmls:
                return xmls
    return []


# ----------------------------------------------------------------------------------------
# XML documentation parsing
# ----------------------------------------------------------------------------------------


@dataclass
class ExternalSymbol:
    doc_id: str
    kind: str
    name: str
    full_name: str
    parent: Optional[str]
    signature: str
    summary: str = ""
    returns: str = ""
    returns_type: str = ""
    returns_basis: str = ""
    params: List[str] = field(default_factory=list)


def _split_top_level(text: str) -> List[str]:
    """Split 'A,B{C,D},E[0:,0:]' on the commas that are not nested."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in text:
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current or parts:
        parts.append("".join(current))
    return [part.strip() for part in parts]


def _short_type(name: str) -> str:
    if name in _ALIASES:
        return _ALIASES[name]
    for prefix in _SHORTENED_NAMESPACES:
        if name.startswith(prefix) and "." not in name[len(prefix):]:
            return name[len(prefix):]
    return name


def render_doc_type(text: str, type_params: Sequence[str] = (), method_params: Sequence[str] = ()) -> str:
    """'System.Collections.Generic.IEnumerable{System.String}' -> 'IEnumerable<string>'."""
    text = text.strip()
    suffix = ""
    if text.endswith("@"):
        text = text[:-1]
        suffix = ""
        prefix = "ref "
    else:
        prefix = ""
    while text.endswith("*"):
        text = text[:-1]
        suffix = "*" + suffix
    while text.endswith("]"):
        start = text.rfind("[")
        if start < 0:
            break
        commas = text[start:].count(",")
        suffix = "[" + "," * commas + "]" + suffix
        text = text[:start]
    if text.startswith("``"):
        index = text[2:]
        try:
            core = method_params[int(index)]
        except (ValueError, IndexError):
            core = "TM" + index
        return prefix + core + suffix
    if text.startswith("`"):
        index = text[1:]
        try:
            core = type_params[int(index)]
        except (ValueError, IndexError):
            core = "T" + index
        return prefix + core + suffix
    if "{" in text and text.endswith("}"):
        head = text[: text.index("{")]
        inner = text[text.index("{") + 1 : -1]
        arguments = [render_doc_type(part, type_params, method_params) for part in _split_top_level(inner)]
        return prefix + _short_type(head) + "<" + ", ".join(arguments) + ">" + suffix
    return prefix + _short_type(text) + suffix


def _strip_arity(name: str) -> Tuple[str, int]:
    """'Foo`2' -> ('Foo', 2); 'Bar``1' -> ('Bar', 1); 'Baz' -> ('Baz', 0)."""
    match = re.match(r"^(.*?)`{1,2}(\d+)$", name)
    if match:
        return match.group(1), int(match.group(2))
    return name, 0


def _clean_dotted(name: str) -> str:
    """Remove generic arities from every segment: 'A.B`1.C' -> 'A.B.C'."""
    return re.sub(r"`{1,2}\d+", "", name)


def _inner_text(element: Optional[ET.Element]) -> str:
    """The prose of a doc element, with <see cref=...> rendered as the short name it points at."""
    if element is None:
        return ""
    pieces: List[str] = [element.text or ""]
    for child in element:
        tag = _local(child.tag)
        if tag in ("see", "seealso"):
            cref = child.get("cref") or ""
            if cref:
                pieces.append(_clean_dotted(cref.split(":", 1)[-1].split("(", 1)[0]))
            elif child.get("langword"):
                pieces.append(child.get("langword") or "")
            else:
                pieces.append(_inner_text(child))
        elif tag in ("paramref", "typeparamref"):
            pieces.append(child.get("name") or "")
        else:
            pieces.append(_inner_text(child))
        pieces.append(child.tail or "")
    return " ".join("".join(pieces).split())


def _first_cref_type(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    for child in element.iter():
        cref = child.get("cref") or ""
        if cref.startswith("T:"):
            return _clean_dotted(cref[2:])
    return ""


def _member_symbol(doc_id: str, member: ET.Element) -> Optional[ExternalSymbol]:
    prefix, _, rest = doc_id.partition(":")
    if prefix not in ("T", "M", "P") or not rest:
        return None
    summary = _inner_text(member.find("summary"))
    returns_element = member.find("returns")
    returns = _inner_text(returns_element)
    type_param_names = [element.get("name") or "" for element in member.findall("typeparam")]
    param_names = [element.get("name") or "" for element in member.findall("param")]

    if prefix == "T":
        full = _clean_dotted(rest)
        simple, arity = _strip_arity(rest.rsplit(".", 1)[-1])
        names = type_param_names or ["T" + str(i + 1) for i in range(arity)] if arity else []
        rendered = full + ("<" + ", ".join(names) + ">" if arity else "")
        parent = full.rsplit(".", 1)[0] if "." in full else None
        return ExternalSymbol(
            doc_id=doc_id,
            kind="external_type",
            name=simple,
            full_name=full,
            parent=parent,
            signature="type " + rendered,
            summary=summary,
        )

    head, _, arguments = rest.partition("(")
    arguments = arguments[:-1] if arguments.endswith(")") else arguments
    conversion = ""
    if "~" in arguments:
        arguments, _, conversion = arguments.partition("~")
        arguments = arguments.rstrip(")")
    elif "~" in head:
        head, _, conversion = head.partition("~")
    owner_raw, _, member_raw = head.rpartition(".")
    owner = _clean_dotted(owner_raw)
    member_name, method_arity = _strip_arity(member_raw)
    method_type_params = type_param_names if method_arity else []
    if method_arity and not method_type_params:
        method_type_params = ["T" + str(i + 1) for i in range(method_arity)]
    owner_arity = _strip_arity(owner_raw.rsplit(".", 1)[-1])[1]
    owner_type_params = ["T" + str(i + 1) for i in range(owner_arity)] if owner_arity else []
    rendered_args: List[str] = []
    if arguments:
        for index, raw in enumerate(_split_top_level(arguments)):
            rendered = render_doc_type(raw, owner_type_params, method_type_params)
            if index < len(param_names) and param_names[index]:
                rendered += " " + param_names[index]
            rendered_args.append(rendered)
    generic = "<" + ", ".join(method_type_params) + ">" if method_arity else ""

    if prefix == "P":
        kind = "external_property"
        if arguments:
            signature = owner + ".this[" + ", ".join(rendered_args) + "]"
        else:
            signature = owner + "." + member_name
        return ExternalSymbol(
            doc_id=doc_id,
            kind=kind,
            name=member_name,
            full_name=owner + "." + member_name,
            parent=owner,
            signature=signature,
            summary=summary,
            returns=returns or _inner_text(member.find("value")),
        )

    if member_name in ("#ctor", "#cctor"):
        type_simple = owner.rsplit(".", 1)[-1]
        return ExternalSymbol(
            doc_id=doc_id,
            kind="external_constructor",
            name=type_simple,
            full_name=owner + "." + type_simple,
            parent=owner,
            signature="new " + owner + "(" + ", ".join(rendered_args) + ")",
            summary=summary,
            params=param_names,
        )
    signature = owner + "." + member_name + generic + "(" + ", ".join(rendered_args) + ")"
    returns_type = ""
    basis = ""
    if conversion:
        returns_type = render_doc_type(conversion, owner_type_params, method_type_params)
        basis = "signature"
    else:
        cref = _first_cref_type(returns_element)
        if cref:
            returns_type, basis = cref, "cref"
    return ExternalSymbol(
        doc_id=doc_id,
        kind="external_method",
        name=member_name,
        full_name=owner + "." + member_name,
        parent=owner,
        signature=signature,
        summary=summary,
        returns=returns,
        returns_type=returns_type,
        returns_basis=basis,
        params=param_names,
    )


def parse_doc_xml(path: Path, limit: int = MAX_MEMBERS_PER_PACKAGE) -> List[ExternalSymbol]:
    """Every T:/M:/P: member of one XML documentation file (F: and E: are skipped)."""
    try:
        if path.stat().st_size > MAX_XML_BYTES:
            logger.info("Skipping %s: larger than %d bytes", path, MAX_XML_BYTES)
            return []
    except OSError:
        return []
    symbols: List[ExternalSymbol] = []
    try:
        for _event, element in ET.iterparse(str(path), events=("end",)):
            if _local(element.tag) != "member":
                continue
            doc_id = element.get("name") or ""
            symbol = _member_symbol(doc_id, element)
            element.clear()
            if symbol is not None:
                symbols.append(symbol)
                if len(symbols) >= limit:
                    break
    except (ET.ParseError, OSError, ValueError) as exc:
        logger.info("Could not parse %s: %s", path, exc)
    return symbols


def infer_return_types(symbols: List[ExternalSymbol]) -> None:
    """Fill returns_type from the <returns> PROSE when it names exactly one type of the package.

    'Creates a Builder object' -> Microsoft.Spark.Sql.Builder, but only when one type of the
    package carries that simple name: an ambiguous word infers nothing. Labelled 'doc-word', so
    the rendering can say it was inferred and not read from a signature.
    """
    by_simple: Dict[str, List[str]] = {}
    for symbol in symbols:
        if symbol.kind == "external_type":
            by_simple.setdefault(symbol.name, []).append(symbol.full_name)
    for symbol in symbols:
        if symbol.kind != "external_method" or symbol.returns_type or not symbol.returns:
            continue
        found: List[str] = []
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", symbol.returns):
            candidates = by_simple.get(word) or []
            if len(candidates) == 1 and candidates[0] not in found:
                found.append(candidates[0])
        if len(found) == 1:
            symbol.returns_type = found[0]
            symbol.returns_basis = "doc-word"


# ----------------------------------------------------------------------------------------
# Storage and queries
# ----------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_packages (
    package TEXT NOT NULL,
    version TEXT NOT NULL,
    source TEXT NOT NULL,
    stamp TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    PRIMARY KEY (package, version)
);
CREATE TABLE IF NOT EXISTS external_symbols (
    package TEXT NOT NULL,
    version TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    parent TEXT,
    signature TEXT NOT NULL,
    summary TEXT,
    returns TEXT,
    returns_type TEXT,
    returns_basis TEXT,
    search_key TEXT NOT NULL,
    PRIMARY KEY (package, version, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_external_full ON external_symbols(full_name);
CREATE INDEX IF NOT EXISTS idx_external_parent ON external_symbols(parent);
"""


def _search_key(full_name: str) -> str:
    """The dotted name AND its dot-less form: 'SparkSessionBuilder' finds SparkSession.Builder."""
    lowered = full_name.lower()
    return lowered + " " + lowered.replace(".", "") + " " + ".".join(lowered.split(".")[-2:]).replace(".", "")


def _kinds_for(kind: Optional[str]) -> Optional[Tuple[str, ...]]:
    """The external kinds a workspace `kind` filter admits; None means every kind, () none."""
    if not kind or kind == "all":
        return None
    mapping = {
        "class": ("external_type",),
        "interface": ("external_type",),
        "struct": ("external_type",),
        "record": ("external_type",),
        "enum": ("external_type",),
        "method": ("external_method",),
        "function": ("external_method",),
        "constructor": ("external_constructor",),
        "property": ("external_property",),
    }
    return mapping.get(kind, ())


_KIND_RANK = {"external_type": 0, "external_method": 1, "external_property": 2, "external_constructor": 3}


class ExternalIndex:
    """The external rows of one database: refreshed from .csproj files, queried by the server."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self.last_stats: Dict[str, Any] = {}
        with self.db.get_connection() as conn:
            conn.executescript(_SCHEMA)

    # -- refresh ---------------------------------------------------------------------

    def clear(self) -> None:
        with self.db.get_connection() as conn:
            with conn:
                conn.execute("DELETE FROM external_symbols")
                conn.execute("DELETE FROM external_packages")

    def refresh(self, csproj_files: Iterable[Path], workspace_root: Optional[Path] = None) -> Dict[str, Any]:
        """Bring the external rows in line with the packages the projects reference now."""
        started = time.perf_counter()
        root = nuget_packages_root()
        wanted: Dict[Tuple[str, str], Path] = {}
        unresolved: List[str] = []
        missing: List[str] = []
        references = 0
        for csproj in sorted(set(csproj_files)):
            for package, version in parse_package_references(csproj, workspace_root):
                references += 1
                folder = resolve_package_dir(root, package, version)
                if folder is None:
                    label = package + " " + (version or "?")
                    (missing if version else unresolved).append(label)
                    continue
                wanted.setdefault((package, folder.name), folder)
        indexed: List[str] = []
        cached: List[str] = []
        without_docs: List[str] = []
        with self.db.get_connection() as conn:
            stored = {
                (row["package"], row["version"]): row["stamp"]
                for row in conn.execute("SELECT package, version, stamp FROM external_packages")
            }
        # One identity per package id and version, whatever the casing the projects use.
        canonical: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for package, version in wanted:
            canonical.setdefault((package.lower(), version.lower()), (package, version))
        stored_by_lower = {(p.lower(), v.lower()): (p, v) for p, v in stored}
        for key, (package, version) in sorted(canonical.items()):
            folder = wanted[(package, version)]
            xmls = find_doc_xmls(folder)
            if not xmls:
                without_docs.append(package + " " + version)
            stamp = ";".join(
                str(path.name) + ":" + str(path.stat().st_size) + ":" + str(path.stat().st_mtime_ns)
                for path in xmls
            ) or "no-xml"
            previous = stored_by_lower.get(key)
            if previous is not None and stored.get(previous) == stamp:
                cached.append(package + " " + version)
                continue
            symbols: List[ExternalSymbol] = []
            for xml in xmls:
                symbols.extend(parse_doc_xml(xml, MAX_MEMBERS_PER_PACKAGE - len(symbols)))
                if len(symbols) >= MAX_MEMBERS_PER_PACKAGE:
                    break
            infer_return_types(symbols)
            self._store(previous, package, version, str(xmls[0].parent) if xmls else str(folder), stamp, symbols)
            indexed.append(package + " " + version + " (" + str(len(symbols)) + " members)")
        keep = set(canonical)
        with self.db.get_connection() as conn:
            with conn:
                for package, version in stored:
                    if (package.lower(), version.lower()) not in keep:
                        conn.execute(
                            "DELETE FROM external_symbols WHERE package = ? AND version = ?", (package, version)
                        )
                        conn.execute(
                            "DELETE FROM external_packages WHERE package = ? AND version = ?", (package, version)
                        )
        self.last_stats = {
            "nuget_root": str(root),
            "references": references,
            "packages": len(canonical),
            "indexed": indexed,
            "cached": cached,
            "missing": sorted(set(missing)),
            "unresolved": sorted(set(unresolved)),
            "without_docs": without_docs,
            "seconds": round(time.perf_counter() - started, 3),
        }
        logger.info(
            "External symbols: %d package(s), %d parsed, %d cached, %d missing in %.3fs",
            len(canonical),
            len(indexed),
            len(cached),
            len(self.last_stats["missing"]),
            self.last_stats["seconds"],
        )
        return self.last_stats

    def _store(
        self,
        previous: Optional[Tuple[str, str]],
        package: str,
        version: str,
        source: str,
        stamp: str,
        symbols: List[ExternalSymbol],
    ) -> None:
        with self.db.get_connection() as conn:
            with conn:
                if previous is not None:
                    conn.execute(
                        "DELETE FROM external_symbols WHERE package = ? AND version = ?", previous
                    )
                    conn.execute(
                        "DELETE FROM external_packages WHERE package = ? AND version = ?", previous
                    )
                conn.executemany(
                    "INSERT OR REPLACE INTO external_symbols (package, version, doc_id, kind, name, full_name, "
                    "parent, signature, summary, returns, returns_type, returns_basis, search_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            package,
                            version,
                            s.doc_id,
                            s.kind,
                            s.name,
                            s.full_name,
                            s.parent,
                            s.signature,
                            s.summary,
                            s.returns,
                            s.returns_type,
                            s.returns_basis,
                            _search_key(s.full_name),
                        )
                        for s in symbols
                    ],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO external_packages (package, version, source, stamp, member_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (package, version, source, stamp, len(symbols)),
                )

    # -- queries ---------------------------------------------------------------------

    def count(self) -> int:
        with self.db.get_connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM external_symbols").fetchone()[0])

    def _rows(self, where: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM external_symbols WHERE " + where, list(params)).fetchall()
        return [dict(row) for row in rows]

    def search(
        self,
        terms: Sequence[str],
        kind: Optional[str] = "all",
        limit: int = MAX_EXTERNAL_MATCHES,
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        """Up to ``limit`` external matches, overloads grouped, best first.

        Ranking, in order: how many query terms the dotted name carries; how many terms ARE the
        simple name or the declaring type's simple name; types before members; shorter names.
        A method whose return type is known brings that type along (it is the name the caller
        is about to write), within the same cap. ``strict`` (used when the workspace already
        answered) keeps only the rows that carry EVERY term or that a term names exactly.
        """
        if not terms or limit <= 0:
            return []
        kinds = _kinds_for(kind)
        if kinds == ():
            return []
        where = "(" + " OR ".join("search_key LIKE ?" for _ in terms) + ")"
        params: List[Any] = ["%" + term + "%" for term in terms]
        if kinds is not None:
            where += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params += list(kinds)
        rows = self._rows(where, params)
        if not rows:
            return []

        def score(row: Dict[str, Any]) -> Tuple:
            key = row["search_key"]
            hits = sum(1 for term in terms if term in key)
            simple = row["name"].lower()
            owner = (row["parent"] or "").rsplit(".", 1)[-1].lower()
            exact = sum(1 for term in terms if term == simple or (row["kind"] != "external_type" and term == owner))
            return (-hits, -exact, _KIND_RANK.get(row["kind"], 9), len(row["full_name"]), row["full_name"])

        rows.sort(key=score)
        if strict:
            rows = [row for row in rows if score(row)[0] == -len(terms) or score(row)[1] < 0]
        groups = self._group(rows)
        chosen: List[Dict[str, Any]] = []
        seen: set = set()
        for group in groups:
            if len(chosen) >= limit:
                break
            identity = (group["kind"], group["symbol_id"])
            if identity in seen:
                continue
            seen.add(identity)
            chosen.append(group)
            companions: List[Tuple[str, str]] = []
            returned = group.get("returns_type")
            if returned:
                companions.append((returned, "returns it"))
            # The declaring type a query term NAMES ('Builder Config' -> Builder): it is the type
            # the caller has to write, and it would otherwise lose to longer names that merely
            # contain both terms (measured: YamlDotNet's SerializerBuilder.Configure* on the target).
            owner = group["symbol_id"].rsplit(".", 1)[0] if group["kind"] != "external_type" else ""
            if owner and owner.rsplit(".", 1)[-1].lower() in terms:
                companions.append((owner, "declares it"))
            for type_name, reason in companions:
                if len(chosen) >= limit or ("external_type", type_name) in seen:
                    continue
                if kinds is not None and "external_type" not in kinds:
                    continue
                types = [entry for entry in self.lookup(type_name) if entry["kind"] == "external_type"]
                if types:
                    seen.add(("external_type", type_name))
                    types[0]["brought_by"] = group["symbol_id"]
                    types[0]["brought_reason"] = reason
                    chosen.append(types[0])
        for entry in chosen:
            if entry["kind"] == "external_type":
                entry["members"] = self._member_names(entry["symbol_id"])
        return chosen

    @staticmethod
    def _group(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One entry per (kind, full name): overloads and package versions folded together."""
        order: List[Tuple[str, str]] = []
        grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in rows:
            key = (row["kind"], row["full_name"])
            entry = grouped.get(key)
            if entry is None:
                entry = {
                    "symbol_id": row["full_name"],
                    "displayed_id": row["full_name"],
                    "kind": row["kind"],
                    "external": True,
                    "editable": False,
                    "package": row["package"],
                    "versions": [row["version"]],
                    "signatures": [],
                    "summary": row["summary"] or "",
                    "returns": row["returns"] or "",
                    "returns_type": row["returns_type"] or "",
                    "returns_basis": row["returns_basis"] or "",
                    "doc_ids": [],
                }
                grouped[key] = entry
                order.append(key)
            if row["version"] not in entry["versions"]:
                entry["versions"].append(row["version"])
            if row["signature"] not in entry["signatures"]:
                entry["signatures"].append(row["signature"])
            if row["doc_id"] not in entry["doc_ids"]:
                entry["doc_ids"].append(row["doc_id"])
            if not entry["returns_type"] and row["returns_type"]:
                entry["returns_type"] = row["returns_type"]
                entry["returns_basis"] = row["returns_basis"] or ""
        result = []
        for key in order:
            entry = grouped[key]
            entry["signature"] = entry["signatures"][0] if entry["signatures"] else ""
            entry["origin"] = external_origin(entry["package"], entry["versions"])
            result.append(entry)
        return result

    def _member_names(self, type_name: str, limit: int = 40) -> List[str]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT name, kind FROM external_symbols WHERE parent = ? AND kind != 'external_type' "
                "ORDER BY kind, name",
                (type_name,),
            ).fetchall()
        names: List[str] = []
        for row in rows:
            label = row["name"] + ("()" if row["kind"] in ("external_method", "external_constructor") else "")
            if row["kind"] == "external_constructor":
                label = "new " + label
            if label not in names:
                names.append(label)
        return names[:limit] + (["... +" + str(len(names) - limit) + " more"] if len(names) > limit else [])

    def lookup(self, address: str) -> List[Dict[str, Any]]:
        """The grouped entries an address names: 'Ns.Type', 'Ns.Type.Member', 'T:Ns.Type', 'Ns.Type.M(x)'."""
        if not address:
            return []
        text = address.strip()
        if len(text) > 2 and text[1] == ":" and text[0] in "TMPFE":
            text = text[2:]
        text = _clean_dotted(text.split("(", 1)[0]).strip()
        if not text or "/" in text or "\\" in text or "::" in text or "." not in text:
            return []
        rows = self._rows("full_name = ?", (text,))
        if not rows:
            rows = self._rows("full_name = ? COLLATE NOCASE", (text,))
        rows.sort(key=lambda row: (_KIND_RANK.get(row["kind"], 9), row["doc_id"]))
        return self._group(rows)

    def members_of(self, type_name: str) -> List[Dict[str, Any]]:
        """Every member of a type, ONE ENTRY PER OVERLOAD so each signature keeps its own summary."""
        rows = self._rows("parent = ? AND kind != 'external_type'", (type_name,))
        rows.sort(key=lambda row: (_KIND_RANK.get(row["kind"], 9), row["name"], row["doc_id"]))
        entries: List[Dict[str, Any]] = []
        seen: set = set()
        for row in rows:
            if row["doc_id"] in seen:  # the same member in two versions of the package
                continue
            seen.add(row["doc_id"])
            entries.extend(self._group([row]))
        return entries


def external_origin(package: str, versions: Sequence[str]) -> str:
    return "external -- package " + package + " " + ", ".join(versions) + ", contract only, not editable"


def external_query_terms(query: str) -> List[str]:
    """The same terms the workspace search matches on (database.query_terms), parentheses dropped."""
    from .database import query_terms  # local import: database does not import this module

    terms = query_terms((query or "").replace("(", " ").replace(")", " "))
    if not terms and (query or "").strip():
        terms = [(query or "").strip().lower()]
    return terms


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _returns_line(entry: Dict[str, Any]) -> str:
    returned = entry.get("returns_type") or ""
    prose = _first_sentence(entry.get("returns") or "", 160)
    if not returned and not prose:
        return ""
    line = "  returns: "
    if returned:
        basis = entry.get("returns_basis") or ""
        if basis == "doc-word":
            line += returned + " (inferred from the <returns> doc text, which reads: '" + prose + "')"
        else:
            line += returned + (" -- " + prose if prose else "")
    else:
        line += prose + " (the XML doc names no type)"
    return line


EXTERNAL_SECTION_HEADER = (
    "external symbols (NuGet packages the .csproj files reference; CONTRACT ONLY, read from the "
    "package XML documentation: no source, not editable; the XML doc carries no return type, so "
    "one is shown only when the doc text names it):"
)


def render_external_matches(entries: List[Dict[str, Any]]) -> str:
    """The text block search_symbols appends after the workspace matches."""
    if not entries:
        return ""
    lines = [EXTERNAL_SECTION_HEADER]
    for entry in entries:
        signatures = entry.get("signatures") or [entry.get("signature") or ""]
        head = entry["displayed_id"] + " [" + entry["kind"] + "] :: " + signatures[0]
        lines.append(head + "  -- " + entry["origin"])
        for other in signatures[1:6]:
            lines.append("  overload: " + other)
        if len(signatures) > 6:
            lines.append("  ... +" + str(len(signatures) - 6) + " more overload(s): read_block(path='" + entry["displayed_id"] + "')")
        summary = _first_sentence(entry.get("summary") or "")
        if summary:
            lines.append("  doc: " + summary)
        returns = _returns_line(entry)
        if returns:
            lines.append(returns)
        if entry.get("brought_by"):
            lines.append("  (listed because " + entry["brought_by"] + " " + entry.get("brought_reason", "returns it") + ")")
        members = entry.get("members") or []
        if members:
            lines.append("  members: " + ", ".join(members))
    return "\n".join(lines)


def render_external_contract(address: str, entries: List[Dict[str, Any]], members: List[Dict[str, Any]]) -> str:
    """What read_block answers for an external address: the contract, never a source text."""
    first = entries[0]
    lines = [
        address + " -- " + first["origin"] + ". This is the CONTRACT read from the package's XML "
        "documentation: there is no source text, no line numbers and no hash, and edit_block / "
        "insert_block / delete_block refuse it. Use it to write the code that CALLS it."
    ]
    for entry in entries:
        lines.append("")
        lines.append("[" + entry["kind"] + "] " + entry["displayed_id"])
        for signature in entry.get("signatures") or [entry.get("signature") or ""]:
            lines.append("  " + signature)
        if entry.get("summary"):
            lines.append("  doc: " + _first_sentence(entry["summary"], 600))
        returns = _returns_line(entry)
        if returns:
            lines.append(returns)
    if members:
        lines.append("")
        lines.append("members (" + str(len(members)) + "):")
        detailed = len(members) <= 80
        for member in members:
            for signature in member.get("signatures") or [member.get("signature") or ""]:
                lines.append("  [" + member["kind"] + "] " + signature)
            if detailed and member.get("summary"):
                lines.append("      " + _first_sentence(member["summary"], 140))
    return "\n".join(lines)

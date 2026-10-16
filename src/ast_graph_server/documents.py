"""Document indexing for the project ontology.

Extracts the title and the paragraph (section) headings of Markdown, Word and
PDF documents so an agent can discover and jump straight to a paragraph without
reading an entire file.
"""

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CORE_NS = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


@dataclass
class DocumentSection:
    """One paragraph / section of a document."""

    heading: str
    level: int
    content: str
    order_index: int
    start_offset: int = 0
    end_offset: int = 0


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

#: A fenced code block: ``` or ~~~, three or more. What is inside it is code, so a
#: "#" there is a comment, never a heading.
_FENCE = re.compile(r"^(`{3,}|~{3,})")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 60) -> str:
    """A stable, readable identifier fragment: accents folded, punctuation to '-'."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return _SLUG_RE.sub("-", ascii_text).strip("-")[:max_length].strip("-")


#: The number a human gives a paragraph: "9. ", "10) ". It is assigned, moved and
#: corrected by the tools, so it is structure -- never part of the name. A version
#: heading like "1.3.2-dev" is not a paragraph number: the dot is followed by a
#: digit, not by a space.
_SECTION_NUMBER = re.compile(r"^\d+[.)]\s+")


def section_title(heading: str) -> str:
    """The title of a heading, without the paragraph number in front of it."""
    return _SECTION_NUMBER.sub("", heading or "", count=1)


def stable_section_ids(file_path: str, sections: List["DocumentSection"]) -> List[str]:
    """Deterministic section identifiers, independent of a section's position.

    `file#<order_index>` shifts every later identifier the moment a paragraph is
    inserted or removed, so an id read before an edit can silently designate a
    different paragraph after it. Measured on 25 journals: 22 runs renumbered
    identifiers mid-run, 533 times. Keying on the heading instead is invariant
    under insertion, deletion, reordering and content edits; the id changes only
    when the heading changes, which is a real change of identity.

    An identifier is the PATH of the headings that hold the paragraph, because a
    bare occurrence suffix is positional and silently re-points: after an agent
    deleted its own duplicate under the [1.3.2-dev] heading, "#new-features-2"
    designated the [1.3.1] paragraph (declared on mapab2 b r1 of the base edfe80a:
    "the id is positional, not stable"). The path does not move when a sibling
    disappears, so an id either still names the same paragraph or names nothing, and
    a missing name is answered with its closest neighbour. The top-level heading is
    the document itself, so it is not repeated in the path: the file part names it.

    The paragraph number is NOT part of the title, so it is not part of the id
    either: renumbering one paragraph moved the identifiers of every paragraph
    after it, and an identifier like `#9-archive-scratchpad-hygiene` reads like a
    position, which is exactly the confusion that made a run write into another
    section (the table stores a name next to an order_index).
    """
    bases = [
        slugify(section_title(section.heading))
        or slugify((section.content or "")[:80])
        or ("section-" + str(index))
        for index, section in enumerate(sections)
    ]

    used: Dict[Tuple[str, str], int] = {}
    stack: List[Tuple[int, str]] = []
    identifiers: List[str] = []
    for section, base in zip(sections, bases):
        level = int(section.level or 1)
        while stack and stack[-1][0] >= level:
            stack.pop()
        # The fragment is the PATH of the headings, so it depends on the paragraph's
        # own place in the outline and on nothing else: the bare occurrence suffix was
        # positional, and after an agent deleted its own duplicate under [1.3.2-dev],
        # "#new-features-2" designated the [1.3.1] paragraph. A path does not move when
        # a sibling disappears, so a name either still names the same paragraph or
        # names nothing.
        is_top_level = not stack
        parent = stack[-1][1] if stack else ""
        count = used.get((parent, base), 0) + 1
        used[(parent, base)] = count
        suffix = "" if count == 1 else "-" + str(count)
        fragment = parent + "/" + base + suffix if parent else base + suffix
        identifiers.append(file_path + "#" + fragment)
        # A top-level heading is the document itself: the file part already names it,
        # so it adds no segment to the path of its children.
        stack.append((level, "" if is_top_level else fragment))
    return identifiers


def heading_lines(lines: List[str]) -> List[Tuple[int, int, str, str]]:
    """(line number, level, title, raw line) of the real headings of a Markdown text.

    A `#` inside a fenced code block is a comment, not a heading. Reading it as one
    gave `DataLinq/docs/LINQ-to-Spark.md` two fake level-1 sections -- the four `#`
    lines of its bash blocks -- which swallowed every real section below them: wrong
    parents, wrong numbering, and an `insert_block` that refused a legitimate
    placement (measured on mapab2 b r1, four turns). A fence is three or more ` or
    ~, closed by the same character and at least as many.
    """
    found: List[Tuple[int, int, str, str]] = []
    fence_char = ""
    fence_len = 0
    for number, line in enumerate(lines):
        stripped = line.strip()
        marker = _FENCE.match(stripped)
        if marker:
            fence = marker.group(1)
            rest = stripped[len(fence):].strip()
            if not fence_char:
                fence_char, fence_len = fence[0], len(fence)
            elif fence[0] == fence_char and len(fence) >= fence_len and not rest:
                fence_char, fence_len = "", 0
            continue
        if fence_char:
            continue
        match = _MD_HEADING.match(line.rstrip("\r\n"))
        if match:
            found.append((number, len(match.group(1)), match.group(2).strip(), line.strip()))
    return found


def unclosed_fence_line(text: str) -> Optional[int]:
    """1-based line of a code fence that never closes, or None when they balance.

    An unclosed fence runs to the end of the document, so every heading after it
    stops being a heading and the section that contains it swallows the rest of the
    file. Measured on mapab2 a r3: one edit left a stray ``` and the section went
    from 61 to 826 lines; the outline guard then refused every repair, and the run
    died at max_turns on a document no tool could touch any more.
    """
    fence_char = ""
    fence_len = 0
    opened_at = 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        marker = _FENCE.match(stripped)
        if not marker:
            continue
        fence = marker.group(1)
        rest = stripped[len(fence):].strip()
        if not fence_char:
            fence_char, fence_len, opened_at = fence[0], len(fence), number
        elif fence[0] == fence_char and len(fence) >= fence_len and not rest:
            fence_char, fence_len, opened_at = "", 0, 0
    return opened_at or None


def markdown_layout(text: str) -> List[dict]:
    """Return the paragraph layout of a Markdown document.

    Each entry carries: heading, level, index, section_start, body_start and
    section_end (character offsets into text), so a paragraph can be edited by
    splicing rather than by rewriting the file.
    """
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    total = position

    headings: List[dict] = []
    for index, level, title, _raw in heading_lines(lines):
        headings.append(
            {
                "heading": title,
                "level": level,
                "section_start": offsets[index],
                "body_start": offsets[index] + len(lines[index]),
            }
        )

    layout: List[dict] = []
    if headings and headings[0]["section_start"] > 0:
        if text[: headings[0]["section_start"]].strip():
            layout.append(
                {
                    "heading": "Preamble",
                    "level": 0,
                    "section_start": 0,
                    "body_start": 0,
                    "section_end": headings[0]["section_start"],
                    "index": 0,
                }
            )
    for position, item in enumerate(headings):
        item["section_end"] = headings[position + 1]["section_start"] if position + 1 < len(headings) else total
        item["index"] = len(layout)
        layout.append(item)
    return layout


@dataclass
class ParsedDocument:
    """Parsed representation of a document."""

    doc_type: str
    title: str
    sections: List[DocumentSection] = field(default_factory=list)


_FILE_REF = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:md|markdown|py|pyi|cs|ts|tsx|js|jsx|json|ya?ml|ps1|sh|txt|csproj|sln)",
    re.IGNORECASE,
)


def resolve_referenced_files(text: str, paths_by_basename: Dict[str, List[str]]) -> List[str]:
    """Map file paths mentioned inside a document to actual workspace files.

    This is what lets an agent connect "the guide says to update X.md" to the real
    workspace path of X.md, instead of editing an unrelated document.
    """
    resolved: List[str] = []
    for candidate in _FILE_REF.findall(text or ""):
        basename = candidate.replace("\\", "/").split("/")[-1]
        for path in paths_by_basename.get(basename, []):
            if path not in resolved:
                resolved.append(path)
    return resolved


class BaseDocumentParser:
    """Interface for document-format parsers."""

    extensions: Tuple[str, ...] = ()
    doc_type: str = "text"

    def can_parse(self, file_path: str) -> bool:
        return file_path.lower().endswith(self.extensions)

    def parse(self, file_path: str, content_bytes: bytes) -> ParsedDocument:  # pragma: no cover - interface
        raise NotImplementedError


class MarkdownDocumentParser(BaseDocumentParser):
    """Markdown headings mapped to paragraphs."""

    extensions = (".md", ".markdown", ".mdx")
    doc_type = "markdown"
    _HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

    def parse(self, file_path: str, content_bytes: bytes) -> ParsedDocument:
        text = content_bytes.decode("utf-8", errors="replace")
        layout = markdown_layout(text)

        title = Path(file_path).stem
        for item in layout:
            if item["level"] > 0:
                title = item["heading"]
                break

        sections: List[DocumentSection] = []
        for item in layout:
            body = text[item["body_start"] : item["section_end"]].strip()
            sections.append(
                DocumentSection(
                    item["heading"],
                    item["level"],
                    body,
                    item["index"],
                    item["section_start"],
                    item["section_end"],
                )
            )

        if not sections:
            body = text.strip()
            if body:
                sections.append(DocumentSection(title, 1, body, 0, 0, len(text)))
        return ParsedDocument(self.doc_type, title, sections)


class DocxDocumentParser(BaseDocumentParser):
    """Word (.docx) paragraphs, using Heading styles and outline levels."""

    extensions = (".docx",)
    doc_type = "docx"

    def parse(self, file_path: str, content_bytes: bytes) -> ParsedDocument:
        fallback_title = Path(file_path).stem
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as archive:
                document_xml = archive.read("word/document.xml")
                core_xml: Optional[bytes]
                try:
                    core_xml = archive.read("docProps/core.xml")
                except KeyError:
                    core_xml = None
        except (zipfile.BadZipFile, KeyError, OSError):
            return ParsedDocument(self.doc_type, fallback_title, [])

        paragraphs: List[Tuple[str, int]] = []
        try:
            root = ET.fromstring(document_xml)
        except ET.ParseError:
            return ParsedDocument(self.doc_type, fallback_title, [])

        for paragraph in root.iter(WORD_NS + "p"):
            text = "".join(node.text or "" for node in paragraph.iter(WORD_NS + "t")).strip()
            level = 0
            props = paragraph.find(WORD_NS + "pPr")
            if props is not None:
                style = props.find(WORD_NS + "pStyle")
                if style is not None:
                    value = style.get(WORD_NS + "val") or ""
                    match = re.match(r"Heading(\d+)", value)
                    if match:
                        level = int(match.group(1))
                    elif value.lower().startswith("title"):
                        level = 1
                outline = props.find(WORD_NS + "outlineLvl")
                if outline is not None and level == 0:
                    try:
                        level = int(outline.get(WORD_NS + "val")) + 1
                    except (TypeError, ValueError):
                        level = 0
            paragraphs.append((text, level))

        title = fallback_title
        if core_xml:
            try:
                core_root = ET.fromstring(core_xml)
                node = core_root.find(DC_NS + "title")
                if node is not None and (node.text or "").strip():
                    title = node.text.strip()
            except ET.ParseError:
                pass

        sections: List[DocumentSection] = []
        current: Optional[DocumentSection] = None
        for text, level in paragraphs:
            if level > 0 and text:
                if current is not None:
                    sections.append(current)
                current = DocumentSection(text, level, "", len(sections))
            elif text:
                if current is None:
                    current = DocumentSection(title, 1, "", len(sections))
                current.content = (current.content + "\n" + text).strip()
        if current is not None:
            sections.append(current)

        if sections and title == fallback_title:
            title = sections[0].heading or title
        return ParsedDocument(self.doc_type, title, sections)


class PdfDocumentParser(BaseDocumentParser):
    """PDF outline/bookmarks when available, otherwise one section per page."""

    extensions = (".pdf",)
    doc_type = "pdf"

    def parse(self, file_path: str, content_bytes: bytes) -> ParsedDocument:
        fallback_title = Path(file_path).stem
        try:
            from pypdf import PdfReader
        except ImportError:
            return ParsedDocument(self.doc_type, fallback_title, [])

        try:
            reader = PdfReader(io.BytesIO(content_bytes))
            pages = [(page.extract_text() or "") for page in reader.pages]
        except Exception:
            return ParsedDocument(self.doc_type, fallback_title, [])

        title = fallback_title
        try:
            metadata_title = getattr(reader.metadata, "title", None) if reader.metadata else None
            if metadata_title and str(metadata_title).strip():
                title = str(metadata_title).strip()
        except Exception:
            pass

        outline = self._outline(reader)
        sections: List[DocumentSection] = []
        if outline:
            for position, (heading, start_page, level) in enumerate(outline):
                end_page = outline[position + 1][1] if position + 1 < len(outline) else len(pages)
                chunk = "\n".join(pages[start_page : max(start_page + 1, end_page)]).strip()
                sections.append(DocumentSection(heading, level, chunk, position))
        else:
            for index, page_text in enumerate(pages):
                text = page_text.strip()
                if not text:
                    continue
                heading = self._guess_heading(text) or ("Page " + str(index + 1))
                sections.append(DocumentSection(heading, 1, text, len(sections)))

        if sections and title == fallback_title:
            title = sections[0].heading or title
        return ParsedDocument(self.doc_type, title, sections)

    @staticmethod
    def _outline(reader) -> List[Tuple[str, int, int]]:
        try:
            outline = reader.outline
        except Exception:
            return []
        if not outline:
            return []

        entries: List[Tuple[str, int, int]] = []

        def walk(items, depth: int) -> None:
            for item in items:
                if isinstance(item, list):
                    walk(item, depth + 1)
                    continue
                try:
                    page_no = reader.get_destination_page_number(item)
                except Exception:
                    page_no = 0
                heading = getattr(item, "title", None) or str(item)
                entries.append((str(heading), page_no, depth + 1))

        try:
            walk(outline, 0)
        except Exception:
            return []
        return entries

    @staticmethod
    def _guess_heading(text: str) -> str:
        first = ""
        for line in text.splitlines():
            if line.strip():
                first = line.strip()
                break
        if first and len(first) <= 80 and not first.endswith("."):
            return first
        return ""


class LegacyDocDocumentParser(BaseDocumentParser):
    """Legacy .doc files: best-effort text extraction (no full Word parser)."""

    extensions = (".doc",)
    doc_type = "doc"

    def parse(self, file_path: str, content_bytes: bytes) -> ParsedDocument:
        title = Path(file_path).stem
        text = self._extract_text(content_bytes).strip()
        sections = [DocumentSection(title, 1, text, 0)] if text else []
        return ParsedDocument(self.doc_type, title, sections)

    @staticmethod
    def _extract_text(content_bytes: bytes) -> str:
        data = content_bytes
        try:
            import olefile

            if olefile.isOleFile(io.BytesIO(content_bytes)):
                with olefile.OleFileIO(io.BytesIO(content_bytes)) as ole:
                    if ole.exists("WordDocument"):
                        data = ole.openstream("WordDocument").read()
        except Exception:
            data = content_bytes
        return LegacyDocDocumentParser._scan_printable(data)

    @staticmethod
    def _scan_printable(data: bytes) -> str:
        utf16 = data.decode("utf-16-le", errors="ignore")
        runs = re.findall(r"[\x20-\x7E\u00C0-\u024F]{12,}", utf16)
        best = "\n".join(run.strip() for run in runs)
        if len(best) < 40:
            latin = data.decode("latin-1", errors="ignore")
            runs = re.findall(r"[\x20-\x7E\u00C0-\u024F]{12,}", latin)
            best = "\n".join(run.strip() for run in runs)
        return best


class DocumentRegistry:
    """Resolves the document parser for a file path."""

    def __init__(self) -> None:
        self._parsers: List[BaseDocumentParser] = [
            MarkdownDocumentParser(),
            DocxDocumentParser(),
            PdfDocumentParser(),
            LegacyDocDocumentParser(),
        ]

    @property
    def extensions(self) -> Tuple[str, ...]:
        found: List[str] = []
        for parser in self._parsers:
            found.extend(parser.extensions)
        return tuple(found)

    def get_parser_for_file(self, file_path: str) -> Optional[BaseDocumentParser]:
        for parser in self._parsers:
            if parser.can_parse(file_path):
                return parser
        return None

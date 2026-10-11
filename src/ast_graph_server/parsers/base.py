"""Base interfaces and models for AST parsers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
from typing import List, Optional, Tuple


@dataclass
class SymbolInfo:
    id: str  # e.g., "src/billing.py::BillingService.charge"
    file_path: str  # relative posix, e.g. "src/billing.py"
    parent_id: Optional[str]  # e.g. "src/billing.py::BillingService" or None
    name: str  # e.g. "charge"
    kind: str  # 'function' | 'method' | 'class' | 'interface'
    signature: str  # e.g. "def charge(self, user_id: str, amount: float) -> Receipt"
    docstring: Optional[str]
    start_byte: int
    end_byte: int
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed
    content_hash: str  # SHA-256


@dataclass
class CallInfo:
    caller_id: str
    callee_name: str
    call_expr: str
    callee_symbol_id: Optional[str] = None


@dataclass
class SyntaxErrorDetail:
    line: int
    col: int
    message: str
    context_snippet: str = ""
    actionable_hint: str = ""

    def __str__(self) -> str:
        base = f"Syntax error at line {self.line}:{self.col} near '{self.context_snippet}'"
        if self.actionable_hint:
            return f"{base} -> Hint: {self.actionable_hint}"
        return base

    def __contains__(self, item: str) -> bool:
        return item in str(self) or item in self.message or item in self.actionable_hint

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "col": self.col,
            "message": self.message,
            "context_snippet": self.context_snippet,
            "actionable_hint": self.actionable_hint,
        }


@dataclass
class ParseResult:
    symbols: List[SymbolInfo] = field(default_factory=list)
    calls: List[CallInfo] = field(default_factory=list)
    syntax_ok: bool = True
    errors: List[str] = field(default_factory=list)
    error_details: List[SyntaxErrorDetail] = field(default_factory=list)


CONTENT_HASH_LENGTH = 8


def compute_content_hash(code_bytes: bytes) -> str:
    """Compute unified short content hash (first 8 hex chars of SHA-256)."""
    return hashlib.sha256(code_bytes).hexdigest()[:CONTENT_HASH_LENGTH]


class BaseParser(ABC):
    """Abstract base class for language AST parsers."""

    @abstractmethod
    def can_parse(self, file_path: str) -> bool:
        """Return True if this parser supports the given file extension."""
        pass

    @abstractmethod
    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        """Parse source file bytes into symbols and call hierarchies."""
        pass

    @abstractmethod
    def validate_syntax(
        self, code: bytes, file_path: Optional[str] = None
    ) -> Tuple[bool, List[SyntaxErrorDetail]]:
        """Validate syntax of a code snippet or whole file.

        Args:
            code: UTF-8 source bytes to validate.
            file_path: optional path used to select a dialect when a language
                exposes several grammars (for example .tsx/.jsx for TypeScript).
        """
        pass

    @staticmethod
    def _declaration_suffix(key: str) -> str:
        """A short, order-independent suffix derived from a declaration key."""
        normalized = " ".join((key or "").split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _declaration_key(symbol: SymbolInfo, by_id: dict) -> str:
        """Signature plus the signatures of every enclosing container.

        The signature alone is not enough: this repository declares
        MultiTypeCategoryMapper<T, R1, R2> and ...<T, R1, R2, R3> -- the same
        name, with identical member signatures. The container signature carries
        the type parameters that tell them apart, so the id stays a function of
        the declaration rather than of its position.
        """
        parts = [symbol.signature]
        seen = set()
        parent = by_id.get(symbol.parent_id) if symbol.parent_id else None
        while parent is not None and parent.id not in seen:
            seen.add(parent.id)
            parts.append(parent.signature)
            parent = by_id.get(parent.parent_id) if parent.parent_id else None
        return " | ".join(parts)

    def _disambiguate_overloads(self, symbols: List[SymbolInfo], calls: List[CallInfo]) -> None:
        """Give every declaration that shares an id a declaration-derived suffix.

        A walk that numbers duplicates in the order it meets them (#2, #3...) makes
        the id a function of POSITION: reorder two overloads in the file and the ids
        swap, so an id captured before the edit then designates another declaration.
        A post-pass over the whole file makes the id a function of the declaration
        itself. It also keeps a language whose ids collide (two Python `def f`, which
        used to kill the indexer with UNIQUE constraint failed: symbols.id) indexable.
        """
        by_id = {symbol.id: symbol for symbol in symbols}
        groups: dict = {}
        for symbol in symbols:
            groups.setdefault(symbol.id.split("#")[0], []).append(symbol)
        remap: dict = {}
        for base, group in groups.items():
            if len(group) < 2:
                continue
            taken = set()
            for symbol in group:
                suffix = self._declaration_suffix(self._declaration_key(symbol, by_id))
                new_id = base + "#" + suffix
                ordinal = 2
                while new_id in taken:
                    new_id = base + "#" + suffix + "-" + str(ordinal)
                    ordinal += 1
                taken.add(new_id)
                remap[symbol.id] = new_id
                symbol.id = new_id
        if not remap:
            return
        for symbol in symbols:
            if symbol.parent_id in remap:
                symbol.parent_id = remap[symbol.parent_id]
        for call in calls:
            if call.caller_id in remap:
                call.caller_id = remap[call.caller_id]


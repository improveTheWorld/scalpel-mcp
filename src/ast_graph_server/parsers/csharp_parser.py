"""C# (C-Sharp) AST parser using Tree-sitter."""

import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple
import tree_sitter
import tree_sitter_c_sharp

from .base import BaseParser, CallInfo, ParseResult, SymbolInfo, SyntaxErrorDetail, compute_content_hash


class CSharpParser(BaseParser):
    """AST parser for C# source files (.cs)."""

    #: Declaration node types mapped to the symbol kinds stored in the graph.
    CONTAINER_KINDS = {
        "namespace_declaration": "namespace",
        "file_scoped_namespace_declaration": "namespace",
        "class_declaration": "class",
        "struct_declaration": "struct",
        "record_declaration": "record",
        "enum_declaration": "enum",
        "interface_declaration": "interface",
    }
    MEMBER_KINDS = {
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "destructor_declaration": "destructor",
        "operator_declaration": "operator",
        "property_declaration": "property",
        "local_function_statement": "function",
    }
    CONTAINER_KIND_NAMES = frozenset(CONTAINER_KINDS.values())
    BODY_TYPES = ("block", "arrow_expression_clause", "accessor_list", "declaration_list", "enum_member_declaration_list", ";")
    STRING_TYPES = ("string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression")

    def __init__(self) -> None:
        self._language = tree_sitter.Language(tree_sitter_c_sharp.language())
        self._parser = tree_sitter.Parser(self._language)

    def can_parse(self, file_path: str) -> bool:
        return file_path.endswith(".cs")

    def validate_syntax(
        self, code: bytes, file_path: Optional[str] = None
    ) -> Tuple[bool, List[SyntaxErrorDetail]]:
        """Check for syntax errors in C# code using Tree-sitter ERROR/MISSING nodes."""
        details: List[SyntaxErrorDetail] = []
        tree = self._parser.parse(code)

        def find_errors(node: Any) -> None:
            if node.type == "ERROR" or node.is_missing:
                line = node.start_point[0] + 1
                col = node.start_point[1]
                snippet = code[node.start_byte : min(node.end_byte + 20, len(code))].decode("utf-8", errors="replace").strip()
                if node.is_missing:
                    hint = "Missing required token '" + node.type + "'."
                else:
                    hint = "Invalid syntax near '" + snippet[:15] + "'. Check braces, semicolons and parentheses."
                details.append(
                    SyntaxErrorDetail(
                        line=line,
                        col=col,
                        message="Syntax error at line " + str(line) + ":" + str(col),
                        context_snippet=snippet,
                        actionable_hint=hint,
                    )
                )
            for child in node.children:
                find_errors(child)

        find_errors(tree.root_node)
        return (len(details) == 0, details)

    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        """Parse C# source into symbols and call relationships."""
        syntax_ok, error_details = self.validate_syntax(content)
        tree = self._parser.parse(content)

        symbols: List[SymbolInfo] = []
        calls: List[CallInfo] = []

        used_ids: Set[str] = set()
        self._walk(
            node=tree.root_node,
            content=content,
            file_path=file_path,
            parent_id=None,
            parent_name="",
            symbols=symbols,
            calls=calls,
            used_ids=used_ids,
        )
        # Les directives `using` de tete sont UN bloc a part entiere : l agent les lit et les edite
        # comme n importe quel autre bloc (read_block / edit_block), au lieu d un outil dedie avec sa
        # propre petite langue (action=list|add|remove). Une seule region : les using d un namespace
        # ne sont pas absorbes ici.
        head = [c for c in tree.root_node.children if c.type == "using_directive"]
        if head:
            start_byte, end_byte = head[0].start_byte, head[-1].end_byte
            symbols.append(
                SymbolInfo(
                    id=file_path + "::using",
                    file_path=file_path,
                    parent_id=None,
                    name="using",
                    kind="using",
                    signature="using directives",
                    docstring=None,
                    start_byte=start_byte,
                    end_byte=end_byte,
                    start_line=head[0].start_point[0] + 1,
                    end_line=head[-1].end_point[0] + 1,
                    content_hash=compute_content_hash(content[start_byte:end_byte]),
                )
            )
        self._disambiguate_overloads(symbols, calls)

        return ParseResult(
            symbols=symbols,
            calls=calls,
            syntax_ok=syntax_ok,
            errors=[str(e) for e in error_details],
            error_details=error_details,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _kind_for(self, node: Any) -> Optional[str]:
        if node.type in self.CONTAINER_KINDS:
            return self.CONTAINER_KINDS[node.type]
        return self.MEMBER_KINDS.get(node.type)

    def _name_of(self, node: Any, content: bytes) -> Optional[str]:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace").strip()
        return name or None

    def _body_of(self, node: Any) -> Optional[Any]:
        body = node.child_by_field_name("body")
        if body is not None:
            return body
        for child in node.children:
            if child.type in ("declaration_list", "block", "accessor_list", "enum_member_declaration_list"):
                return child
        return None

    def _span(self, node: Any, content: bytes) -> Tuple[int, int, int, int]:
        start_byte = node.start_byte
        line_start = content.rfind(b"\n", 0, start_byte)
        prefix = content[:start_byte] if line_start == -1 else content[line_start + 1 : start_byte]
        if prefix.strip() == b"":
            start_byte = 0 if line_start == -1 else line_start + 1
        return start_byte, node.end_byte, node.start_point[0] + 1, node.end_point[0] + 1

    def _signature(self, node: Any, content: bytes) -> str:
        start = node.start_byte
        for child in node.children:
            if child.type in ("attribute_list", "comment"):
                start = child.end_byte
                continue
            break

        end = node.end_byte
        for child in node.children:
            if child.type in self.BODY_TYPES:
                end = child.start_byte
                break
        signature = content[start:end].decode("utf-8", errors="replace").strip()
        return " ".join(line.strip() for line in signature.splitlines())

    def _extract_docstring(self, node: Any, content: bytes) -> Optional[str]:
        """Extract a preceding XML documentation comment (/// ...)."""
        collected: List[str] = []
        sibling = node.prev_named_sibling
        while sibling is not None and sibling.type == "comment":
            text = content[sibling.start_byte : sibling.end_byte].decode("utf-8", errors="replace").strip()
            if not text.startswith("///"):
                break
            collected.append(text)
            sibling = sibling.prev_named_sibling
        if not collected:
            return None
        collected.reverse()
        cleaned = [line.lstrip("/").strip() for line in collected]
        return "\n".join(line for line in cleaned if line) or None

    def _extract_calls(self, node: Any, content: bytes, caller_id: str, calls: List[CallInfo], seen: Set[str]) -> None:
        """Collect invocation expressions inside a member body."""
        for child in node.children:
            if child.type in self.CONTAINER_KINDS or child.type in self.MEMBER_KINDS:
                continue
            if child.type == "invocation_expression":
                function = child.child_by_field_name("function")
                if function is not None:
                    full_expr = content[function.start_byte : function.end_byte].decode("utf-8", errors="replace").strip()
                    callee_name = full_expr[5:] if full_expr.startswith("this.") else full_expr
                    key = caller_id + "->" + full_expr
                    if key not in seen:
                        seen.add(key)
                        calls.append(CallInfo(caller_id=caller_id, callee_name=callee_name, call_expr=full_expr))
            self._extract_calls(child, content, caller_id, calls, seen)

    def _walk(
        self,
        node: Any,
        content: bytes,
        file_path: str,
        parent_id: Optional[str],
        parent_name: str,
        symbols: List[SymbolInfo],
        calls: List[CallInfo],
        used_ids: Set[str],
    ) -> None:
        for child in node.children:
            kind = self._kind_for(child)
            if kind is None:
                self._walk(child, content, file_path, parent_id, parent_name, symbols, calls, used_ids)
                continue

            name = self._name_of(child, content)
            if not name:
                self._walk(child, content, file_path, parent_id, parent_name, symbols, calls, used_ids)
                continue

            qualified_name = (parent_name + "." + name) if parent_name else name
            symbol_id = file_path + "::" + qualified_name
            # C# supports method/property overloads: disambiguate identical ids.
            if symbol_id in used_ids:
                suffix = 2
                while (symbol_id + "#" + str(suffix)) in used_ids:
                    suffix += 1
                symbol_id = symbol_id + "#" + str(suffix)
            used_ids.add(symbol_id)
            start_byte, end_byte, start_line, end_line = self._span(child, content)
            body = self._body_of(child)

            symbols.append(
                SymbolInfo(
                    id=symbol_id,
                    file_path=file_path,
                    parent_id=parent_id,
                    name=name,
                    kind=kind,
                    signature=self._signature(child, content),
                    docstring=self._extract_docstring(child, content),
                    start_byte=start_byte,
                    end_byte=end_byte,
                    start_line=start_line,
                    end_line=end_line,
                    content_hash=compute_content_hash(content[start_byte:end_byte]),
                )
            )

            if kind in self.CONTAINER_KIND_NAMES:
                if body is not None:
                    self._walk(body, content, file_path, symbol_id, qualified_name, symbols, calls, used_ids)
                else:
                    self._walk(child, content, file_path, symbol_id, qualified_name, symbols, calls, used_ids)
            elif body is not None:
                seen: Set[str] = set()
                self._extract_calls(body, content, symbol_id, calls, seen)
                self._walk(body, content, file_path, symbol_id, qualified_name, symbols, calls, used_ids)

"""JSON parser using Tree-sitter for AST graph navigation and block editing."""

from typing import Any, List, Optional, Tuple
import tree_sitter
import tree_sitter_json

from .base import BaseParser, ParseResult, SymbolInfo, SyntaxErrorDetail, compute_content_hash


class JSONParser(BaseParser):
    """Parser for JSON files using tree-sitter-json."""

    def __init__(self) -> None:
        super().__init__()
        self._language = tree_sitter.Language(tree_sitter_json.language())
        self._parser = tree_sitter.Parser(self._language)

    def can_parse(self, file_path: str) -> bool:
        return file_path.lower().endswith(".json")

    def validate_syntax(
        self, code: bytes, file_path: Optional[str] = None
    ) -> Tuple[bool, List[SyntaxErrorDetail]]:
        """Validate JSON syntax using Tree-sitter."""
        details: List[SyntaxErrorDetail] = []
        tree = self._parser.parse(code)

        def find_errors(node: Any) -> None:
            if node.type == "ERROR" or node.is_missing:
                line = node.start_point[0] + 1
                col = node.start_point[1]
                snippet = code[node.start_byte : min(node.end_byte + 20, len(code))].decode(
                    "utf-8", errors="replace"
                ).strip()
                if node.is_missing:
                    hint = f"Missing required token '{node.type}'."
                else:
                    hint = f"Invalid JSON syntax near '{snippet[:15]}'. Check quotes, commas, brackets and braces."
                details.append(
                    SyntaxErrorDetail(
                        line=line,
                        col=col,
                        message=f"JSON syntax error at line {line}:{col}",
                        context_snippet=snippet,
                        actionable_hint=hint,
                    )
                )
            for child in node.children:
                find_errors(child)

        find_errors(tree.root_node)
        return (len(details) == 0, details)

    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        """Parse JSON source into property/object symbols."""
        syntax_ok, error_details = self.validate_syntax(content)
        tree = self._parser.parse(content)
        symbols: List[SymbolInfo] = []

        self._walk(
            node=tree.root_node,
            content=content,
            file_path=file_path,
            parent_id=None,
            parent_name="",
            symbols=symbols,
            depth=0,
            max_depth=4,
        )
        self._disambiguate_overloads(symbols, [])

        return ParseResult(
            symbols=symbols,
            calls=[],
            syntax_ok=syntax_ok,
            errors=[str(e) for e in error_details],
            error_details=error_details,
        )

    def _infer_type(self, node: Any, content: bytes) -> str:
        if not node:
            return "unknown"
        t = node.type
        if t == "string":
            return "string"
        elif t == "number":
            raw = content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
            if "." in raw or "e" in raw or "E" in raw:
                return "number"
            return "int"
        elif t in ("true", "false"):
            return "bool"
        elif t == "object":
            return "object"
        elif t == "array":
            return "array"
        return "unknown"

    def _get_object_fields(self, node: Any, content: bytes) -> Optional[List[Tuple[str, str]]]:
        if not node or node.type != "object":
            return None
        fields = []
        for child in node.children:
            if child.type == "pair":
                k_node = child.child_by_field_name("key")
                v_node = child.child_by_field_name("value")
                if not k_node:
                    continue
                k_name = content[k_node.start_byte : k_node.end_byte].decode("utf-8", errors="replace").strip('"\'')
                v_type = self._infer_type(v_node, content)
                fields.append((v_type, k_name))
        return fields

    def _infer_array_signature(self, key_name: str, array_node: Any, content: bytes) -> str:
        elements = [c for c in array_node.children if c.type not in ('[', ']', ',')]
        n = len(elements)
        if n == 0:
            return f'"{key_name}": [ ... ]'

        types = [self._infer_type(elem, content) for elem in elements]
        if all(t == types[0] for t in types):
            t0 = types[0]
            if t0 in ("string", "int", "number", "bool"):
                return f"{key_name} [{n}]{t0}"
            elif t0 == "object":
                obj_fields_list = [self._get_object_fields(elem, content) for elem in elements]
                if obj_fields_list and all(f is not None for f in obj_fields_list):
                    first_fields = obj_fields_list[0]
                    if all(f == first_fields for f in obj_fields_list):
                        fields_str = ", ".join(f"{ftype} {fname}" for ftype, fname in first_fields)
                        return f"{key_name} [{n}]{{{fields_str}}}"

        return f'"{key_name}": [ ... ]'

    def _walk(
        self,
        node: Any,
        content: bytes,
        file_path: str,
        parent_id: Optional[str],
        parent_name: str,
        symbols: List[SymbolInfo],
        depth: int,
        max_depth: int,
    ) -> None:
        if depth > max_depth:
            return

        if node.type == "array":
            elements = [child for child in node.children if child.type not in ('[', ']', ',')]
            for i, child in enumerate(elements):
                elem_name = f"[{i}]"
                qualified_name = f"{parent_name}{elem_name}" if parent_name else elem_name
                symbol_id = f"{file_path}::{qualified_name}"

                elem_type = child.type
                if elem_type == "object":
                    kind = "object"
                    signature = f"{elem_name}: {{ ... }}"
                elif elem_type == "array":
                    kind = "array"
                    signature = self._infer_array_signature(elem_name, child, content)
                else:
                    kind = "property"
                    raw_val = content[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
                    val_preview = raw_val.strip()
                    if len(val_preview) > 60:
                        val_preview = val_preview[:57] + "..."
                    signature = f"{elem_name}: {val_preview}"

                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])

                symbols.append(
                    SymbolInfo(
                        id=symbol_id,
                        file_path=file_path,
                        parent_id=parent_id,
                        name=elem_name,
                        kind=kind,
                        signature=signature,
                        docstring=None,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        content_hash=content_hash,
                    )
                )

                if elem_type in ("object", "array"):
                    self._walk(
                        node=child,
                        content=content,
                        file_path=file_path,
                        parent_id=symbol_id,
                        parent_name=qualified_name,
                        symbols=symbols,
                        depth=depth + 1,
                        max_depth=max_depth,
                    )
            return

        for child in node.children:
            if child.type == "pair":
                key_node = child.child_by_field_name("key")
                val_node = child.child_by_field_name("value")
                if not key_node:
                    continue

                raw_key = content[key_node.start_byte : key_node.end_byte].decode("utf-8", errors="replace")
                key_name = raw_key.strip('"\'')
                qualified_name = f"{parent_name}.{key_name}" if parent_name else key_name
                symbol_id = f"{file_path}::{qualified_name}"

                val_type = val_node.type if val_node else "unknown"
                if val_type == "object":
                    kind = "object"
                    signature = f'"{key_name}": {{ ... }}'
                elif val_type == "array":
                    kind = "array"
                    signature = self._infer_array_signature(key_name, val_node, content)
                else:
                    kind = "property"
                    raw_val = (
                        content[val_node.start_byte : val_node.end_byte].decode("utf-8", errors="replace")
                        if val_node
                        else ""
                    )
                    val_preview = raw_val.strip()
                    if len(val_preview) > 60:
                        val_preview = val_preview[:57] + "..."
                    signature = f'"{key_name}": {val_preview}'

                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])

                symbols.append(
                    SymbolInfo(
                        id=symbol_id,
                        file_path=file_path,
                        parent_id=parent_id,
                        name=key_name,
                        kind=kind,
                        signature=signature,
                        docstring=None,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        content_hash=content_hash,
                    )
                )

                if val_node and val_type in ("object", "array"):
                    self._walk(
                        node=val_node,
                        content=content,
                        file_path=file_path,
                        parent_id=symbol_id,
                        parent_name=qualified_name,
                        symbols=symbols,
                        depth=depth + 1,
                        max_depth=max_depth,
                    )
            else:
                self._walk(
                    node=child,
                    content=content,
                    file_path=file_path,
                    parent_id=parent_id,
                    parent_name=parent_name,
                    symbols=symbols,
                    depth=depth,
                    max_depth=max_depth,
                )
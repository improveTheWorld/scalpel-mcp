"""YAML parser using Tree-sitter for AST graph navigation and block editing."""

from typing import Any, List, Optional, Tuple
import tree_sitter
import tree_sitter_yaml

from .base import BaseParser, ParseResult, SymbolInfo, SyntaxErrorDetail, compute_content_hash


class YAMLParser(BaseParser):
    """Parser for YAML files (.yaml, .yml) using tree-sitter-yaml."""

    def __init__(self) -> None:
        super().__init__()
        self._language = tree_sitter.Language(tree_sitter_yaml.language())
        self._parser = tree_sitter.Parser(self._language)

    def can_parse(self, file_path: str) -> bool:
        lower = file_path.lower()
        return lower.endswith(".yaml") or lower.endswith(".yml")

    def validate_syntax(
        self, code: bytes, file_path: Optional[str] = None
    ) -> Tuple[bool, List[SyntaxErrorDetail]]:
        """Validate YAML syntax using Tree-sitter."""
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
                    hint = f"Missing required YAML token '{node.type}'."
                else:
                    hint = f"Invalid YAML syntax near '{snippet[:15]}'. Check indentation, colons and quotes."
                details.append(
                    SyntaxErrorDetail(
                        line=line,
                        col=col,
                        message=f"YAML syntax error at line {line}:{col}",
                        context_snippet=snippet,
                        actionable_hint=hint,
                    )
                )
            for child in node.children:
                find_errors(child)

        find_errors(tree.root_node)
        return (len(details) == 0, details)

    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        """Parse YAML source into property/mapping symbols."""
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
            max_depth=5,
        )
        self._disambiguate_overloads(symbols, [])

        return ParseResult(
            symbols=symbols,
            calls=[],
            syntax_ok=syntax_ok,
            errors=[str(e) for e in error_details],
            error_details=error_details,
        )

    def _unwrap_node(self, node: Any) -> Any:
        inner = node
        while inner and inner.type in ("block_node", "flow_node") and len(inner.children) == 1:
            inner = inner.children[0]
        return inner

    def _infer_type(self, node: Any, content: bytes) -> str:
        if not node:
            return "unknown"
        inner = self._unwrap_node(node)
        t = inner.type
        if t in ("block_mapping", "flow_mapping"):
            return "object"
        elif t in ("block_sequence", "flow_sequence"):
            return "array"
        elif t in ("string_scalar", "single_quote_scalar", "double_quote_scalar"):
            return "string"
        elif t == "integer_scalar":
            return "int"
        elif t == "float_scalar":
            return "number"
        elif t == "boolean_scalar":
            return "bool"
        elif t == "plain_scalar":
            if inner.children:
                child_t = inner.children[0].type
                if child_t == "string_scalar":
                    return "string"
                elif child_t == "integer_scalar":
                    return "int"
                elif child_t == "float_scalar":
                    return "number"
                elif child_t == "boolean_scalar":
                    return "bool"
            raw = content[inner.start_byte : inner.end_byte].decode("utf-8", errors="replace").strip()
            if raw in ("true", "false", "True", "False", "TRUE", "FALSE", "yes", "no", "on", "off"):
                return "bool"
            try:
                int(raw)
                return "int"
            except ValueError:
                pass
            try:
                float(raw)
                return "number"
            except ValueError:
                pass
            return "string"
        return "unknown"

    def _get_mapping_fields(self, node: Any, content: bytes) -> Optional[List[Tuple[str, str]]]:
        if not node:
            return None
        inner = self._unwrap_node(node)
        if inner.type not in ("block_mapping", "flow_mapping"):
            return None
        fields = []
        for child in inner.children:
            if child.type in ("block_mapping_pair", "flow_pair"):
                k_node = child.child_by_field_name("key")
                v_node = child.child_by_field_name("value")
                if not k_node:
                    continue
                k_name = content[k_node.start_byte : k_node.end_byte].decode("utf-8", errors="replace").strip().strip('"\'')
                v_type = self._infer_type(v_node, content)
                fields.append((v_type, k_name))
        return fields

    def _infer_sequence_signature(self, key_name: str, seq_node: Any, content: bytes) -> str:
        seq = self._unwrap_node(seq_node)
        if seq.type == "block_sequence":
            items = [c for c in seq.children if c.type == "block_sequence_item"]
            elements = []
            for item in items:
                c_nodes = [c for c in item.children if c.type != "-"]
                elements.append(c_nodes[0] if c_nodes else item)
        elif seq.type == "flow_sequence":
            elements = [c for c in seq.children if c.type not in ('[', ']', ',')]
        else:
            return f"{key_name}: [...]"

        n = len(elements)
        if n == 0:
            return f"{key_name}: [...]"

        types = [self._infer_type(elem, content) for elem in elements]
        if all(t == types[0] for t in types):
            t0 = types[0]
            if t0 in ("string", "int", "number", "bool"):
                return f"{key_name} [{n}]{t0}"
            elif t0 == "object":
                map_fields_list = [self._get_mapping_fields(elem, content) for elem in elements]
                if map_fields_list and all(f is not None for f in map_fields_list):
                    first_fields = map_fields_list[0]
                    if all(f == first_fields for f in map_fields_list):
                        fields_str = ", ".join(f"{ftype} {fname}" for ftype, fname in first_fields)
                        return f"{key_name} [{n}]{{{fields_str}}}"

        return f"{key_name}: [...]"

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

        if node.type in ("block_sequence", "flow_sequence"):
            if node.type == "block_sequence":
                items = [c for c in node.children if c.type == "block_sequence_item"]
            else:
                items = [c for c in node.children if c.type not in ('[', ']', ',')]

            for i, item in enumerate(items):
                elem_name = f"[{i}]"
                qualified_name = f"{parent_name}{elem_name}" if parent_name else elem_name
                symbol_id = f"{file_path}::{qualified_name}"

                target_node = item
                if node.type == "block_sequence":
                    content_nodes = [c for c in item.children if c.type != "-"]
                    if content_nodes:
                        target_node = content_nodes[0]

                inner = target_node
                while inner.type in ("block_node", "flow_node") and len(inner.children) == 1:
                    inner = inner.children[0]

                is_elem_mapping = inner.type in ("block_mapping", "flow_mapping")
                is_elem_seq = inner.type in ("block_sequence", "flow_sequence")

                if is_elem_mapping:
                    kind = "mapping"
                    signature = f"{elem_name}:"
                elif is_elem_seq:
                    kind = "sequence"
                    signature = self._infer_sequence_signature(elem_name, inner, content)
                else:
                    kind = "property"
                    raw_val = content[inner.start_byte : inner.end_byte].decode("utf-8", errors="replace").strip()
                    if len(raw_val) > 60:
                        raw_val = raw_val[:57] + "..."
                    signature = f"{elem_name}: {raw_val}" if raw_val else f"{elem_name}:"

                start_byte = item.start_byte
                end_byte = item.end_byte
                start_line = item.start_point[0] + 1
                end_line = item.end_point[0] + 1
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

                if is_elem_mapping or is_elem_seq:
                    self._walk(
                        node=inner,
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
            if child.type in ("block_mapping_pair", "flow_pair"):
                key_node = child.child_by_field_name("key")
                val_node = child.child_by_field_name("value")
                if not key_node:
                    continue

                raw_key = content[key_node.start_byte : key_node.end_byte].decode("utf-8", errors="replace")
                key_name = raw_key.strip().strip('"\'')
                qualified_name = f"{parent_name}.{key_name}" if parent_name else key_name
                symbol_id = f"{file_path}::{qualified_name}"

                val_inner_types = [sub.type for sub in (val_node.children if val_node else [])]
                is_mapping = any(t in ("block_mapping", "flow_mapping") for t in val_inner_types)
                is_sequence = any(t in ("block_sequence", "flow_sequence") for t in val_inner_types)

                if is_mapping:
                    kind = "mapping"
                    signature = f"{key_name}:"
                elif is_sequence:
                    kind = "sequence"
                    signature = self._infer_sequence_signature(key_name, val_node, content)
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
                    signature = f"{key_name}: {val_preview}" if val_preview else f"{key_name}:"

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

                if val_node and (is_mapping or is_sequence):
                    target = val_node
                    while target.type in ("block_node", "flow_node") and len(target.children) == 1:
                        target = target.children[0]
                    self._walk(
                        node=target,
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
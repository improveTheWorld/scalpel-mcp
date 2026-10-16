"""TypeScript and JavaScript AST parser using Tree-sitter."""

from typing import Any, List, Optional, Set, Tuple
import tree_sitter
import tree_sitter_typescript

from .base import BaseParser, CallInfo, ParseResult, SymbolInfo, SyntaxErrorDetail, compute_content_hash

TEST_CALL_NAMES = {
    "test", "it", "describe", "suite", "context",
    "before", "after", "beforeEach", "afterEach", "beforeAll", "afterAll"
}


class TypeScriptParser(BaseParser):
    """AST parser for TypeScript and JavaScript files."""

    def __init__(self) -> None:
        self._ts_language = tree_sitter.Language(tree_sitter_typescript.language_typescript())
        self._tsx_language = tree_sitter.Language(tree_sitter_typescript.language_tsx())
        self._ts_parser = tree_sitter.Parser(self._ts_language)
        self._tsx_parser = tree_sitter.Parser(self._tsx_language)

    def can_parse(self, file_path: str) -> bool:
        return any(file_path.endswith(ext) for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))

    def _get_parser(self, file_path: str) -> tree_sitter.Parser:
        if file_path.endswith((".tsx", ".jsx")):
            return self._tsx_parser
        return self._ts_parser

    def _collect_errors(self, root: Any, code: bytes) -> List[SyntaxErrorDetail]:
        details: List[SyntaxErrorDetail] = []

        def find_errors(node: Any) -> None:
            if node.type == "ERROR" or node.is_missing:
                line = node.start_point[0] + 1
                col = node.start_point[1]
                snippet = code[node.start_byte : min(node.end_byte + 20, len(code))].decode("utf-8", errors="replace").strip()
                hint = f"Missing required token '{node.type}'." if node.is_missing else f"Invalid syntax near '{snippet[:15]}'."
                details.append(
                    SyntaxErrorDetail(
                        line=line,
                        col=col,
                        message=f"Syntax error at line {line}:{col}",
                        context_snippet=snippet,
                        actionable_hint=hint,
                    )
                )
            for child in node.children:
                find_errors(child)

        find_errors(root)
        return details

    def validate_syntax(
        self, code: bytes, file_path: Optional[str] = None
    ) -> Tuple[bool, List[SyntaxErrorDetail]]:
        """Validate syntax, selecting the TSX or TS grammar when the path is known.

        When the file path is unknown, both grammars are tried and the code is
        accepted if either parses without errors. This avoids rejecting valid
        TSX/JSX with the plain TypeScript grammar.
        """
        if file_path:
            candidates = [self._get_parser(file_path)]
        else:
            candidates = [self._ts_parser, self._tsx_parser]

        best: Optional[List[SyntaxErrorDetail]] = None
        for parser in candidates:
            tree = parser.parse(code)
            details = self._collect_errors(tree.root_node, code)
            if not details:
                return True, []
            if best is None or len(details) < len(best):
                best = details
        return False, best or []

    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        """Parse TS/JS source into symbols and call relationships."""
        parser = self._get_parser(file_path)
        tree = parser.parse(content)
        syntax_ok, error_details = self.validate_syntax(content)

        symbols: List[SymbolInfo] = []
        calls: List[CallInfo] = []

        self._extract_symbols_and_calls(
            node=tree.root_node,
            content=content,
            file_path=file_path,
            parent_id=None,
            parent_name="",
            symbols=symbols,
            calls=calls,
        )
        # Top-level contiguous import statements grouped as an addressable '::imports' block
        head = []
        for c in tree.root_node.children:
            if c.type == "import_statement":
                head.append(c)
            elif c.type == "comment":
                continue
            elif head:
                break
        if head and symbols:
            start_byte, end_byte = head[0].start_byte, head[-1].end_byte
            symbols.append(
                SymbolInfo(
                    id=file_path + "::imports",
                    file_path=file_path,
                    parent_id=None,
                    name="imports",
                    kind="imports",
                    signature="import statements",
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

    def _extract_docstring(self, node: Any, content: bytes) -> Optional[str]:
        """Extract JSDoc comment preceding node if present."""
        target = node
        if target.parent and target.parent.type == "export_statement":
            target = target.parent
        prev = target.prev_named_sibling
        if prev and prev.type == "comment":
            text = content[prev.start_byte : prev.end_byte].decode("utf-8", errors="replace").strip()
            if text.startswith("/**"):
                # Clean JSDoc asterisks
                lines = []
                for line in text.splitlines():
                    cleaned = line.strip().lstrip("/*").rstrip("*/").strip()
                    if cleaned:
                        lines.append(cleaned)
                return "\n".join(lines)
        return None

    def _extract_signature(self, node: Any, content: bytes) -> str:
        """Extract signature line up to opening brace { or semicolon ;."""
        first_line = content[node.start_byte : node.end_byte].splitlines()[0]
        sig = first_line.decode("utf-8", errors="replace").strip()
        if "{" in sig:
            sig = sig.split("{")[0].strip()
        return sig.rstrip(";")

    def _extract_calls(self, node: Any, content: bytes, caller_id: str, calls: List[CallInfo], seen_calls: Set[str]) -> None:
        """Collect function/method calls inside a symbol body."""
        for child in node.children:
            if child.type in ("function_declaration", "method_definition", "class_declaration", "interface_declaration"):
                continue

            if child.type == "call_expression":
                fn_node = child.child_by_field_name("function")
                if fn_node:
                    full_expr = content[fn_node.start_byte : fn_node.end_byte].decode("utf-8", errors="replace").strip()
                    callee_name = full_expr
                    if full_expr.startswith("this."):
                        callee_name = full_expr[5:]

                    call_key = f"{caller_id}->{full_expr}"
                    if call_key not in seen_calls:
                        seen_calls.add(call_key)
                        calls.append(
                            CallInfo(
                                caller_id=caller_id,
                                callee_name=callee_name,
                                call_expr=full_expr,
                            )
                        )

            self._extract_calls(child, content, caller_id, calls, seen_calls)

    def _extract_symbols_and_calls(
        self,
        node: Any,
        content: bytes,
        file_path: str,
        parent_id: Optional[str],
        parent_name: str,
        symbols: List[SymbolInfo],
        calls: List[CallInfo],
    ) -> None:
        """Walk AST to find class, interface, function, and variable definitions."""
        for child in node.children:
            if child.type == "export_statement":
                has_clause = False
                for sub in child.children:
                    if sub.type == "export_clause":
                        has_clause = True
                        for spec in sub.children:
                            if spec.type == "export_specifier":
                                alias_node = spec.child_by_field_name("alias")
                                name_node = spec.child_by_field_name("name")
                                target = alias_node if alias_node else name_node
                                if target:
                                    name = content[target.start_byte : target.end_byte].decode("utf-8", errors="replace")
                                    sym_id = f"{file_path}::{name}"
                                    symbols.append(
                                        SymbolInfo(
                                            id=sym_id,
                                            file_path=file_path,
                                            parent_id=parent_id,
                                            name=name,
                                            kind="variable",
                                            signature=f"export {name}",
                                            docstring=None,
                                            start_byte=spec.start_byte,
                                            end_byte=spec.end_byte,
                                            start_line=spec.start_point[0] + 1,
                                            end_line=spec.end_point[0] + 1,
                                            content_hash=compute_content_hash(content[spec.start_byte : spec.end_byte]),
                                        )
                                    )
                    elif sub.type == "*":
                        has_clause = True
                        src_node = child.child_by_field_name("source")
                        src_text = content[src_node.start_byte : src_node.end_byte].decode("utf-8", errors="replace") if src_node else ""
                        name = f"* ({src_text})" if src_text else "*"
                        sym_id = f"{file_path}::{name}"
                        symbols.append(
                            SymbolInfo(
                                id=sym_id,
                                file_path=file_path,
                                parent_id=parent_id,
                                name=name,
                                kind="variable",
                                signature=content[child.start_byte : child.end_byte].decode("utf-8", errors="replace").strip().rstrip(";"),
                                docstring=None,
                                start_byte=child.start_byte,
                                end_byte=child.end_byte,
                                start_line=child.start_point[0] + 1,
                                end_line=child.end_point[0] + 1,
                                content_hash=compute_content_hash(content[child.start_byte : child.end_byte]),
                            )
                        )

                is_default = any(c.type == "default" for c in child.children)
                decl = child.child_by_field_name("declaration") or child.child_by_field_name("value")
                if is_default and decl and decl.type not in ("function_declaration", "class_declaration"):
                    name = "default"
                    sym_id = f"{file_path}::{name}"
                    is_func = decl.type in ("arrow_function", "function_expression")
                    kind = "function" if is_func else ("class" if decl.type == "class" else "variable")
                    symbols.append(
                        SymbolInfo(
                            id=sym_id,
                            file_path=file_path,
                            parent_id=parent_id,
                            name=name,
                            kind=kind,
                            signature=self._extract_signature(child, content),
                            docstring=self._extract_docstring(child, content),
                            start_byte=child.start_byte,
                            end_byte=child.end_byte,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            content_hash=compute_content_hash(content[child.start_byte : child.end_byte]),
                        )
                    )
                    if is_func:
                        body_node = decl.child_by_field_name("body")
                        if body_node:
                            seen_calls: Set[str] = set()
                            self._extract_calls(body_node, content, sym_id, calls, seen_calls)
                            self._extract_symbols_and_calls(
                                node=body_node,
                                content=content,
                                file_path=file_path,
                                parent_id=sym_id,
                                parent_name=name,
                                symbols=symbols,
                                calls=calls,
                            )
                    continue

                if not has_clause:
                    before_len = len(symbols)
                    self._extract_symbols_and_calls(
                        node=child,
                        content=content,
                        file_path=file_path,
                        parent_id=parent_id,
                        parent_name=parent_name,
                        symbols=symbols,
                        calls=calls,
                    )
                    # Include the leading 'export ' keyword in the span of the single top-level
                    # exported declaration so read_block line 1 shows 'export ...' and replacing
                    # line 1 with 'export ...' never produces 'export export ...' (#30).
                    direct_exported = [
                        s for s in symbols[before_len:]
                        if s.parent_id == parent_id
                    ]
                    if len(direct_exported) == 1:
                        sym = direct_exported[0]
                        sym.start_byte = child.start_byte
                        sym.start_line = child.start_point[0] + 1
                        sym.content_hash = compute_content_hash(content[sym.start_byte : sym.end_byte])
                        if not sym.signature.startswith("export "):
                            sym.signature = "export " + sym.signature
                continue

            if child.type in ("class_declaration", "abstract_class_declaration"):
                name_node = child.child_by_field_name("name")
                if not name_node:
                    continue
                name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                qualified_name = f"{parent_name}.{name}" if parent_name else name
                symbol_id = f"{file_path}::{qualified_name}"

                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])
                docstring = self._extract_docstring(child, content)
                signature = self._extract_signature(child, content)

                symbols.append(
                    SymbolInfo(
                        id=symbol_id,
                        file_path=file_path,
                        parent_id=parent_id,
                        name=name,
                        kind="class",
                        signature=signature,
                        docstring=docstring,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        content_hash=content_hash,
                    )
                )

                body_node = child.child_by_field_name("body")
                if body_node:
                    self._extract_symbols_and_calls(
                        node=body_node,
                        content=content,
                        file_path=file_path,
                        parent_id=symbol_id,
                        parent_name=qualified_name,
                        symbols=symbols,
                        calls=calls,
                    )

            elif child.type in ("interface_declaration", "type_alias_declaration", "enum_declaration", "internal_module", "module"):
                name_node = child.child_by_field_name("name")
                if not name_node:
                    continue
                name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                qualified_name = f"{parent_name}.{name}" if parent_name else name
                symbol_id = f"{file_path}::{qualified_name}"
                kind_map = {
                    "interface_declaration": "interface",
                    "type_alias_declaration": "interface",
                    "enum_declaration": "enum",
                    "internal_module": "namespace",
                    "module": "namespace",
                }
                kind = kind_map.get(child.type, "interface")

                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])
                docstring = self._extract_docstring(child, content)
                signature = self._extract_signature(child, content)

                symbols.append(
                    SymbolInfo(
                        id=symbol_id,
                        file_path=file_path,
                        parent_id=parent_id,
                        name=name,
                        kind=kind,
                        signature=signature,
                        docstring=docstring,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        content_hash=content_hash,
                    )
                )
                body_node = child.child_by_field_name("body")
                if body_node and child.type in ("internal_module", "module"):
                    self._extract_symbols_and_calls(
                        node=body_node,
                        content=content,
                        file_path=file_path,
                        parent_id=symbol_id,
                        parent_name=qualified_name,
                        symbols=symbols,
                        calls=calls,
                    )
            elif child.type in ("public_field_definition", "field_definition") and parent_id is not None:
                name_node = child.child_by_field_name("name")
                if not name_node:
                    continue
                name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                qualified_name = f"{parent_name}.{name}" if parent_name else name
                symbol_id = f"{file_path}::{qualified_name}"
                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                symbols.append(
                    SymbolInfo(
                        id=symbol_id,
                        file_path=file_path,
                        parent_id=parent_id,
                        name=name,
                        kind="property",
                        signature=self._extract_signature(child, content),
                        docstring=self._extract_docstring(child, content),
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        content_hash=compute_content_hash(content[start_byte:end_byte]),
                    )
                )

            elif child.type in ("function_declaration", "function_signature", "method_definition", "method_signature"):
                name_node = child.child_by_field_name("name")
                if not name_node:
                    continue
                name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                qualified_name = f"{parent_name}.{name}" if parent_name else name
                symbol_id = f"{file_path}::{qualified_name}"
                kind = "method" if child.type in ("method_definition", "method_signature") or parent_id else "function"

                start_byte = child.start_byte
                end_byte = child.end_byte
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])
                docstring = self._extract_docstring(child, content)
                signature = self._extract_signature(child, content)

                symbols.append(
                    SymbolInfo(
                        id=symbol_id,
                        file_path=file_path,
                        parent_id=parent_id,
                        name=name,
                        kind=kind,
                        signature=signature,
                        docstring=docstring,
                        start_byte=start_byte,
                        end_byte=end_byte,
                        start_line=start_line,
                        end_line=end_line,
                        content_hash=content_hash,
                    )
                )

                body_node = child.child_by_field_name("body")
                if body_node:
                    seen_calls: Set[str] = set()
                    self._extract_calls(body_node, content, symbol_id, calls, seen_calls)
                    self._extract_symbols_and_calls(
                        node=body_node,
                        content=content,
                        file_path=file_path,
                        parent_id=symbol_id,
                        parent_name=qualified_name,
                        symbols=symbols,
                        calls=calls,
                    )
            elif child.type in ("lexical_declaration", "variable_declaration"):
                declarators = [c for c in child.children if c.type == "variable_declarator"]
                for decl in declarators:
                    name_node = decl.child_by_field_name("name")
                    if not name_node:
                        continue
                    if name_node.type == "identifier":
                        name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                        qualified_name = f"{parent_name}.{name}" if parent_name else name
                        symbol_id = f"{file_path}::{qualified_name}"
                        val_node = decl.child_by_field_name("value")
                        is_func = val_node is not None and val_node.type in ("arrow_function", "function_expression")
                        kind = "function" if is_func else "variable"

                        if len(declarators) == 1:
                            start_byte = child.start_byte
                            end_byte = child.end_byte
                            start_line = child.start_point[0] + 1
                            end_line = child.end_point[0] + 1
                            sig_node = child
                        else:
                            start_byte = decl.start_byte
                            end_byte = decl.end_byte
                            start_line = decl.start_point[0] + 1
                            end_line = decl.end_point[0] + 1
                            sig_node = decl

                        content_hash = compute_content_hash(content[start_byte:end_byte])
                        docstring = self._extract_docstring(child, content)
                        signature = self._extract_signature(sig_node, content)

                        symbols.append(
                            SymbolInfo(
                                id=symbol_id,
                                file_path=file_path,
                                parent_id=parent_id,
                                name=name,
                                kind=kind,
                                signature=signature,
                                docstring=docstring,
                                start_byte=start_byte,
                                end_byte=end_byte,
                                start_line=start_line,
                                end_line=end_line,
                                content_hash=content_hash,
                            )
                        )
                        if is_func and val_node:
                            body_node = val_node.child_by_field_name("body")
                            if body_node:
                                seen_calls: Set[str] = set()
                                self._extract_calls(body_node, content, symbol_id, calls, seen_calls)
                                self._extract_symbols_and_calls(
                                    node=body_node,
                                    content=content,
                                    file_path=file_path,
                                    parent_id=symbol_id,
                                    parent_name=qualified_name,
                                    symbols=symbols,
                                    calls=calls,
                                )
            elif child.type == "expression_statement" and parent_id is None:
                for sub in child.children:
                    if sub.type == "assignment_expression":
                        left = sub.child_by_field_name("left")
                        right = sub.child_by_field_name("right")
                        if left and right and left.type == "member_expression":
                            left_text = content[left.start_byte : left.end_byte].decode("utf-8", errors="replace")
                            name = None
                            if left_text.startswith("exports."):
                                name = left_text[len("exports."):]
                            elif left_text.startswith("module.exports."):
                                name = left_text[len("module.exports."):]
                            elif left_text == "module.exports":
                                name = "exports"

                            if name and "." not in name:
                                symbol_id = f"{file_path}::{name}"
                                is_func = right.type in ("arrow_function", "function_expression")
                                kind = "function" if is_func else "variable"
                                start_byte = child.start_byte
                                end_byte = child.end_byte
                                symbols.append(
                                    SymbolInfo(
                                        id=symbol_id,
                                        file_path=file_path,
                                        parent_id=parent_id,
                                        name=name,
                                        kind=kind,
                                        signature=self._extract_signature(child, content),
                                        docstring=self._extract_docstring(child, content),
                                        start_byte=start_byte,
                                        end_byte=end_byte,
                                        start_line=child.start_point[0] + 1,
                                        end_line=child.end_point[0] + 1,
                                        content_hash=compute_content_hash(content[start_byte:end_byte]),
                                    )
                                )
                                if is_func:
                                    body_node = right.child_by_field_name("body")
                                    if body_node:
                                        seen_calls: Set[str] = set()
                                        self._extract_calls(body_node, content, symbol_id, calls, seen_calls)
                                        self._extract_symbols_and_calls(
                                            node=body_node,
                                            content=content,
                                            file_path=file_path,
                                            parent_id=symbol_id,
                                            parent_name=name,
                                            symbols=symbols,
                                            calls=calls,
                                        )
            # Extraction des appels de tests, hooks, suites et routes / middlewares
            call_node = None
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type == "call_expression":
                        call_node = sub
                        break
            elif child.type == "call_expression":
                call_node = child

            if call_node:
                func_node = call_node.child_by_field_name("function")
                args_node = call_node.child_by_field_name("arguments")
                if func_node and args_node:
                    func_text = content[func_node.start_byte : func_node.end_byte].decode("utf-8", errors="replace").strip()
                    base_func_name = func_text.split(".")[0].split("(")[0].strip()

                    first_arg_str = None
                    func_args = []
                    for arg in args_node.children:
                        if arg.type in ("arrow_function", "function_expression"):
                            func_args.append(arg)
                        elif first_arg_str is None and arg.type in ("string", "template_string"):
                            raw_str = content[arg.start_byte : arg.end_byte].decode("utf-8", errors="replace")
                            if (raw_str.startswith('"') and raw_str.endswith('"')) or (raw_str.startswith("'") and raw_str.endswith("'")):
                                first_arg_str = raw_str[1:-1]
                            elif raw_str.startswith("`") and raw_str.endswith("`"):
                                first_arg_str = raw_str[1:-1]
                            else:
                                first_arg_str = raw_str

                    is_test_call = base_func_name in TEST_CALL_NAMES
                    is_route_or_cb_call = len(func_args) > 0 and first_arg_str is not None

                    if is_test_call or is_route_or_cb_call:
                        if first_arg_str is not None:
                            extracted_name = first_arg_str.replace("::", " - ")
                        else:
                            extracted_name = func_text

                        name = extracted_name
                        qualified_name = f"{parent_name}.{name}" if parent_name else name
                        symbol_id = f"{file_path}::{qualified_name}"
                        start_byte = child.start_byte
                        end_byte = child.end_byte
                        start_line = child.start_point[0] + 1
                        end_line = child.end_point[0] + 1
                        signature = self._extract_signature(child, content)
                        docstring = self._extract_docstring(child, content)
                        content_hash = compute_content_hash(content[start_byte:end_byte])

                        symbols.append(
                            SymbolInfo(
                                id=symbol_id,
                                file_path=file_path,
                                parent_id=parent_id,
                                name=name,
                                kind="function",
                                signature=signature,
                                docstring=docstring,
                                start_byte=start_byte,
                                end_byte=end_byte,
                                start_line=start_line,
                                end_line=end_line,
                                content_hash=content_hash,
                            )
                        )

                        for f_arg in func_args:
                            body_node = f_arg.child_by_field_name("body")
                            if body_node:
                                seen_calls: Set[str] = set()
                                self._extract_calls(body_node, content, symbol_id, calls, seen_calls)
                                self._extract_symbols_and_calls(
                                    node=body_node,
                                    content=content,
                                    file_path=file_path,
                                    parent_id=symbol_id,
                                    parent_name=qualified_name,
                                    symbols=symbols,
                                    calls=calls,
                                )
                        continue

"""Python AST parser using Tree-sitter."""

import ast
import re
from typing import Any, List, Optional, Set, Tuple
import tree_sitter
import tree_sitter_python

from .base import BaseParser, CallInfo, ParseResult, SymbolInfo, SyntaxErrorDetail, compute_content_hash


class PythonParser(BaseParser):
    """AST parser for Python files."""

    def __init__(self) -> None:
        self._language = tree_sitter.Language(tree_sitter_python.language())
        self._parser = tree_sitter.Parser(self._language)

    def can_parse(self, file_path: str) -> bool:
        return file_path.endswith(".py") or file_path.endswith(".pyi")

    def validate_syntax(
        self, code: bytes, file_path: Optional[str] = None
    ) -> Tuple[bool, List[SyntaxErrorDetail]]:
        """Check for syntax errors in Python code using Tree-sitter and ast.parse for actionable hints."""
        # file_path is accepted for interface parity; Python has a single grammar.
        details: List[SyntaxErrorDetail] = []
        if code.startswith(b"\xef\xbb\xbf"):
            code = code[3:]
        tree = self._parser.parse(code)

        py_error_msg: Optional[str] = None
        py_error_line: Optional[int] = None
        try:
            ast.parse(code.decode("utf-8-sig", errors="replace"))
        except SyntaxError as se:
            py_error_msg = se.msg
            py_error_line = se.lineno

        def find_errors(node: Any) -> None:
            if node.type == "ERROR" or node.is_missing:
                line = node.start_point[0] + 1
                col = node.start_point[1]
                snippet = code[node.start_byte : min(node.end_byte + 20, len(code))].decode("utf-8", errors="replace").strip()

                hint = ""
                if node.is_missing:
                    hint = f"Missing required token '{node.type}'."
                elif py_error_msg and (py_error_line is None or abs(py_error_line - line) <= 1):
                    hint = f"Python syntax issue: {py_error_msg}."
                elif snippet.startswith(("def ", "class ")) and ":" not in snippet:
                    hint = "Declaration appears to be missing closing ':' or has unclosed parameter parenthesis."
                elif "(" in snippet and ")" not in snippet:
                    hint = "Unclosed parenthesis detected."
                else:
                    hint = f"Invalid syntax near '{snippet[:15]}'. Check matching punctuation and block indentation."

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

        find_errors(tree.root_node)

        if len(details) == 0 and py_error_msg is not None:
            details.append(
                SyntaxErrorDetail(
                    line=py_error_line or 1,
                    col=0,
                    message=f"Python syntax error: {py_error_msg}",
                    context_snippet="",
                    actionable_hint=f"Check Python syntax: {py_error_msg}",
                )
            )

        return (len(details) == 0, details)

    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        """Parse Python source into symbols and call relationships."""
        syntax_ok, error_details = self.validate_syntax(content)
        tree = self._parser.parse(content)

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
        # Imports region at module level (contiguous initial imports only)
        head = []
        for idx_c, c in enumerate(tree.root_node.children):
            if c.type in ("import_statement", "import_from_statement", "future_import_statement"):
                head.append(c)
            elif c.type == "comment":
                continue
            elif idx_c == 0 and c.type == "expression_statement" and all(sub.type == "string" for sub in c.children):
                # Allow module docstring before imports
                continue
            elif head:
                break
            elif c.type in ("class_definition", "function_definition", "decorated_definition"):
                break
            elif c.type == "expression_statement" and any(sub.type == "assignment" for sub in c.children):
                break
        if head:
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

        # Two `def f` in one file are legal Python, and both walk away with the same id:
        # the indexer then died on UNIQUE constraint failed: symbols.id and the SERVER NEVER
        # STARTED on that workspace. A post-pass gives each declaration that shares an id a
        # suffix derived from the declaration itself -- the same rule C# has always had.
        self._disambiguate_overloads(symbols, calls)

        return ParseResult(
            symbols=symbols,
            calls=calls,
            syntax_ok=syntax_ok,
            errors=[str(e) for e in error_details],
            error_details=error_details,
        )

    def _extract_docstring(self, body_node: Any, content: bytes) -> Optional[str]:
        """Extract docstring from class or function block node."""
        if not body_node:
            return None
        for child in body_node.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type == "string":
                        raw_str = content[sub.start_byte : sub.end_byte].decode("utf-8", errors="replace")
                        try:
                            val = ast.literal_eval(raw_str)
                            if isinstance(val, str):
                                return val.strip()
                        except Exception:
                            # Strip quotes manually if ast parsing fails
                            return raw_str.strip("'\" \n\t")
            elif child.type in ("comment", "\n"):
                continue
            else:
                break
        return None

    def _extract_signature(self, def_node: Any, content: bytes) -> str:
        """Extract the signature line(s) from def or class keyword up to the colon."""
        # Find the colon token that marks the start of the block
        colon_byte = None
        for child in def_node.children:
            if child.type == ":":
                colon_byte = child.end_byte
                break
        if colon_byte is not None:
            sig_bytes = content[def_node.start_byte : colon_byte]
            # Replace multiple newlines or indentation inside multiline signatures
            sig = sig_bytes.decode("utf-8", errors="replace").strip()
            # Collapse multiple spaces or newlines inside signature
            return " ".join(line.strip() for line in sig.splitlines())

        # Fallback to first line
        first_line = content[def_node.start_byte : def_node.end_byte].splitlines()[0]
        return first_line.decode("utf-8", errors="replace").strip().rstrip(":")

    def _extract_calls(self, node: Any, content: bytes, caller_id: str, calls: List[CallInfo], seen_calls: Set[str]) -> None:
        """Recursively collect function/method calls inside a symbol body."""
        for child in node.children:
            # Do not recurse into nested function or class definitions
            if child.type in ("function_definition", "class_definition", "decorated_definition"):
                continue

            if child.type == "call":
                fn_node = child.child_by_field_name("function")
                if fn_node:
                    full_expr = content[fn_node.start_byte : fn_node.end_byte].decode("utf-8", errors="replace").strip()
                    callee_name = full_expr
                    if full_expr.startswith("self."):
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
        """Walk AST to find class and function definitions."""
        for child in node.children:
            outer_node = child
            def_node = child

            # Handle decorated definitions (wraps decorators and the definition)
            if child.type == "decorated_definition":
                outer_node = child
                for sub in child.children:
                    if sub.type in ("class_definition", "function_definition"):
                        def_node = sub
                        break

            if def_node.type == "class_definition":
                name_node = def_node.child_by_field_name("name")
                if not name_node:
                    continue
                name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                qualified_name = f"{parent_name}.{name}" if parent_name else name
                symbol_id = f"{file_path}::{qualified_name}"

                start_byte = outer_node.start_byte
                line_start = content.rfind(b"\n", 0, start_byte)
                prefix = content[:start_byte] if line_start == -1 else content[line_start + 1 : start_byte]
                if prefix.strip() == b"":
                    start_byte = 0 if line_start == -1 else line_start + 1
                end_byte = outer_node.end_byte
                start_line = outer_node.start_point[0] + 1
                end_line = outer_node.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])

                body_node = def_node.child_by_field_name("body")
                docstring = self._extract_docstring(body_node, content)
                signature = self._extract_signature(def_node, content)

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

            elif def_node.type == "function_definition":
                name_node = def_node.child_by_field_name("name")
                if not name_node:
                    continue
                name = content[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
                qualified_name = f"{parent_name}.{name}" if parent_name else name
                symbol_id = f"{file_path}::{qualified_name}"
                parent_is_class = any(s.id == parent_id and s.kind == "class" for s in symbols) if parent_id else False
                kind = "method" if parent_is_class else "function"

                start_byte = outer_node.start_byte
                line_start = content.rfind(b"\n", 0, start_byte)
                prefix = content[:start_byte] if line_start == -1 else content[line_start + 1 : start_byte]
                if prefix.strip() == b"":
                    start_byte = 0 if line_start == -1 else line_start + 1
                end_byte = outer_node.end_byte
                start_line = outer_node.start_point[0] + 1
                end_line = outer_node.end_point[0] + 1
                content_hash = compute_content_hash(content[start_byte:end_byte])

                body_node = def_node.child_by_field_name("body")
                docstring = self._extract_docstring(body_node, content)
                signature = self._extract_signature(def_node, content)

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

                # Extract calls inside this function body
                if body_node:
                    seen_calls: Set[str] = set()
                    self._extract_calls(body_node, content, symbol_id, calls, seen_calls)

                    # Also inspect any nested classes or functions
                    self._extract_symbols_and_calls(
                        node=body_node,
                        content=content,
                        file_path=file_path,
                        parent_id=symbol_id,
                        parent_name=qualified_name,
                        symbols=symbols,
                        calls=calls,
                    )
            elif child.type in (
                "block",
                "if_statement",
                "elif_clause",
                "else_clause",
                "try_statement",
                "except_clause",
                "finally_clause",
                "with_statement",
                "for_statement",
                "while_statement",
                "match_statement",
                "case_clause",
            ):
                self._extract_symbols_and_calls(
                    node=child,
                    content=content,
                    file_path=file_path,
                    parent_id=parent_id,
                    parent_name=parent_name,
                    symbols=symbols,
                    calls=calls,
                )
            elif parent_id is None:
                # Top-level assignments (constants and module variables)
                target_node = None
                if child.type == "expression_statement":
                    for sub in child.children:
                        if sub.type == "assignment":
                            target_node = sub
                            break
                elif child.type == "assignment":
                    target_node = child

                if target_node:
                    left_node = target_node.child_by_field_name("left")
                    target_ident = None
                    if left_node:
                        if left_node.type == "identifier":
                            target_ident = left_node
                        elif left_node.type in ("pattern_list", "tuple_pattern", "list_pattern"):
                            target_ident = next(
                                (c for c in left_node.children if c.type == "identifier"),
                                None,
                            )
                    if target_ident:
                        name = content[target_ident.start_byte : target_ident.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                        symbol_id = f"{file_path}::{name}"

                        start_byte = outer_node.start_byte
                        line_start = content.rfind(b"\n", 0, start_byte)
                        prefix = (
                            content[:start_byte]
                            if line_start == -1
                            else content[line_start + 1 : start_byte]
                        )
                        if prefix.strip() == b"":
                            start_byte = 0 if line_start == -1 else line_start + 1
                        end_byte = outer_node.end_byte
                        start_line = outer_node.start_point[0] + 1
                        end_line = outer_node.end_point[0] + 1
                        content_hash = compute_content_hash(content[start_byte:end_byte])

                        slice_lines = content[start_byte:end_byte].splitlines()
                        first_line = (
                            slice_lines[0].decode("utf-8", errors="replace").strip()
                            if slice_lines
                            else name
                        )

                        symbols.append(
                            SymbolInfo(
                                id=symbol_id,
                                file_path=file_path,
                                parent_id=None,
                                name=name,
                                kind="variable",
                                signature=first_line,
                                docstring=None,
                                start_byte=start_byte,
                                end_byte=end_byte,
                                start_line=start_line,
                                end_line=end_line,
                                content_hash=content_hash,
                            )
                        )

"""AST-aware indentation derivation and re-basing utilities."""

import re
import textwrap
from typing import List, Optional, Set
import tree_sitter
import tree_sitter_c_sharp
import tree_sitter_python
import tree_sitter_typescript


class IndentationManager:
    """Manages source file indentation styles and AST-level code alignment."""

    def __init__(self) -> None:
        self._python_language = tree_sitter.Language(tree_sitter_python.language())
        self._python_parser = tree_sitter.Parser(self._python_language)
        self._ts_language = tree_sitter.Language(tree_sitter_typescript.language_typescript())
        self._tsx_language = tree_sitter.Language(tree_sitter_typescript.language_tsx())
        self._ts_parser = tree_sitter.Parser(self._ts_language)
        self._tsx_parser = tree_sitter.Parser(self._tsx_language)
        self._cs_language = tree_sitter.Language(tree_sitter_c_sharp.language())
        self._cs_parser = tree_sitter.Parser(self._cs_language)

    def _select_parser(self, file_path: Optional[str]) -> tree_sitter.Parser:
        """Pick the tree-sitter grammar matching the file extension."""
        if file_path:
            if file_path.endswith((".tsx", ".jsx")):
                return self._tsx_parser
            if file_path.endswith((".ts", ".js", ".mjs", ".cjs")):
                return self._ts_parser
            if file_path.endswith(".cs"):
                return self._cs_parser
        return self._python_parser

    def detect_file_indent_step(self, content: bytes) -> str:
        """Detect indentation step used in file (defaulting to 4 spaces)."""
        lines = content.decode("utf-8", errors="replace").splitlines()
        for line in lines:
            if line.startswith("\t"):
                return "\t"
            match = re.match(r"^( +)", line)
            if match:
                spaces = len(match.group(1))
                if spaces in (2, 4, 8):
                    return " " * spaces
        return "    "

    def _find_multiline_string_lines(self, code_bytes: bytes, file_path: Optional[str] = None) -> Set[int]:
        """Find 0-indexed line numbers that are internal lines of multiline strings."""
        multiline_lines: Set[int] = set()
        parser = self._select_parser(file_path)
        if parser is self._python_parser:
            string_types = {"string"}
        elif parser is self._cs_parser:
            string_types = {
                "string_literal",
                "verbatim_string_literal",
                "raw_string_literal",
                "interpolated_string_expression",
            }
        else:
            string_types = {"string", "template_string"}
        tree = parser.parse(code_bytes)

        def walk(node: tree_sitter.Node) -> None:
            if node.type in string_types:
                start_row = node.start_point[0]
                end_row = node.end_point[0]
                # Lines between start_row+1 and end_row (internal literal content)
                for r in range(start_row + 1, end_row + 1):
                    multiline_lines.add(r)
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return multiline_lines

    def rebase_indentation(
        self, code: str, target_col: int, indent_unit: str = "    ", file_path: Optional[str] = None
    ) -> str:
        """
        Re-base incoming code snippet to target column indentation.
        If code already matches target_col, it is returned unchanged.
        Multiline string literals are protected from internal whitespace shifts.
        """
        if not code:
            return code

        lines = code.splitlines()
        # Find first non-empty line
        first_non_empty_idx = -1
        for idx, l in enumerate(lines):
            if l.strip():
                first_non_empty_idx = idx
                break

        if first_non_empty_idx == -1:
            return code

        first_line = lines[first_non_empty_idx]
        current_indent = len(first_line) - len(first_line.lstrip(" \t"))

        if current_indent == target_col:
            return code

        # Identify multiline string internal lines before modifying
        multiline_internal = self._find_multiline_string_lines(code.encode("utf-8"), file_path)

        shift = target_col - current_indent
        result_lines: List[str] = []

        for idx, line in enumerate(lines):
            if not line.strip():
                result_lines.append("")
                continue

            if idx in multiline_internal:
                # Do not modify internal multiline string literals
                result_lines.append(line)
                continue

            if shift > 0:
                result_lines.append((" " * shift) + line)
            else:
                # Need to unindent
                to_remove = abs(shift)
                leading_spaces = len(line) - len(line.lstrip(" "))
                remove_count = min(to_remove, leading_spaces)
                result_lines.append(line[remove_count:])

        return "\n".join(result_lines)

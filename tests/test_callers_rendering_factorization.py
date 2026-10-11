"""Tests for callers factorization and compactness in render_symbol_view."""
from typing import Any, Dict

from ast_graph_server.rendering import render_symbol_view


def test_render_symbol_view_implementation_groups_callers_by_file() -> None:
    payload: Dict[str, Any] = {
        "view": "implementation",
        "file": "src/utils.py",
        "lines": [10, 20],
        "content_hash": "a1b2c3d4",
        "code": "def helper():\n    pass",
        "callers": [
            "src/services/order.py::OrderService.process",
            "src/services/order.py::OrderService.cancel",
            "src/controllers/api.py::ApiController.handle",
        ],
    }
    rendered = render_symbol_view(payload)
    first_line = rendered.splitlines()[0]

    # Multiple callers in the same file must be grouped to avoid verbose path repetitions
    assert "src/services/order.py (OrderService.process, OrderService.cancel)" in first_line
    # Single caller in a file should remain an exact address
    assert "src/controllers/api.py::ApiController.handle" in first_line
    # File path must not be repeated
    assert first_line.count("src/services/order.py") == 1


def test_render_symbol_view_contract_groups_callers_by_file_and_preserves_raw() -> None:
    payload: Dict[str, Any] = {
        "view": "contract",
        "signature": "def calculate_total(items: list) -> float",
        "docstring": "Compute order total.",
        "callers": [
            "src/parsers/python_parser.py::PythonParser._extract_symbols",
            "src/parsers/python_parser.py::PythonParser.parse_file",
            "external_runner",
        ],
    }
    rendered = render_symbol_view(payload)
    callers_line = next(line for line in rendered.splitlines() if "callers:" in line)

    # Grouped members
    assert "src/parsers/python_parser.py (PythonParser._extract_symbols, PythonParser.parse_file)" in callers_line
    # Raw caller without '::' displayed directly
    assert "external_runner" in callers_line
    # File path must appear exactly once
    assert callers_line.count("src/parsers/python_parser.py") == 1


def test_render_symbol_view_single_caller_preserved() -> None:
    payload: Dict[str, Any] = {
        "view": "contract",
        "signature": "def single_callee() -> None",
        "callers": ["src/app.py::main_process"],
    }
    rendered = render_symbol_view(payload)
    callers_line = next(line for line in rendered.splitlines() if "callers:" in line)
    assert "src/app.py::main_process" in callers_line
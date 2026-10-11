import json
from pathlib import Path
import pytest

from tests.test_documents import make_env
from ast_graph_server.server import ASTGraphServer


@pytest.mark.anyio
async def test_insert_symbol_after_specific_child_anchor(tmp_path: Path):
    """insert_block with anchor pointing to a specific child method must place the new method right after it."""
    code = (
        "class Calculator:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "\n"
        "    def multiply(self, a, b):\n"
        "        return a * b\n"
    )
    ws, config, db, indexer = make_env(tmp_path, {"src/calc.py": code})
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    # We want to insert 'subtract' right after 'add' and before 'multiply'
    new_method = (
        "def subtract(self, a, b):\n"
        "    return a - b\n"
    )
    res = await server.mcp.call_tool(
        "insert_block",
        {
            "parent": "src/calc.py::Calculator",
            "anchor": "src/calc.py::Calculator::add",
            "text": new_method,
        },
    )
    payload = json.loads(res.content[0].text) if hasattr(res.content[0], "text") and res.content[0].text.startswith("{") else res.content[0].text
    if isinstance(payload, str):
        payload = json.loads(payload)

    assert payload.get("status") == "success", payload

    updated_code = (ws / "src/calc.py").read_text(encoding="utf-8")
    # Verify exact ordering: add -> subtract -> multiply
    pos_add = updated_code.index("def add")
    pos_sub = updated_code.index("def subtract")
    pos_mul = updated_code.index("def multiply")

    assert pos_add < pos_sub < pos_mul, f"Expected 'subtract' between 'add' and 'multiply', got:\n{updated_code}"


@pytest.mark.anyio
async def test_insert_symbol_with_anchor_not_child_refuses(tmp_path: Path):
    """insert_block must cleanly refuse when anchor is not a child of parent."""
    code = (
        "class A:\n"
        "    def m_a(self):\n"
        "        pass\n"
        "\n"
        "class B:\n"
        "    def m_b(self):\n"
        "        pass\n"
    )
    ws, config, db, indexer = make_env(tmp_path, {"src/test.py": code})
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    res = await server.mcp.call_tool(
        "insert_block",
        {
            "parent": "src/test.py::A",
            "anchor": "src/test.py::B::m_b",
            "text": "def m_new(self):\n    pass\n",
        },
    )
    payload = json.loads(res.content[0].text) if hasattr(res.content[0], "text") and res.content[0].text.startswith("{") else res.content[0].text
    if isinstance(payload, str):
        payload = json.loads(payload)

    assert payload.get("status") == "error", payload
    assert "anchor" in payload.get("error", "").lower() or "child" in payload.get("error", "").lower()


@pytest.mark.anyio
async def test_insert_symbol_multi_methods_in_single_call(tmp_path: Path):
    """insert_block must support injecting multiple sibling methods in one call."""
    code = (
        "class Service:\n"
        "    def start(self):\n"
        "        pass\n"
    )
    ws, config, db, indexer = make_env(tmp_path, {"src/service.py": code})
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    multi_code = (
        "def pause(self):\n"
        "    pass\n"
        "\n"
        "def resume(self):\n"
        "    pass\n"
    )
    res = await server.mcp.call_tool(
        "insert_block",
        {
            "parent": "src/service.py::Service",
            "text": multi_code,
        },
    )
    payload = json.loads(res.content[0].text) if hasattr(res.content[0], "text") and res.content[0].text.startswith("{") else res.content[0].text
    if isinstance(payload, str):
        payload = json.loads(payload)

    assert payload.get("status") == "success", payload
    assert len(payload.get("created_symbol_ids", [])) == 2
    assert any("pause" in s for s in payload["created_symbol_ids"])
    assert any("resume" in s for s in payload["created_symbol_ids"])

    symbols = db.get_symbols_for_file("src/service.py")
    sym_names = [s["name"] for s in symbols]
    assert "pause" in sym_names
    assert "resume" in sym_names
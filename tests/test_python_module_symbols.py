import pytest
from ast_graph_server.parsers.python_parser import PythonParser


def test_python_parser_indexes_module_constants_and_imports():
    parser = PythonParser()
    code = (
        "import os\n"
        "from typing import List, Optional\n"
        "\n"
        "MY_CONSTANT = 42\n"
        "SCHEMAS = [{'name': 'test'}]\n"
        "\n"
        "def helper() -> int:\n"
        "    return MY_CONSTANT\n"
    ).encode("utf-8")

    result = parser.parse_file("sample.py", code)
    assert result.syntax_ok
    symbols_by_name = {s.name: s for s in result.symbols}

    # 1. Imports region
    assert "imports" in symbols_by_name, "La région 'imports' doit être indexée"
    assert symbols_by_name["imports"].kind == "imports"
    assert symbols_by_name["imports"].id == "sample.py::imports"

    # 2. Module variables / constants
    assert "MY_CONSTANT" in symbols_by_name, "La constante MY_CONSTANT doit être indexée"
    assert symbols_by_name["MY_CONSTANT"].kind == "variable"
    assert symbols_by_name["MY_CONSTANT"].id == "sample.py::MY_CONSTANT"

    assert "SCHEMAS" in symbols_by_name, "La constante SCHEMAS doit être indexée"
    assert symbols_by_name["SCHEMAS"].kind == "variable"
    assert symbols_by_name["SCHEMAS"].id == "sample.py::SCHEMAS"


def test_python_parser_edge_cases():
    parser = PythonParser()
    code = (
        '"""Module docstring."""\n'
        'import os\n'
        'import sys\n'
        '\n'
        'X: int = 10\n'
        'A, B = 1, 2\n'
        'CONFIG = {\n'
        '    "key": "val",\n'
        '}\n'
        '\n'
        'def func():\n'
        '    y = 20  # local variable, must not be indexed\n'
        '    return y\n'
    ).encode("utf-8")

    result = parser.parse_file("edge.py", code)
    assert result.syntax_ok
    symbols_by_name = {s.name: s for s in result.symbols}

    assert "imports" in symbols_by_name
    assert symbols_by_name["imports"].start_line == 2
    assert symbols_by_name["imports"].end_line == 3

    assert "X" in symbols_by_name
    assert symbols_by_name["X"].kind == "variable"

    assert "A" in symbols_by_name
    assert symbols_by_name["A"].kind == "variable"

    assert "CONFIG" in symbols_by_name
    assert symbols_by_name["CONFIG"].kind == "variable"
    assert symbols_by_name["CONFIG"].start_line == 7
    assert symbols_by_name["CONFIG"].end_line == 9

    assert "y" not in symbols_by_name, "Local variable must not be indexed"


def test_read_and_edit_python_imports_and_constants(tmp_path):
    from pathlib import Path
    from ast_graph_server.config import ServerConfig
    from ast_graph_server.database import DatabaseManager
    from ast_graph_server.indexer import Indexer
    from ast_graph_server.patcher import Patcher

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    code_path = ws / "src" / "config.py"
    code_path.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "DEFAULT_TIMEOUT = 30\n"
        "SCHEMAS = [{'name': 'default'}]\n"
        "\n"
        "def get_timeout():\n"
        "    return DEFAULT_TIMEOUT\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)

    # 1. read_block on ::imports
    imports_block = patcher.read_block("src/config.py::imports")
    assert imports_block["status"] == "success"
    assert "import os" in imports_block["numbered_text"]
    assert "import sys" in imports_block["numbered_text"]

    # 2. edit_block on ::imports
    edit_imp = patcher.edit_block(
        "src/config.py::imports",
        ops=[{"replace": [1, 2], "content": "import os\nimport sys\nimport time"}],
        expected_hash=imports_block["content_hash"],
    )
    assert edit_imp["status"] == "success"
    assert "import time" in code_path.read_text(encoding="utf-8")

    # 3. read_block on variable
    var_block = patcher.read_block("src/config.py::DEFAULT_TIMEOUT")
    assert var_block["status"] == "success"
    assert "DEFAULT_TIMEOUT = 30" in var_block["numbered_text"]

    # 4. edit_block on variable
    edit_var = patcher.edit_block(
        "src/config.py::DEFAULT_TIMEOUT",
        ops=[{"replace": [1, 1], "content": "DEFAULT_TIMEOUT = 60"}],
        expected_hash=var_block["content_hash"],
    )
    assert edit_var["status"] == "success"
    assert "DEFAULT_TIMEOUT = 60" in code_path.read_text(encoding="utf-8")

    # 5. search_symbols finds DEFAULT_TIMEOUT and SCHEMAS
    results = db.search_symbols("DEFAULT_TIMEOUT")
    assert len(results) == 1
    assert results[0]["symbol_id"] == "src/config.py::DEFAULT_TIMEOUT"
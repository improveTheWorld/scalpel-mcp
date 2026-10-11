import pytest
from pathlib import Path
from src.ast_graph_server.parsers.typescript_parser import TypeScriptParser
from src.ast_graph_server.config import ServerConfig
from src.ast_graph_server.database import DatabaseManager
from src.ast_graph_server.indexer import Indexer
from src.ast_graph_server.patcher import Patcher

def test_typescript_exports_and_arrow_functions():
    p = TypeScriptParser()
    code = b'''
export const VERSION = '1.0.0';
export * from './user';
export { auth, login as doLogin } from './auth';

export const createLedger = (opts = {}) => {
    return { entries: [] };
};

export let count = 0, increment = () => { count++; };

export default {
    createLedger,
    count
};
'''
    res = p.parse_file("test.js", code)
    assert res.syntax_ok
    sym_map = {s.id: s for s in res.symbols}

    assert "test.js::VERSION" in sym_map
    assert sym_map["test.js::VERSION"].kind == "variable"

    assert "test.js::createLedger" in sym_map
    assert sym_map["test.js::createLedger"].kind == "function"

    assert "test.js::auth" in sym_map
    assert "test.js::doLogin" in sym_map

    assert "test.js::count" in sym_map
    assert "test.js::increment" in sym_map
    assert sym_map["test.js::increment"].kind == "function"

    assert "test.js::default" in sym_map

def test_ledger_js_end_to_end_read_and_edit(tmp_path: Path):
    cfg = ServerConfig(workspace_root=tmp_path, db_path=tmp_path / "test.db")
    db = DatabaseManager(cfg.db_path, cfg.workspace_root)
    indexer = Indexer(cfg, db)
    patcher = Patcher(cfg, db, indexer)

    ledger_path = tmp_path / "ledger.js"
    ledger_path.write_text('''export const createLedger = () => {
  return { entries: [] };
};

export const addEntry = (ledger, entry) => {
  ledger.entries.push(entry);
};
''', encoding="utf-8")

    index_path = tmp_path / "index.js"
    index_path.write_text('''export const VERSION = '1.0.0';
export { createLedger, addEntry } from './ledger';
''', encoding="utf-8")

    indexer.index_workspace()

    # read_block on ledger.js lists symbols
    r_ledger = patcher.read_block("ledger.js")
    assert r_ledger["status"] == "success", r_ledger
    assert "createLedger" in r_ledger["text"]
    assert "addEntry" in r_ledger["text"]

    # read_block on index.js lists exports
    r_index = patcher.read_block("index.js")
    assert r_index["status"] == "success", r_index
    assert "VERSION" in r_index["text"]
    assert "createLedger" in r_index["text"]

    # edit_block on ledger.js::createLedger works
    sym = patcher.read_block("ledger.js::createLedger")
    assert sym["status"] == "success"
    edit_res = patcher.edit_block(
        "ledger.js::createLedger",
        ops=[{"replace": [2, 2], "content": "  return { entries: [], active: true };"}],
        expected_hash=sym["content_hash"],
    )
    assert edit_res["status"] == "success", edit_res
    assert "active: true" in ledger_path.read_text(encoding="utf-8")

"""init / doctor: the install-time commands."""

import json
import sys

from ast_graph_server import setup_cmds


def test_init_writes_absolute_config_and_gitignore(tmp_path, capsys):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert setup_cmds.dispatch(["init", "--workspace", str(tmp_path)]) == 0
    entry = json.loads((tmp_path / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"][
        "ast-code-graph"
    ]
    assert entry["command"] == sys.executable
    assert entry["args"][-1] == str(tmp_path.resolve())
    assert ".agent_graph.db*" in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_init_keeps_other_servers_and_refuses_overwrite_without_force(tmp_path):
    cfg = tmp_path / ".agents" / "mcp_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}, "ast-code-graph": {"command": "old"}}}))
    assert setup_cmds.dispatch(["init", "--workspace", str(tmp_path)]) == 1
    assert json.loads(cfg.read_text())["mcpServers"]["ast-code-graph"]["command"] == "old"
    assert setup_cmds.dispatch(["init", "--workspace", str(tmp_path), "--force"]) == 0
    servers = json.loads(cfg.read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["ast-code-graph"]["command"] == sys.executable


def test_init_refuses_invalid_json_without_touching_it(tmp_path):
    cfg = tmp_path / ".agents" / "mcp_config.json"
    cfg.parent.mkdir()
    cfg.write_text("{not json")
    assert setup_cmds.dispatch(["init", "--workspace", str(tmp_path)]) == 2
    assert cfg.read_text() == "{not json"


def test_doctor_healthy_workspace(tmp_path, capsys):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    setup_cmds.dispatch(["init", "--workspace", str(tmp_path)])
    assert setup_cmds.dispatch(["doctor", "--workspace", str(tmp_path)]) == 0
    assert "healthy" in capsys.readouterr().out


def test_doctor_fails_on_empty_workspace(tmp_path, capsys):
    assert setup_cmds.dispatch(["doctor", "--workspace", str(tmp_path)]) == 1
    assert "none found" in capsys.readouterr().out


def test_doctor_fails_on_unexpanded_placeholder(capsys):
    assert setup_cmds.dispatch(["doctor", "--workspace", "${workspaceFolder}"]) == 1
    assert "placeholder" in capsys.readouterr().out


def test_doctor_flags_relative_interpreter_and_placeholder_in_config(tmp_path, capsys):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    cfg = tmp_path / ".agents" / "mcp_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"ast-code-graph": {
        "command": "python", "args": ["-m", "ast_graph_server", "--workspace", "${workspaceFolder}"]}}}))
    assert setup_cmds.dispatch(["doctor", "--workspace", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "unexpanded" in out and "not an absolute path" in out


def test_doctor_accepts_mcp_config_with_utf8_bom(tmp_path, capsys):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    cfg = tmp_path / ".agents" / "mcp_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8-sig")
    setup_cmds.dispatch(["doctor", "--workspace", str(tmp_path)])
    assert "not valid JSON" not in capsys.readouterr().out


def test_dispatch_ignores_server_arguments():
    assert setup_cmds.dispatch(["--workspace", "."]) is None
    assert setup_cmds.dispatch([]) is None

"""End-to-end integration tests for ASTGraphServer running over stdio transport."""

import json
from pathlib import Path
import subprocess
import sys
import pytest


def test_stdio_jsonrpc_handshake_and_tools(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    src = ws / "src"
    src.mkdir()

    billing_file = src / "billing.py"
    billing_file.write_text(
        '''class BillingService:
    def charge(self, user_id: str, amount: float):
        """Process charge."""
        if not self.validate_user(user_id):
            return None
        return "receipt_123"

    def validate_user(self, user_id: str) -> bool:
        return True
''',
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ast_graph_server",
            "--workspace",
            str(ws),
            "--no-watch",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
    )

    def send_msg(msg: dict) -> None:
        data = json.dumps(msg) + "\n"
        proc.stdin.write(data)
        proc.stdin.flush()

    def read_msg() -> dict:
        while True:
            line = proc.stdout.readline()
            if not line:
                stderr = proc.stderr.read()
                raise RuntimeError(f"Subprocess terminated unexpectedly. Stderr: {stderr}")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    try:
        # 1. Initialize
        send_msg(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            }
        )
        init_resp = read_msg()
        assert init_resp.get("id") == 1
        assert "capabilities" in init_resp["result"]

        # 2. Initialized notification
        send_msg({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. List tools
        send_msg({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_resp = read_msg()
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        assert "search_symbols" in tool_names
        assert "delete_block" in tool_names
        assert "read_block" in tool_names
        assert "edit_block" in tool_names
        assert "insert_block" in tool_names
        # The surface is frozen at nine tools: the eight this server registers, plus `build`,
        # which the harness runs itself. An exact set, so an orphan decorator or a
        # half-absorbed tool can never hide behind a bare `in` assertion again.
        assert sorted(tool_names) == [
            "build_context",
            "delete_block",
            "edit_block",
            "get_project_map",
            "insert_block",
            "read_block",
            "search_documents",
            "search_symbols",
        ]

        # 4. Call search_symbols
        send_msg(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_symbols",
                    "arguments": {"query": "charge", "format": "json"},
                },
            }
        )
        search_resp = read_msg()
        search_content = json.loads(search_resp["result"]["content"][0]["text"])
        matches = search_content["matches"]
        assert len(matches) >= 1
        assert matches[0]["symbol_id"] == "src/billing.py::BillingService.charge"

        # 5. read_block by paths (the absorbed batch read)
        send_msg(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_block",
                    "arguments": {"paths": ["src/billing.py::BillingService.charge"], "format": "json"},
                },
            }
        )
        inspect_resp = read_msg()
        inspect_content = json.loads(inspect_resp["result"]["content"][0]["text"])
        assert inspect_content["blocks"][0]["status"] == "success"
        assert "def charge" in inspect_content["blocks"][0]["numbered_text"]

    finally:
        proc.terminate()
        proc.wait(timeout=3)

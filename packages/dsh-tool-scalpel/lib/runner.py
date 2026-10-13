import asyncio
import json
from pathlib import Path
import re
import sys

repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
if str(repo_root / "src") not in sys.path:
    sys.path.insert(0, str(repo_root / "src"))

# Force UTF-8 stdout on Windows to prevent charmap/cp1252 encode crashes
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer
from ast_graph_server.toolset_schemas import SCALPEL_SCHEMAS

def _plain_tool_error(name: str, exc: Exception, accepted: list) -> str:
    text = str(exc)
    if text.strip().startswith("Error executing tool"):
        return (
            name + " failed INSIDE the tool -- this is NOT a parameter problem, your arguments "
            "were accepted. Retry the same call once; if it fails again, reach the same block "
            "another way (read the whole file, or find it with search_documents)."
        )
    if "validation error for" in text:
        named = sorted({key for key in accepted if re.search(r"\b" + re.escape(key) + r"\b", text)})
        detail = (
            "the value of " + ", ".join(named) + " has the wrong type or shape"
            if named else "an argument has the wrong type or shape"
        )
        return name + " refused the call: " + detail + " -- it accepts " + ", ".join(accepted) + "."
    first = text.split("For further information visit", 1)[0].strip()
    first = re.sub(r"\s*\n\s*", " ", first)
    return (
        name + " refused the call: " + first[:400]
        + " -- it accepts " + ", ".join(accepted) + "."
    )

def main():
    if len(sys.argv) < 3:
        print(json.dumps({"status": "error", "error": "Insufficient arguments"}))
        sys.exit(1)

    workspace = sys.argv[1]
    cmd = sys.argv[2]
    if len(sys.argv) > 3:
        raw = sys.argv[3]
    elif sys.stdin is None or sys.stdin.isatty():
        raw = ""
    else:
        raw = sys.stdin.read()

    if raw.strip():
        try:
            args = json.loads(raw)
        except Exception as e:
            print(json.dumps({"status": "error", "error": f"Invalid args JSON: {e}"}))
            sys.exit(1)
    else:
        args = {}

    cfg = ServerConfig.from_env(workspace_arg=workspace)
    cfg.answer_format = "text"
    server = ASTGraphServer(cfg)

    if cmd in ("read_block", "edit_block", "insert_block", "delete_block"):
        candidates = []
        for key in ("path", "parent", "parent_id", "block_id"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val)
        paths_val = args.get("paths")
        if isinstance(paths_val, list):
            for val in paths_val:
                if isinstance(val, str) and val.strip():
                    candidates.append(val)

        for cand in candidates:
            clean_path = cand.split("::")[0].split("#")[0].strip()
            if not clean_path or clean_path.startswith(("\\\\", "//")):
                continue
            try:
                abs_p = cfg.to_absolute_path(clean_path)
                if abs_p.is_file() and not server.indexer.should_ignore(abs_p):
                    rel_p = cfg.to_relative_posix(abs_p)
                    if server.indexer._is_supported(rel_p):
                        server.indexer.index_file(abs_p)
            except Exception:
                pass

    if cmd == "edit_block":
        raw_ops = args.get("ops")
        if isinstance(raw_ops, list):
            new_ops = []
            for op in raw_ops:
                if isinstance(op, str):
                    try:
                        new_ops.append(json.loads(op))
                    except Exception:
                        new_ops.append(op)
                else:
                    new_ops.append(op)
            args["ops"] = new_ops

    tool_schemas = {
        item["function"]["name"]: item["function"]
        for item in SCALPEL_SCHEMAS
    }

    if cmd in tool_schemas:
        schema = tool_schemas[cmd]
        parameters = schema.get("parameters") or {}
        accepted = sorted(parameters.get("properties") or {})
        try:
            result = asyncio.run(server.mcp.call_tool(cmd, args))
            if hasattr(result, "content") and result.content:
                print(result.content[0].text, end="")
            else:
                print(json.dumps(result, default=str), end="")
        except Exception as exc:
            err_msg = _plain_tool_error(cmd, exc, accepted)
            print(json.dumps({"status": "error", "error": err_msg}), end="")
    else:
        print(json.dumps({"status": "error", "error": f"Unknown command: {cmd}"}), end="")

if __name__ == "__main__":
    main()

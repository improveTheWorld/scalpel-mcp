import asyncio
import json
import logging
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
            "another way (e.g. read_block on the parent file or directory)."
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

def _normalize_edit_ops(ops_list):
    if not isinstance(ops_list, list):
        return ops_list
    new_ops = []
    for op in ops_list:
        if isinstance(op, str):
            try:
                new_ops.append(json.loads(op))
            except Exception:
                new_ops.append(op)
        else:
            new_ops.append(op)
    return new_ops


def dispatch_command(server, cfg, tool_schemas, cmd: str, args: dict, mtime_cache=None, loop=None) -> str:
    if not isinstance(args, dict):
        args = {}

    for list_key in ("ops", "edits", "inserts", "paths"):
        raw_val = args.get(list_key)
        if isinstance(raw_val, str):
            try:
                parsed_val = json.loads(raw_val)
                if isinstance(parsed_val, list):
                    args[list_key] = parsed_val
            except Exception:
                pass
        if list_key in ("edits", "inserts") and isinstance(args.get(list_key), list):
            norm_entries = []
            for entry in args[list_key]:
                if isinstance(entry, str):
                    try:
                        parsed_entry = json.loads(entry)
                        norm_entries.append(parsed_entry if isinstance(parsed_entry, dict) else entry)
                    except Exception:
                        norm_entries.append(entry)
                else:
                    norm_entries.append(entry)
            args[list_key] = norm_entries

    mutated_abs_paths = []
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
        edits_val = args.get("edits")
        if isinstance(edits_val, list):
            for entry in edits_val:
                if isinstance(entry, dict):
                    val = entry.get("path")
                    if isinstance(val, str) and val.strip():
                        candidates.append(val)
        inserts_val = args.get("inserts")
        if isinstance(inserts_val, list):
            for entry in inserts_val:
                if isinstance(entry, dict):
                    val = entry.get("parent") or entry.get("parent_id") or entry.get("path")
                    if isinstance(val, str) and val.strip():
                        candidates.append(val)
        server.indexer._ensure_hashes_loaded()
        for cand in candidates:
            clean_path = cand.split("::")[0].split("#")[0].strip()
            if not clean_path or clean_path.startswith(("\\\\", "//")):
                continue
            try:
                abs_p = cfg.to_absolute_path(clean_path)
                abs_key = str(abs_p)
                rel_p = cfg.to_relative_posix(abs_p)
                mutated_abs_paths.append(abs_p)
                if abs_p.is_file() and server.indexer.ignore_pattern_for(rel_p) is None:
                    if server.indexer._is_supported(rel_p):
                        st = abs_p.stat()
                        sig = (st.st_mtime_ns, st.st_size)
                        if (
                            mtime_cache is None
                            or mtime_cache.get(abs_key) != sig
                            or rel_p not in server.indexer._file_hashes
                        ):
                            server.indexer.index_file(abs_p)
                            if mtime_cache is not None:
                                mtime_cache[abs_key] = sig
                elif not abs_p.exists():
                    if (
                        (mtime_cache is not None and abs_key in mtime_cache)
                        or rel_p in server.indexer._file_hashes
                    ):
                        server.indexer.remove_file(abs_p)
                        if mtime_cache is not None:
                            mtime_cache.pop(abs_key, None)
            except Exception:
                pass

    if cmd == "edit_block":
        if "ops" in args and isinstance(args.get("ops"), list):
            args["ops"] = _normalize_edit_ops(args["ops"])
        if "edits" in args and isinstance(args.get("edits"), list):
            for entry in args["edits"]:
                if isinstance(entry, dict):
                    if isinstance(entry.get("ops"), str):
                        try:
                            parsed_ops = json.loads(entry["ops"])
                            if isinstance(parsed_ops, list):
                                entry["ops"] = parsed_ops
                        except Exception:
                            pass
                    if isinstance(entry.get("ops"), list):
                        entry["ops"] = _normalize_edit_ops(entry["ops"])
    if cmd in tool_schemas:
        schema = tool_schemas[cmd]
        parameters = schema.get("parameters") or {}
        accepted = sorted(parameters.get("properties") or {})
        try:
            coro = server.mcp.call_tool(cmd, args)
            if loop is not None:
                result = loop.run_until_complete(coro)
            else:
                result = asyncio.run(coro)
            if hasattr(result, "content") and result.content:
                out = result.content[0].text
            else:
                out = json.dumps(result, default=str)
        except Exception as exc:
            err_msg = _plain_tool_error(cmd, exc, accepted)
            out = json.dumps({"status": "error", "error": err_msg})
    else:
        out = json.dumps({"status": "error", "error": f"Unknown command: {cmd}"})

    if cmd in ("edit_block", "insert_block", "delete_block") and mtime_cache is not None:
        for abs_p in mutated_abs_paths:
            mtime_cache.pop(str(abs_p), None)

    return out


def _get_or_create_server(servers: dict, workspace: str):
    ws_key = str(Path(workspace).resolve())
    entry = servers.get(ws_key)
    if entry is not None:
        return entry
    cfg = ServerConfig.from_env(workspace_arg=workspace)
    cfg.answer_format = "text"
    server = ASTGraphServer(cfg)
    try:
        has_valid_index = any(
            server.db._inside_workspace(p) for p in server.db.get_indexed_paths()
        )
        if not has_valid_index:
            server.indexer.index_workspace(force=False)
    except Exception:
        pass
    mtime_cache = {}
    entry = (cfg, server, mtime_cache)
    servers[ws_key] = entry
    return entry


def run_daemon(tool_schemas: dict):
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    servers = {}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        while True:
            raw_line = sys.stdin.readline()
            if raw_line == "":
                break
            line = raw_line.strip()
            if not line:
                continue
            req_id = None
            try:
                req = json.loads(line)
                req_id = req.get("id")
                workspace = req.get("workspace")
                cmd = req.get("cmd")
                args = req.get("args") or {}
                if not workspace or not cmd:
                    resp = {"id": req_id, "ok": False, "error": "Insufficient arguments"}
                else:
                    cfg, server, mtime_cache = _get_or_create_server(servers, workspace)
                    output = dispatch_command(
                        server, cfg, tool_schemas, cmd, args, mtime_cache=mtime_cache, loop=loop
                    )
                    resp = {"id": req_id, "ok": True, "result": output}
            except Exception as exc:
                resp = {"id": req_id, "ok": False, "error": str(exc)}
            real_stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            real_stdout.flush()
    finally:
        sys.stdout = real_stdout
        try:
            loop.close()
        except Exception:
            pass


def main():
    tool_schemas = {
        item["function"]["name"]: item["function"]
        for item in SCALPEL_SCHEMAS
    }

    if len(sys.argv) >= 2 and sys.argv[1] == "--daemon":
        run_daemon(tool_schemas)
        return

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

    servers = {}
    cfg, server, mtime_cache = _get_or_create_server(servers, workspace)
    out = dispatch_command(server, cfg, tool_schemas, cmd, args, mtime_cache=mtime_cache)
    print(out, end="")

if __name__ == "__main__":
    main()

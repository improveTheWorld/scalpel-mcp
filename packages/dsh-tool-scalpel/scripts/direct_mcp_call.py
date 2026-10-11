import asyncio
import json
from pathlib import Path
import sys

repo_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "src"))

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer

workspace = sys.argv[1]
name = sys.argv[2]
args_json = sys.argv[3] if len(sys.argv) > 3 else sys.stdin.read()
args = json.loads(args_json) if args_json else {}

cfg = ServerConfig.from_env(workspace_arg=workspace)
cfg.answer_format = "text"
server = ASTGraphServer(cfg)

for key in ("path", "parent", "parent_id", "block_id"):
    val = args.get(key)
    if isinstance(val, str) and val.strip():
        clean_path = val.split("::")[0].split("#")[0].strip()
        try:
            abs_p = cfg.to_absolute_path(clean_path)
            if abs_p.is_file() and not server.indexer.should_ignore(abs_p):
                rel_p = cfg.to_relative_posix(abs_p)
                if server.indexer._is_supported(rel_p):
                    server.indexer.index_file(abs_p)
        except Exception:
            pass

result = asyncio.run(server.mcp.call_tool(name, args))
if hasattr(result, "content") and result.content:
    print(result.content[0].text, end="")
else:
    print(json.dumps(result, default=str), end="")
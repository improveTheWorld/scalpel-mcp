import json
from pathlib import Path
import sys

repo_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "src"))

from benchmarks.toolsets import SCALPEL_SCHEMAS  # noqa: E402

tools = []
for item in SCALPEL_SCHEMAS:
    fn = item["function"]
    tools.append({
        "name": fn["name"],
        "description": fn["description"],
        "parameters": fn["parameters"],
    })

payload = {
    "source": "benchmarks/toolsets.py::SCALPEL_SCHEMAS",
    "tools": tools,
}

print(json.dumps(payload, indent=2, ensure_ascii=False))
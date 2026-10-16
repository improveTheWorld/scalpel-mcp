import json
from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parent.parent.parent.parent
if (repo_root.parent / "scalpel-benchmarks").is_dir():
    sys.path.insert(0, str(repo_root.parent / "scalpel-benchmarks"))
sys.path.insert(0, str(repo_root / "src"))
sys.path.insert(0, str(repo_root))

try:
    from benchmarks.runner import SCALPEL_SYSTEM_PROMPT
except Exception as e:
    sys.stderr.write(f"Failed to import SCALPEL_SYSTEM_PROMPT: {e}\n")
    sys.exit(1)

start_marker = "This project is pre-indexed by Scalpel"
step5_marker = "5. BEFORE you finish"
budget_marker = "Budget:"

start_idx = SCALPEL_SYSTEM_PROMPT.find(start_marker)
if start_idx == -1:
    sys.stderr.write(f"Start marker not found: {start_marker}\n")
    sys.exit(1)

sub = SCALPEL_SYSTEM_PROMPT[start_idx:]

step5_idx = sub.find(step5_marker)
if step5_idx == -1:
    sys.stderr.write(f"Step 5 marker not found: {step5_marker}\n")
    sys.exit(1)

budget_idx = sub.find(budget_marker)
if budget_idx == -1:
    sys.stderr.write(f"Budget marker not found: {budget_marker}\n")
    sys.exit(1)

text = sub[:step5_idx] + sub[budget_idx:]

payload = {
    "source": "benchmarks/runner.py::SCALPEL_SYSTEM_PROMPT (étapes 1-4 + Budget)",
    "text": text,
}

sys.stdout.buffer.write((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
sys.exit(0)

print(json.dumps(payload, indent=2, ensure_ascii=False))
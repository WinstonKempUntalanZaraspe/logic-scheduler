from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models import BusyBlock, Task
from app.scheduler import plan
data = json.loads((ROOT / "sample_data" / "basic_day.json").read_text())

tasks = [Task(**row) for row in data["tasks"]]
busy = [BusyBlock(datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"]),
                  row["label"], row.get("source", "fixed")) for row in data["busy"]]

segments, warnings, diagnostics = plan(
    tasks, data["meta"], busy, datetime.fromisoformat(data["start"]),
    int(data["horizon_days"]), data["config"], {}
)

print("SCHEDULE")
for s in segments:
    print(f"{s.start.isoformat()} -> {s.end.isoformat()}  {s.title}")

print("\nWARNINGS")
for w in warnings:
    print("-", w)

by_id = {}
for s in segments:
    by_id.setdefault(s.task_id, []).append(s)

assert "abandoned" not in by_id
assert "read" in by_id and "problems" in by_id
assert max(s.end for s in by_id["read"]) <= min(s.start for s in by_id["problems"])
for s in segments:
    for b in busy:
        assert s.end <= b.start or s.start >= b.end

print("\nCHECKS")
print("dependency order: OK")
print("hard-block overlap: OK")
print("abandoned-task filter: OK")
print("engine:", diagnostics.get("engine"))

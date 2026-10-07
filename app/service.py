"""Tiny provenance helper required by scheduler.py."""
from .models import Task

def _source_id_from_session(task: Task) -> str | None:
    for line in (task.content or "").splitlines():
        if line.startswith("AutoSchedulerSource:"):
            return line.split(":", 1)[1].strip()
    return None

def _owned_generated_session(task: Task) -> bool:
    if "autoscheduler-session" not in {x.lower() for x in task.tags} or not _source_id_from_session(task):
        return False
    if task.desc.strip() or task.items or task.parent_id or task.repeat_flag:
        return False
    lines = (task.content or "").splitlines()
    if not any(line.startswith("AutoSchedulerPlan:") and line.split(":", 1)[1].strip() for line in lines):
        return False
    return all(
        not line.strip()
        or line.startswith(("AutoSchedulerSource:", "AutoSchedulerPlan:"))
        or line == "Time block only. Completing this session does not imply the source task is complete."
        for line in lines
    )

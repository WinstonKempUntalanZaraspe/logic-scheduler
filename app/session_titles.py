"""Display managed source names without reusing an old plan's session suffix."""
import json
import re


def task_base_title(task):
    title = task.title
    if not task.is_actionable:
        return title
    saved_base = saved_final = None
    for line in (task.content or '').splitlines():
        if line.startswith('AutoSchedulerBaseTitle:'):
            try:
                value = json.loads(line.split(':', 1)[1])
                if isinstance(value, str):
                    saved_base = value
            except (ValueError, TypeError):
                pass
        elif line.startswith('AutoSchedulerFinalSession:'):
            saved_final = line.split(':', 1)[1].strip()
    match = re.fullmatch(r'([1-9]\d*)/([1-9]\d*)', saved_final or '')
    if (saved_base is not None and match and match[1] == match[2]
            and title == f'{saved_base} [{saved_final}]'):
        return saved_base
    return title

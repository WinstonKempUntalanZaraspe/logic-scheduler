"""Safety refinement for focus-day triage.

Support steps such as travel/change/shower are controlled by the outing bundle's main
activity. Do not label them as separately deferred when the focused activity stays today.
"""
from __future__ import annotations

from . import decision_patch as _dp
from . import reality_patch as _rp

_BASE_PREPARE = _dp._prepare_meta


def safe_prepare_meta(tasks, meta_map, start, config, busy=None):
    metas, estimates, deferred = _BASE_PREPARE(tasks, meta_map, start, config, busy=busy)
    original = meta_map or {}
    support_titles = set()
    for task in tasks:
        if not _rp._is_support(task):
            continue
        support_titles.add(task.title)
        # Undo focus-only deferral on support tasks. The reality bundle will follow
        # the activity's placement and preserve the physical order.
        before = dict(original.get(task.id, {}))
        after = dict(metas.get(task.id, {}))
        if "earliest" in before:
            after["earliest"] = before["earliest"]
        else:
            after.pop("earliest", None)
        if "timing" in before:
            after["timing"] = before["timing"]
        elif after.get("timing") == "late":
            after.pop("timing", None)
        metas[task.id] = after
    deferred = [x for x in deferred if x not in support_titles]
    return metas, estimates, deferred


_dp._prepare_meta = safe_prepare_meta

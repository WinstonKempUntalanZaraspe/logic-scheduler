from __future__ import annotations

"""Remove self-blocking dependency edges from contiguous real-life outing bundles.

Reality Guard represents support steps such as ``Travel to pool`` and ``Change at pool``
*inside* the primary outing.  Those support tasks are deliberately marked
``autoschedule=False`` so they cannot float elsewhere in the day.

A durable metadata edge such as ``Swimming depends on Change at pool`` is therefore not
an external prerequisite once the tasks are bundled.  Leaving that edge on the primary
creates a deadlock: the solver waits for a support task that the bundle intentionally
removed from the independent scheduling pool.

This patch removes only dependencies that point to support tasks actually attached to the
same bundle.  Genuine prerequisites (for example a medical clearance or another user
specified task) remain untouched.  The cleanup is planning-local; it does not silently
rewrite unrelated TickTick metadata merely because a preview was generated.
"""

from . import reality_patch as _reality


_ORIGINAL_BUILD_FLEXIBLE_BUNDLE = _reality._build_flexible_bundle
_INSTALLED = False


def _strip_internal_support_dependencies(primary, supports, metas: dict[str, dict]) -> list[str]:
    support_ids = {str(task.id) for task in supports if getattr(task, "id", None)}
    if not support_ids:
        return []

    raw = metas.setdefault(str(primary.id), {})
    dependencies = [str(x) for x in (raw.get("dependencies") or []) if x]
    removed = [dep for dep in dependencies if dep in support_ids]
    if not removed:
        return []

    raw["dependencies"] = [dep for dep in dependencies if dep not in support_ids]

    # A zero-gap rule for an internal support dependency is equally stale once the
    # support is represented by bundle geometry rather than by the dependency graph.
    gaps = raw.get("_dependency_gap_minutes")
    if isinstance(gaps, dict):
        kept = {str(k): v for k, v in gaps.items() if str(k) not in support_ids}
        if kept:
            raw["_dependency_gap_minutes"] = kept
        else:
            raw.pop("_dependency_gap_minutes", None)

    return removed


def _bundle_without_internal_prereq_deadlock(primary, supports, metas):
    _strip_internal_support_dependencies(primary, supports, metas)
    return _ORIGINAL_BUILD_FLEXIBLE_BUNDLE(primary, supports, metas)


def install_outing_dependency_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _reality._build_flexible_bundle = _bundle_without_internal_prereq_deadlock
    _INSTALLED = True


__all__ = [
    "install_outing_dependency_patch",
    "_strip_internal_support_dependencies",
]

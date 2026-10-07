# Logic Scheduler

A logic-focused extract of AutoScheduler for reviewing and improving scheduling decisions.

This repository intentionally excludes deployment, OAuth, TickTick/Google account plumbing, databases, UI assets, API keys, personal configuration, and production history.

## What the scheduler is supposed to do

Given:
- a current local time and planning horizon;
- tasks with effort estimates, priorities, deadlines, dependencies, energy needs, timing constraints and optional fixed times;
- hard busy blocks such as classes, appointments and sleep;
- rules such as minimum chunk sizes, meal/recovery windows, daily budgets and transition time;

the planner should return:
- a non-overlapping ordered timeline of scheduled task segments;
- warnings when requested work cannot legally fit;
- diagnostics explaining remaining work, legal windows and unused/constrained capacity.

Core invariants:
1. Never overlap hard commitments or protected sleep.
2. Never schedule completed, abandoned/"Won't Do", expired, or note-only items.
3. Respect dependency order, earliest/latest bounds, hard stops and allowed weekdays.
4. Preserve explicit durations and fixed events.
5. Keep travel/recovery/transition constraints separate from productive work.
6. Prefer useful productive placement over large avoidable idle gaps.
7. Distinguish genuinely free time from time where unfinished work exists but is blocked by constraints.
8. Do not mark a dependency complete merely because only part of its required effort was scheduled.
9. Replanning may move flexible work, but must not silently move fixed commitments.
10. The same task should not be scheduled in overlapping duplicate segments.

## Important files

- `app/scheduler.py` — core CP-SAT / heuristic assignment, scoring, chunking and compaction.
- `app/models.py` — task, metadata, busy-block and scheduled-segment models.
- `app/duration_intelligence.py` — effort estimation.
- `app/planning_gaps.py` — remaining-capacity / gap diagnostics.
- `app/decision_patch.py` — higher-level decision scoring.
- `app/reality_patch.py` — physical/logistics constraints.
- `app/plan_integrity_patch.py` — plan consistency checks.
- `app/plan_quality_patch.py` — schedule quality post-processing.
- `app/final_overlap_guard.py` — final overlap reconciliation.
- `app/final_task_state_guard.py` — final lifecycle/state boundary.
- `app/final_productivity_contract_patch.py` and `app/productive_gap_policy_patch.py` — productive-gap policy.

## Running the small core example

```bash
python -m pip install -r requirements.txt
python examples/run_core_example.py
```

The sample uses fabricated tasks and times only.

## Review goal

Improve general scheduling logic, not individual prompt-specific hacks. Changes should generalize across task names and scenarios while preserving the invariants above.

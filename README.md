# Logic Scheduler

A small, public extract of AutoScheduler containing only the code that decides **what gets scheduled, when, and in what order**.

It intentionally excludes OAuth, TickTick/Google account plumbing, databases, deployment config, UI code, API keys, personal settings, and production history.

## Inputs

The core planner receives:

- current time + planning horizon
- tasks and estimated remaining effort
- priorities, deadlines, dependencies, energy/timing constraints
- hard busy blocks
- sleep/day boundaries, chunk rules and optional daily budgets

## Expected output

`app.scheduler.plan(...)` returns:

1. ordered, non-overlapping scheduled segments
2. warnings for work that cannot legally fit
3. diagnostics about remaining work and available/constrained capacity

The intended invariants are simple: never overlap hard commitments, never schedule completed/abandoned/note items, respect dependencies and timing bounds, preserve fixed commitments, and prefer useful productive placement over avoidable large gaps.

## Main logic

- `app/scheduler.py` — CP-SAT + fallback scheduling, chunking, scoring, dependency ordering and compaction
- `app/models.py` — task and schedule data models
- `app/decision_patch.py` / `app/decision_safety_patch.py` — higher-level decision scoring and safety
- `app/reality_patch.py` / `app/outing_dependency_patch.py` — physical/logistics constraints
- `app/final_overlap_guard.py` — final overlap reconciliation
- `app/final_task_state_guard.py` — final lifecycle/state filter
- `app/planning_gaps.py` — remaining-capacity and gap diagnostics
- `app/final_productivity_contract_patch.py` / `app/productive_gap_policy_patch.py` — constrained-vs-free productive-gap policy
- `app/duration_intelligence.py` — effort estimation

`app/config.py`, `app/db.py`, `app/service.py`, and `app/plan_duration_requests.py` are deliberately tiny logic-lab adapters so the scheduler can be reviewed without pulling in production infrastructure.

## Run the fake example

```bash
python -m pip install -r requirements.txt
python examples/run_core_example.py
pytest -q
```

All sample values are fabricated.

## Review goal

Improve the general scheduling algorithm, not individual task names or prompt-specific cases. Any change should preserve the invariants above and ideally add a regression test.


## Production language / Project Intelligence review surface

The latest production natural-language and learning/project-planning logic is mirrored under `review_surface/`. Start with `review_surface/CLAUDE_REVIEW.md`.

That review surface currently includes the Universal Temporal Language Engine, accidental-task-creation safeguards, semantic fallback/resilience, exam and memorisation intelligence, hackathon/project intelligence, owned-resource + Module Library logic, and the regression tests that define those behaviors. It is a review snapshot from AutoScheduler Pro production commit `a24baf06e5c045900f5b7e4aa3a2147c0000716d`; it is intentionally separate from the minimal runnable scheduler extract above.

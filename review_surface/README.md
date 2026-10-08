# AutoScheduler logic review surface

This directory mirrors selected **production logic** from AutoScheduler Pro for external review by Claude or another reviewer.

Base production snapshot: `a24baf06e5c045900f5b7e4aa3a2147c0000716d`

Latest incremental logic mirror: unfinished-work carry-forward synced from production `fe34a97f35979c936ab6677af952e55525ba38c5`.

Production status at mirror time:
- Universal Temporal Language Engine deployed.
- Independent Render full-suite gate: **1,248 tests passed, 0 failed**.
- Final temporal integration is reconciled on production `main`; compatibility fixes include shorthand clock ranges, spoken durations, venue/arrival role ownership, and legacy day-plan behavior.

It is intentionally separate from the runnable minimal scheduler extract under `app/`. Files here may import production-only modules; the goal is code review, reasoning, patch design, and regression analysis without OAuth/UI/deployment noise.

## Review areas

- **Universal temporal language:** dates, clocks, exact intervals, flexible windows, deadlines, recurrence, relative timing, AM/PM, arbitrary future dates, wake/sleep overrides, provenance and source-grounded semantic fallback.
- **Accidental task creation / natural language safety:** distinguish state, commands, questions, notes, hypotheticals, cancellations and planning requests from genuine task creation.
- **Exam intelligence:** authoritative syllabus/topic discovery, prerequisite-aware progression, prior-knowledge skipping, memorisation/visual recall and assessment planning.
- **Hackathon/project intelligence:** URL/source research, deliverables, rubrics, deadlines, beginner-to-advanced learning progression, resource use and milestone generation.
- **Project sources/resources:** provenance, source validation, PDFs/owned resources, Module Library retrieval and learning-navigation logic.

See `CLAUDE_REVIEW.md` for the review contract and priority test files.

Please improve **general rules**, not prompt-specific keyword hacks. Preserve permission boundaries: reviewed planning must not silently write to TickTick.


## Latest incremental mirror — unfinished-work carry-forward

The public review surface now includes the production carry-forward engine and regressions for requests such as:

- `I didn't finish all my tasks tdy, reschedule them tmr`
- `I can't finish some of my tasks today, move the unfinished ones next week`
- `reschedule the tasks I didn't finish today to tomorrow`
- `move the unfinished work to next month`

Files to review:
- `app/unfinished_carry_forward.py`
- `app/temporal_engine_core.py`
- `app/final_task_state_guard.py`
- `app/language_intake.py`
- `app/plan_revision_patch.py`
- `tests/test_unfinished_carry_forward.py`

Key contract: **all** forces all proven unfinished flexible work forward; **some** is overflow-aware, guarantees at least one low-risk task moves, keeps remaining eligible work today first, and may spill more only into the requested future destination. Week/month requests remain true date ranges rather than collapsing onto the first day. Fixed, NOTE, completed, Won't Do, recurring, all-day and autoschedule-off work stays excluded.


## Latest gap-classification mirror

Production source: `9d9fe25320f5f3f605ad5a184adc02864e1a4066`.

`app/planning_gaps.py`, `app/final_productivity_contract_patch.py`, and `tests/test_day_aware_gap_truth.py` are byte-identical review copies of the production gap-truth fix. They prevent Saturday-only work from marking Friday's idle periods constrained, remove ghost gaps left by displaced default lunch/dinner reservations, and preserve real protected buffers.

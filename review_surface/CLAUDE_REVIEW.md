# Claude review brief

This folder is a **logic-only mirror** of AutoScheduler Pro, sourced from production commit `a24baf06e5c045900f5b7e4aa3a2147c0000716d`.

The finalized production tree passed an independent Render full-suite gate of **1,248 tests, 0 failures**. The focused temporal suite also passed before the full compatibility gate. The review goal is not to rewrite everything; it is to find general logic flaws, missing edge cases, and simplifications that preserve the current contracts.

## Review priorities

1. **Universal temporal language**
   - dates: relative, absolute, weekday occurrences, ranges
   - clocks: AM/PM, 24-hour, approximate times, intervals/windows
   - deadlines vs exact starts vs earliest/latest bounds
   - recurrence and relative relationships
   - provenance/confidence and ambiguity handling
   - prevent date/duration numbers from becoming phantom clocks

2. **Accidental task creation**
   - state/context must not become tasks
   - questions, hypotheticals, negations, availability, notes, planning commands and cancellation language must stay non-create unless there is explicit activity authority
   - preserve review-first / no silent TickTick writes

3. **Exam + memorisation intelligence**
   - authoritative syllabus/topic discovery
   - prerequisite-aware progression and prior-knowledge skipping
   - conceptual/procedural/memorisation/visual-recall classification
   - spaced retrieval based on actual completion
   - measurable self-checks and exam coverage audits

4. **Hackathon / project intelligence**
   - URL/source resolution and provenance
   - fail-soft behavior when a source cannot be fetched
   - deliverables, rubric extraction, deadlines, milestones and beginner-to-advanced learning plans
   - avoid vague generic tasks; produce concrete learning/build/test/demo work

5. **Large resource/module handling**
   - owned resources and multi-PDF Module Library
   - exact page/section navigation, mastery, bounded retrieval, task-time passage retrieval
   - do not silently truncate or hallucinate unreadable content

## Important contracts to preserve

- General rules over prompt-specific keyword hacks.
- Explicit exact times are hard only when the user states them as such.
- Approximate/optional times remain soft.
- Unknown ends remain unknown; do not invent shift durations.
- Fixed commitments and sleep must not overlap.
- Existing `Won't Do` / expired task logic must stay excluded from scheduling.
- Never write to TickTick merely because interpretation succeeded; final apply permission is separate.
- Semantic/LLM fallback may help interpret unfamiliar wording but must not invent unsupported absolute calendar times.
- Do not weaken the distinction between genuinely unallocated time and work blocked by constraints.

## Useful tests

Start with:
- `tests/test_temporal_language_benchmark.py`
- `tests/test_temporal_engine.py` (includes shorthand `6-7pm`, spoken-duration/title and date-vs-clock regressions)
- `tests/test_temporal_intake_patch.py`
- `tests/test_quickdump.py`
- `tests/test_production_reliability_patch.py`
- `tests/test_exam_intelligence.py`
- `tests/test_memorisation_intelligence.py`
- `tests/test_project_intelligence_stress.py`
- `tests/test_project_intelligence_url_provenance.py`
- `tests/test_module_library.py`

Please propose fixes as general invariants and add/update regression cases for every behavior change.


## Incremental review target: unfinished-work carry-forward

Production source for this feature: `fe34a97f35979c936ab6677af952e55525ba38c5`.

Please stress-test the general invariant rather than individual phrases:
- recognize unfinished-work rescheduling as a scheduler command, never task-creation authority;
- distinguish `all` from `some`;
- exact day targets are one-day windows;
- next week/month are real ranges;
- partial carry-forward may keep work today first but must not leak into dates between today and the requested future window;
- remaining effort, dependency metadata, deadlines and lifecycle exclusions must survive;
- source-side dates such as `today` must not beat an explicit destination such as `to tomorrow`;
- carry-forward eligibility must persist after transient Quick Dump context expires.

Primary regression file: `tests/test_unfinished_carry_forward.py`.


## Incremental review: day-aware gap truth and shifted meals

Production source: `9d9fe25320f5f3f605ad5a184adc02864e1a4066`.

Review `app/planning_gaps.py`, `app/final_productivity_contract_patch.py` and `tests/test_day_aware_gap_truth.py`. Key invariants:
- Unfinished work only eligible on a future day never converts today's genuinely free intervals into CONSTRAINED.
- Genuine same-day work that cannot legally fit reports CONSTRAINED with a concrete reason.
- Legal work that fits an idle gap remains CASE A (planner should fill it).
- Flexible meals are represented by their actual scheduled blocks, not by their displaced configured clock reservations. `13:45–19:00` must remain one contiguous unallocated interval when dinner occurs at `19:00` and no real buffer intervenes.
- Explicit real transition/travel/recovery buffers remain protected and visible; never merge across one.
- Meal rules on other days must not disappear when today's flexible meal shifts.

A Docker build-time regression gate also runs in production, but the public runnable-core test file is the review target.

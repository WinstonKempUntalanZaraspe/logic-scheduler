# Claude review brief

This folder is a **logic-only mirror** of AutoScheduler Pro, sourced from production commit `c205dd86ac449fc26bd5cf4141258e90ec0e5fbc`.

The production branch passed an independent Render gate of **90/90 temporal tests** before going live. The review goal is not to rewrite everything; it is to find general logic flaws, missing edge cases, and simplifications that preserve the current contracts.

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
- `tests/test_temporal_engine.py`
- `tests/test_temporal_intake_patch.py`
- `tests/test_quickdump.py`
- `tests/test_production_reliability_patch.py`
- `tests/test_exam_intelligence.py`
- `tests/test_memorisation_intelligence.py`
- `tests/test_project_intelligence_stress.py`
- `tests/test_project_intelligence_url_provenance.py`
- `tests/test_module_library.py`

Please propose fixes as general invariants and add/update regression cases for every behavior change.

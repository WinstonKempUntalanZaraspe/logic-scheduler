# AutoScheduler logic review surface

This directory mirrors selected **production logic** from AutoScheduler Pro for external review by Claude or another reviewer.

Source production commit: `c205dd86ac449fc26bd5cf4141258e90ec0e5fbc`

Production status at mirror time:
- Universal Temporal Language Engine deployed.
- Independent Render temporal gate: **90/90 tests passed**.
- Production backend and frontend: LIVE on the same source tree.

It is intentionally separate from the runnable minimal scheduler extract under `app/`. Files here may import production-only modules; the goal is code review, reasoning, patch design, and regression analysis without OAuth/UI/deployment noise.

## Review areas

- **Universal temporal language:** dates, clocks, exact intervals, flexible windows, deadlines, recurrence, relative timing, AM/PM, arbitrary future dates, wake/sleep overrides, provenance and source-grounded semantic fallback.
- **Accidental task creation / natural language safety:** distinguish state, commands, questions, notes, hypotheticals, cancellations and planning requests from genuine task creation.
- **Exam intelligence:** authoritative syllabus/topic discovery, prerequisite-aware progression, prior-knowledge skipping, memorisation/visual recall and assessment planning.
- **Hackathon/project intelligence:** URL/source research, deliverables, rubrics, deadlines, beginner-to-advanced learning progression, resource use and milestone generation.
- **Project sources/resources:** provenance, source validation, PDFs/owned resources, Module Library retrieval and learning-navigation logic.

See `CLAUDE_REVIEW.md` for the review contract and priority test files.

Please improve **general rules**, not prompt-specific keyword hacks. Preserve permission boundaries: reviewed planning must not silently write to TickTick.

from __future__ import annotations

"""Cost/latency helpers for interactive semantic intake.

The semantic engine itself remains available for explicit semantic tests and non-Quick-Dump
callers. Quick Dump decides whether a request is simple enough to force deterministic local
review; this module supplies that conservative classifier plus a bounded secondary semantic
interpreter used by the legacy semantic-plan path.

Absolute invariant: when ``semantic_plan.LOCAL_REVIEW`` is active, this layer must never
enter the provider/network path. LOCAL means local all the way down.
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable

from . import semantic_plan

_INSTALLED = False

_AMBIGUITY = re.compile(
    r"\b(?:if|unless|whether|depending|whichever|whatever|either|otherwise|instead\s+of|"
    r"maybe|might|perhaps|possibly|somehow|one\s+of|something|anything)\b",
    re.I,
)
_REFERENTIAL = re.compile(
    r"\b(?:it|them|those|these|that|this\s+one|the\s+other\s+one|same\s+thing)\b",
    re.I,
)
_SIMPLE_STATE = re.compile(
    r"\b(?:woke\s+up|slept\s+in|i(?:'m|\s+am)\s+home\s+now|got\s+home|arrived\s+home|"
    r"back\s+home|i(?:'m|\s+am)\s+(?:tired|exhausted|sleepy)|got\s+interrupted|"
    r"was\s+interrupted|finished\s+(?:breakfast|lunch|dinner)|"
    r"(?:i(?:'m|\s+am)\s+)?(?:eating|having)\s+(?:breakfast|lunch|dinner)\s+(?:right\s+)?now|"
    r"(?:eat|have)\s+(?:my\s+)?(?:breakfast|lunch|dinner)\s+(?:right\s+)?now|"
    r"replan|reschedule|rebuild|reorganize|reorganise)\b",
    re.I,
)
_SIMPLE_ACTION = re.compile(
    r"\b(?:do|study|revise|review|read|work\s+on|work\s+through|go\s+through|go\s+over|"
    r"look\s+over|skim|finish|start|continue|resume|solve|derive|prove|calculate|plot|graph|"
    r"practice|practise|drill|memorize|memorise|annotate|summarize|summarise|swim|swimming|"
    r"gym|workout|run|running|walk|jog|cycle|bike|eat|have\s+(?:breakfast|lunch|dinner)|"
    r"cook|go\s+to|head\s+to|head\s+over|drop\s+by|stop\s+by|swing\s+by|visit|meet|"
    r"attend|grab|fetch|collect|pick\s+up|drop\s+off|buy|shop|return|head\s+back|church|"
    r"sleep|shower|bath|nap|rest|train|training|play|write|draft|edit|proofread|code|coding|"
    r"debug|test|compile|deploy|benchmark|refactor|configure|install|update|upload|submit|"
    r"email|message|reply|send|clean|tidy|pack|unpack|charge|refill|pray|journal)\b",
    re.I,
)
_SIMPLE_OBJECT = re.compile(
    r"\b(?:math|maths|calculus|algebra|integration|limits?|physics|mechanics|thermo|"
    r"coding|code|programming|bible|scripture|report|assignment|homework|project|email|"
    r"swim|swimming|gym|workout|run|running|church|breakfast|lunch|dinner|shower|bath|"
    r"nap|table\s+tennis|training|practice)\b",
    re.I,
)


def _clauses(text: str) -> list[str]:
    connector = (
        r"\b(?:and\s+then|then|after\s+that|afterwards?|following\s+that|followed\s+by|"
        r"subsequently|next\s+up|from\s+there|after\s+which|later\s+on|"
        r"once\s+(?:that(?:'s|\s+is)\s+done|this(?:'s|\s+is)\s+done|i(?:'m|\s+am)\s+done))\b"
    )
    return [p.strip(" ,") for p in re.split(r"[.;\n]+|" + connector, text, flags=re.I) if p.strip(" ,")]


def is_simple_local_prompt(text: str) -> bool:
    """Return True only when forcing Quick Dump local is a conservative choice."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 320 or "?" in raw:
        return False
    clauses = _clauses(raw)
    if not clauses or len(clauses) > 5:
        return False

    # "after that" / "before that" are ordinary sequence connectors. Any remaining
    # bare pronoun such as "do that" is genuinely referential and should keep semantic
    # interpretation available.
    reference_scan = re.sub(r"\b(?:after|before)\s+that\b", "", raw, flags=re.I)
    if _AMBIGUITY.search(raw) or _REFERENTIAL.search(reference_scan):
        return False
    if _SIMPLE_STATE.search(raw):
        return True

    if not _SIMPLE_ACTION.search(clauses[0]):
        return False
    for index, part in enumerate(clauses):
        if _SIMPLE_ACTION.search(part):
            continue
        words = re.findall(r"[a-z0-9]+", part.lower())
        if index > 0 and len(words) <= 5 and _SIMPLE_OBJECT.search(part):
            continue
        return False
    return True


def semantic_timeout_seconds(config: dict | None = None) -> float:
    """Suggested whole-attempt budget for optional interactive semantic work."""
    raw = os.getenv("AUTOSCHEDULER_SEMANTIC_TIMEOUT_SECONDS", "6")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 6.0
    return min(8.0, max(2.5, value))


def _timeout_warning() -> str:
    return "Semantic interpretation exceeded the interactive time budget; using local interpretation instead."


def make_cost_aware_interpreter(base: Callable[..., Awaitable]):
    """Bound the secondary/legacy semantic-plan interpreter.

    LOCAL_REVIEW is an absolute no-network contract. This guard is intentionally checked
    before every other classifier so a semantic wrapper can never sneak a provider call
    into the deterministic fallback path.
    """
    async def cost_aware_interpret(text, rows, config, now):
        if semantic_plan.LOCAL_REVIEW.get():
            semantic_plan.LAST_ATTEMPT.set(None)
            return None
        if is_simple_local_prompt(text):
            semantic_plan.LAST_ATTEMPT.set(None)
            return None
        try:
            return await asyncio.wait_for(
                base(text, rows, config, now),
                timeout=semantic_timeout_seconds(config),
            )
        except TimeoutError:
            semantic_plan.LAST_ATTEMPT.set({
                "code": "semantic-time-budget-exceeded",
                "summary": _timeout_warning(),
            })
            return None

    cost_aware_interpret._semantic_cost_gate = True
    return cost_aware_interpret


def install_semantic_cost_gate():
    """Install only the secondary semantic-plan gate.

    Do not globally replace contextual semantic extraction: direct contextual callers and
    semantic regression tests must retain their normal semantics. The production Quick Dump
    endpoint applies local-first routing explicitly in ``quickdump_latency_patch``.
    """
    global _INSTALLED
    current = semantic_plan.interpret_plan
    if _INSTALLED or getattr(current, "_semantic_cost_gate", False):
        return current
    _INSTALLED = True
    wrapped = make_cost_aware_interpreter(current)
    semantic_plan.interpret_plan = wrapped
    return wrapped


__all__ = [
    "install_semantic_cost_gate", "is_simple_local_prompt", "make_cost_aware_interpreter",
    "semantic_timeout_seconds",
]

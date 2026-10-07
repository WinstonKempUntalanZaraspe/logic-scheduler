"""Deterministic plan review. Estimates are not calendar feasibility guarantees."""
from __future__ import annotations
import math
import re
from datetime import datetime, timedelta
from .config import settings


def project_capacity(text: str) -> tuple[int, str]:
    # Only a personal availability statement can override the labelled default.
    match = re.search(r'\b(?:i|we)\s+(?:can\s+(?:spend|study|work)(?:\s+for)?|have|only\s+have)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\s*(?:available\s*)?(?:per|a|each|/)\s*(day|week)\b', text, re.I)
    if not match:
        return 120, 'assumed_default'
    amount, unit, period = match.groups()
    minutes = float(amount) * (60 if unit.lower().startswith('h') else 1)
    if period.lower() == 'week': minutes /= 7
    return max(0, min(1440, math.floor(minutes))), 'user_input'


def _minutes(amount, unit) -> int:
    return max(0, min(1440, math.floor(float(amount) * (60 if str(unit).lower().startswith('h') else 1))))


def project_priority_class(text: str) -> str:
    value = str(text or "")
    if re.search(r"\b(?:lower|low|secondary|side)\s+priority\b|\b(?:side|secondary)\s+(?:goal|project|competition)\b", value, re.I):
        return "low"
    if re.search(r"\b(?:top|highest|high|main|primary)\s+priority\b", value, re.I):
        return "high"
    return "normal"


def _next_named_day(text: str, now: datetime):
    """Resolve a user-stated transition date without inventing an event deadline."""
    from calendar import monthrange
    from datetime import date
    value = str(text or "")
    iso = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b", value)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
        "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    month_words = "|".join(sorted(months, key=len, reverse=True))
    explicit = re.search(
        rf"\b(?:from|starting|after|on|school\s+(?:reopens?|starts?|resumes?)\s+(?:on\s+)?)"
        rf"(?:the\s+)?(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_words})|"
        rf"({month_words})\s+(\d{{1,2}})(?:st|nd|rd|th)?)\b",
        value, re.I,
    )
    if explicit:
        if explicit.group(1):
            day, month = int(explicit.group(1)), months[explicit.group(2).lower()]
        else:
            month, day = months[explicit.group(3).lower()], int(explicit.group(4))
        year = now.year
        try:
            candidate = date(year, month, day)
            if candidate < now.date():
                candidate = date(year + 1, month, day)
            return candidate
        except ValueError:
            return None
    # "school reopens on the 19th" / "from 19th onwards" means the next occurrence
    # of that day-of-month relative to the planning clock.
    day_only = re.search(
        r"\b(?:school\s+(?:reopens?|starts?|resumes?)\s+(?:on\s+)?|from\s+)"
        r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:\s+onwards?|\s+onward)?\b",
        value, re.I,
    )
    if not day_only:
        return None
    day = int(day_only.group(1))
    year, month = now.year, now.month
    for _ in range(14):
        if day <= monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if candidate >= now.date():
                return candidate
        month += 1
        if month == 13:
            month = 1
            year += 1
    return None


_DAILY_RANGE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)?\s*"
    r"(?:to|through|[-–—]|up\s+to)\s*"
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)?\s*"
    r"(?:per|a|each|/)?\s*day\b",
    re.I,
)


def _daily_capacity_ranges(text: str):
    value = str(text or "")
    found = []
    for match in _DAILY_RANGE_RE.finditer(value):
        # Natural ranges often state the unit only once: "3 to 5 hours per day"
        # or "30-60 min/day".  Inherit the stated unit across the range, but reject
        # a unitless "3 to 5 per day" rather than silently treating it as minutes.
        first_unit = match.group(2) or match.group(4)
        second_unit = match.group(4) or match.group(2)
        if not first_unit or not second_unit:
            continue
        lo = _minutes(match.group(1), first_unit)
        hi = _minutes(match.group(3), second_unit)
        found.append({
            "low": min(lo, hi), "high": max(lo, hi),
            "start": match.start(), "end": match.end(),
            "text": match.group(0),
        })
    return found


def _daily_capacity_range(text: str):
    ranges = _daily_capacity_ranges(text)
    return (ranges[0]["low"], ranges[0]["high"]) if ranges else None


def _phased_capacity_ranges(text: str, transition) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Classify explicit before/after-transition daily ranges without cross-clause leakage.

    Capacity cues are normally written immediately before the range they qualify, e.g.
    "right now 3-5 hours/day" or "from the 19th onward 30-60 min/day".  Earlier code
    used a wide surrounding window, so the cue from the next sentence could contaminate
    the previous range.  This version keeps punctuation-bounded local context and, when
    two cues share a clause, assigns the nearest cue while strongly preferring a cue
    written before the range.

    If only one range is present, it remains the post-transition range for backwards
    compatibility with the existing "from 19th onwards..." behavior.
    """
    ranges = _daily_capacity_ranges(text)
    if not ranges:
        return None, None
    if len(ranges) == 1:
        row = ranges[0]
        return None, (row["low"], row["high"])

    value = str(text or "")
    day = int(getattr(transition, "day", 0) or 0)

    pre_pattern = re.compile(
        r"\b(?:right\s+now|currently|for\s+now|until\s+then|"
        r"before\s+(?:school|then|that|the\s+transition)|"
        r"before\s+\d{1,2}(?:st|nd|rd|th)?|until\s+\d{1,2}(?:st|nd|rd|th)?)\b",
        re.I,
    )
    post_parts = [
        r"\b(?:from|after|starting)\b.{0,55}\b(?:onwards?|onward|school\s+(?:reopens?|starts?|resumes?))\b"
    ]
    if day:
        post_parts.append(
            rf"\b(?:from|after|starting)\s+(?:the\s+)?{day}(?:st|nd|rd|th)?\b"
        )
    post_pattern = re.compile("|".join(post_parts), re.I)

    def clause_bounds(row):
        # Full stops/semicolons/newlines are strong phase boundaries.  Keep commas
        # inside the clause because users commonly write "from the 19th onward, ...".
        left_candidates = [
            value.rfind(".", 0, row["start"]),
            value.rfind(";", 0, row["start"]),
            value.rfind("\n", 0, row["start"]),
            value.rfind("!", 0, row["start"]),
            value.rfind("?", 0, row["start"]),
        ]
        left = max(left_candidates) + 1
        right_candidates = [
            pos for pos in (
                value.find(".", row["end"]),
                value.find(";", row["end"]),
                value.find("\n", row["end"]),
                value.find("!", row["end"]),
                value.find("?", row["end"]),
            )
            if pos >= 0
        ]
        right = min(right_candidates) if right_candidates else len(value)
        return left, right

    def cue_distance(pattern, row):
        left, right = clause_bounds(row)
        best = None
        for match in pattern.finditer(value, left, right):
            if match.end() <= row["start"]:
                distance = row["start"] - match.end()
            elif match.start() >= row["end"]:
                # Phase language usually precedes the capacity it qualifies.  A cue
                # after the range is still accepted ("3-5 h/day right now"), but loses
                # to a reasonably close preceding cue from the same clause.
                distance = 80 + match.start() - row["end"]
            else:
                distance = 0
            best = distance if best is None else min(best, distance)
        return best

    pre = post = None
    leftovers = []
    for row in ranges:
        pre_distance = cue_distance(pre_pattern, row)
        post_distance = cue_distance(post_pattern, row)
        pair = (row["low"], row["high"])

        if pre_distance is not None and (post_distance is None or pre_distance < post_distance):
            if pre is None:
                pre = pair
            else:
                leftovers.append(pair)
        elif post_distance is not None and (pre_distance is None or post_distance < pre_distance):
            if post is None:
                post = pair
            else:
                leftovers.append(pair)
        else:
            leftovers.append(pair)

    # With exactly two ranges, one confidently classified side determines the other.
    if len(ranges) == 2:
        if post is not None and pre is None and leftovers:
            pre = leftovers[0]
        elif pre is not None and post is None and leftovers:
            post = leftovers[0]
    return pre, post

def project_capacity_profile(text: str, now=None) -> dict:
    """Parse stable and phased project availability.

    Phased availability is preserved exactly. For example, "right now 3-5 hours
    per day; from the 19th onward 30-60 minutes per day" becomes a 180-300
    minute/day phase through the 18th and a 30-60 minute/day phase from the 19th.
    The older "go all out until then" wording remains supported as an uncapped
    pre-transition phase. Ordinary life constraints and task priorities always win.
    """
    now = now or datetime.now(settings.tz)
    base, source = project_capacity(text)
    transition = _next_named_day(text, now)
    daily_range = _daily_capacity_range(text)
    phases = []
    planning_capacity = base
    capacity_source = source
    if transition and daily_range:
        explicit_pre, explicit_post = _phased_capacity_ranges(text, transition)
        low, high = explicit_post or daily_range
        all_out = bool(re.search(r"\b(?:right\s+now|for\s+now|until\s+then|before\s+(?:school|that|then)).{0,50}\b(?:go\s+all\s+out|all\s+out|as\s+much\s+as\s+(?:i|we)\s+can)\b|\b(?:go\s+all\s+out|all\s+out).{0,50}\b(?:right\s+now|for\s+now|until\s+then)\b", str(text or ""), re.I))
        if transition > now.date():
            pre_low, pre_high = explicit_pre or (0, None if all_out else base)
            phases.append({
                "start": now.date().isoformat(),
                "end": (transition - timedelta(days=1)).isoformat(),
                "min_minutes_per_day": pre_low,
                "max_minutes_per_day": pre_high,
                "source": "user_input" if explicit_pre else ("user_intensive_window" if all_out else source),
            })
        phases.append({
            "start": transition.isoformat(),
            "end": None,
            "min_minutes_per_day": low,
            "max_minutes_per_day": high,
            "source": "user_input",
        })
        # Review/roadmap estimates should describe today's phase when the transition
        # is still in the future. Daily scheduling still uses the exact per-date phase.
        if transition > now.date() and explicit_pre:
            planning_capacity = explicit_pre[1]
        else:
            planning_capacity = high
        capacity_source = "phased_user_input"
    return {
        "planning_capacity_minutes": planning_capacity,
        "capacity_source": capacity_source,
        "capacity_phases": phases,
        "priority_class": project_priority_class(text),
    }


def campaign_capacity_for_date(campaign: dict, day):
    """Return that day's project cap, or None for an explicitly uncapped phase."""
    from datetime import date
    if isinstance(day, datetime):
        day = day.date()
    elif isinstance(day, str):
        day = date.fromisoformat(day[:10])
    for phase in campaign.get("capacity_phases") or []:
        start = date.fromisoformat(str(phase.get("start"))[:10])
        end = date.fromisoformat(str(phase.get("end"))[:10]) if phase.get("end") else None
        if day >= start and (end is None or day <= end):
            value = phase.get("max_minutes_per_day")
            return None if value is None else max(0, int(value))
    return max(0, int(campaign.get("estimated_daily_project_capacity_minutes", 120)))


def preparation_days_for_effort(campaign: dict, start, effort: int) -> int:
    """Integrate phased daily limits instead of extending today's budget forever."""
    fallback = max(0, int(campaign.get('estimated_daily_project_capacity_minutes', 120)))
    remaining, days = max(0, int(effort)), 0
    while remaining > 0 and days < 3660:
        cap = campaign_capacity_for_date(campaign, start + timedelta(days=days))
        remaining -= fallback if cap is None else cap
        days += 1
    return days


def assign_roadmap_targets(packages, profile, start, end, effort_field='estimated_minutes'):
    """Place ordered effort against daily capacity; targets remain estimates, not bookings."""
    from bisect import bisect_left, bisect_right
    from itertools import accumulate
    span = max(1, (end - start).days + 1)
    fallback = max(0, int(profile.get('estimated_daily_project_capacity_minutes',
                                      profile.get('planning_capacity_minutes', 120))))
    weights = []
    for offset in range(span):
        cap = campaign_capacity_for_date(profile, start + timedelta(days=offset))
        weights.append(fallback if cap is None else max(0, cap))
    # A zero-capacity roadmap cannot be feasible; the review reports this separately.
    cumulative_capacity = list(accumulate(weights if any(weights) else [1] * span))
    capacity = cumulative_capacity[-1]
    effort = sum(max(0, int(p.get(effort_field) or 0)) for p in packages)
    used = 0
    for p in packages:
        a = min(span - 1, bisect_right(cumulative_capacity, capacity * used / max(1, effort)))
        used += max(0, int(p.get(effort_field) or 0))
        b = min(span - 1, max(a, bisect_left(cumulative_capacity, capacity * used / max(1, effort))))
        p['target_start'] = (start + timedelta(days=a)).isoformat()
        p['target_finish'] = (start + timedelta(days=b)).isoformat()


def assess_campaign(campaign: dict, now=None) -> dict:
    now = now or datetime.now(settings.tz)
    packages = campaign.get('work_packages') or []
    remaining = sum(max(0, int(p.get('remaining_minutes') or 0)) for p in packages if p.get('status') not in {'done','cancelled'})
    active_cap = campaign_capacity_for_date(campaign, now.date())
    capacity = max(0, int(campaign.get('estimated_daily_project_capacity_minutes',120) if active_cap is None else active_cap))
    end = campaign.get('internal_deadline') or campaign.get('deadline') or campaign.get('presentation_date')
    days = max(0,(datetime.fromisoformat(end).date()-now.date()).days+1) if end else None
    required = math.ceil(remaining/max(1,days)) if days is not None else None
    issues=[]
    def issue(code,message): issues.append({'code':code,'message':message})
    if not end: issue('missing_deadline','No confirmed finish date: workload can be estimated, but deadline feasibility is unknown.')
    if campaign.get('capacity_source','assumed_default') == 'assumed_default':
        issue('assumed_capacity','Preparation budget defaults to 120 minutes per day. Adjust it before applying; fixed commitments, meals and sleep still take precedence.')
    budget = sum(
        capacity if (cap := campaign_capacity_for_date(campaign, now.date() + timedelta(days=offset))) is None else cap
        for offset in range(days)
    ) if days is not None else None
    overloaded = budget is not None and remaining > budget
    if overloaded:
        issue('capacity_overload',f'Remaining scope exceeds the preparation budget by {remaining-budget} minutes. Reduce scope, add available time, or change the deadline; fixed commitments remain protected.')
    for p in packages:
        if p.get('requires_contiguous_session') and p.get('status') not in {'done', 'cancelled'}:
            target = str(p.get('target_start') or now.date().isoformat())[:10]
            cap = campaign_capacity_for_date(campaign, target)
            duration = int(p.get('remaining_minutes') or p.get('estimated_minutes') or 0)
            if cap is not None and duration > cap:
                issue('continuous_session_budget',
                      f"{p.get('title', 'Timed assessment')} needs one uninterrupted {duration}-minute session; "
                      f"the allowance on {target} is {cap} minutes. Agree a longer practice window or use a "
                      "shorter timed section; the full assessment will not be split or the budget overridden.")
    if any(p.get('status')=='paused' for p in packages): issue('paused_prerequisites','Some work is paused. Dependents remain blocked until their prerequisites are completed.')
    text=lambda p: ' '.join(str(p.get(k) or '') for k in ('phase','title','description')).casefold()
    learning=[p for p in packages if re.search(r'\b(?:learn\w*|foundation\w*|prerequisite\w*|study|concept\w*|fundamental\w*)\b',text(p))]
    practice=[p for p in packages if re.search(r'\b(?:test\w*|validat\w*|evaluat\w*|practice|exercise\w*|mock\w*)\b',text(p))]
    review=[p for p in packages if re.search(r'\b(?:demo\w*|rehears\w*|present\w*|submit\w*|submission|deliver\w*)\b',text(p))]
    if 'ZERO' in str(campaign.get('knowledge_assumption','')).upper() and not learning:
        issue('missing_foundations','Zero-knowledge preparation has no explicit foundations. Review the prerequisite learning path before applying.')
    if not practice: issue('missing_validation','No explicit practice, testing or evaluation package was found. Add a measurable check of readiness.')
    if campaign.get('campaign_type') in {'hackathon','competition','research_project','portfolio_project'} and not review:
        issue('missing_delivery','No explicit demo, submission or presentation preparation was found.')
    rubric=campaign.get('rubric') or []
    linked={str(link).casefold().strip() for p in packages for link in p.get('rubric_links',[])}
    uncovered=[r['criterion'] for r in rubric if str(r.get('criterion','')).casefold().strip() not in linked]
    if uncovered: issue('uncovered_rubric','No work is linked to these criteria: '+', '.join(uncovered))
    if any(int(p.get('estimated_minutes') or 0)>480 for p in packages):
        issue('coarse_work','Some packages exceed eight hours. Use concrete intermediate checkpoints to track learning and delivery.')
    return {'remaining_minutes':remaining,'available_minutes_per_day':capacity,'required_minutes_per_day':required,
            'preparation_days_remaining':days,'capacity_source':campaign.get('capacity_source','assumed_default'),
            'capacity_status':'overloaded' if overloaded else ('unknown' if days is None else 'within_estimated_budget'),
            'calendar_feasibility':'not_evaluated','issues':issues,
            'scope_options':[{'key':p['key'],'title':p['title'],'minutes':p.get('remaining_minutes',0)} for p in packages
                             if p.get('priority')=='low' and p.get('status') not in {'done','cancelled'}
                             and not any(p['key'] in q.get('dependencies',[]) for q in packages if q.get('status') not in {'done','cancelled'})]}


def refresh_review(campaign: dict) -> None:
    refresh_practice_horizon(campaign)
    review=assess_campaign(campaign)
    campaign['planning_review']=review
    campaign['remaining_minutes']=review['remaining_minutes']
    campaign['required_average_minutes_per_day']=review['required_minutes_per_day']
    daily=review['available_minutes_per_day']
    required=review['required_minutes_per_day']
    campaign['planning_capacity_ratio']=round(required/daily,3) if daily and required is not None else None



def refresh_practice_horizon(campaign: dict, now=None) -> None:
    """Extend an estimated practice window; never move a real event date."""
    if any(campaign.get(k) for k in ('internal_deadline', 'deadline', 'presentation_date')):
        return
    now = now or datetime.now(settings.tz)
    capacity = max(0, int(campaign.get('estimated_daily_project_capacity_minutes', 0)))
    if not capacity:
        return
    start = datetime.fromisoformat(campaign.get('preparation_start') or now.date().isoformat()).date()
    pending = [p for p in campaign.get('work_packages', []) if p.get('status') not in {'done', 'cancelled'}]
    effort = math.ceil(sum(max(0, int(p.get('remaining_minutes') or 0)) for p in pending) * 1.25)
    campaign['readiness_effort_minutes_with_buffer'] = effort
    old_end = datetime.fromisoformat(campaign.get('roadmap_end') or start.isoformat()).date()
    effort_days = preparation_days_for_effort(campaign, now.date(), effort)
    end = max(old_end, start + timedelta(days=27), now.date() + timedelta(days=max(0, effort_days-1)))
    campaign['recommended_preparation_days'] = (end-start).days+1
    campaign['roadmap_end'] = end.isoformat()
    if end > old_end:
        # Preserve completed history. Spread remaining work in dependency order.
        from .project_intelligence_models import topological
        by = {p['key']: p for p in campaign.get('work_packages', [])}
        ordered = [by[key] for key in topological(campaign.get('work_packages', []))
                   if by[key].get('status') not in {'done', 'cancelled'}]
        assign_roadmap_targets(ordered, campaign, now.date(), end, 'remaining_minutes')

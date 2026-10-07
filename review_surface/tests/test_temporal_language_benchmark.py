from datetime import datetime
from zoneinfo import ZoneInfo

import app.final_entrypoint  # install the production language stack
from app import language_intake
from app.temporal_engine import parse_temporal

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 7, 14, 20, tzinfo=TZ)
CFG = {"wake_time": "07:00", "sleep_start": "23:00", "day_start": "07:00", "day_end": "23:00"}

DATE_FORMS = [
    "19 Oct",
    "19th Oct",
    "October 19",
    "Oct 19",
    "19 October",
    "2026-10-19",
    "19/10",
    "Monday after next",
    "two Mondays from now",
    "in 12 days",
]
CLOCK_FORMS = [
    "7pm",
    "7 PM",
    "7:00pm",
    "19:00",
    "1900",
    "seven pm",
    "about 7pm",
    "7pm-ish",
]
RANGE_FORMS = [
    "from 7pm to 9pm",
    "7pm-9pm",
    "seven pm till nine pm",
]


def test_temporal_cross_product_normalizes_over_one_hundred_surface_forms():
    checked = 0
    for date_form in DATE_FORMS:
        for clock_form in CLOCK_FORMS:
            text = f"Study on {date_form} at {clock_form}"
            doc = parse_temporal(text, NOW)
            points = [x for x in doc.constraints if x.kind == "point"]
            assert points, text
            point = points[0]
            assert point.start_at.isoformat() == "2026-10-19T19:00:00+08:00", text
            checked += 1

        for range_form in RANGE_FORMS:
            text = f"Study on {date_form} {range_form}"
            doc = parse_temporal(text, NOW)
            intervals = [x for x in doc.constraints if x.kind == "interval"]
            assert intervals, text
            interval = intervals[0]
            assert interval.start_at.isoformat() == "2026-10-19T19:00:00+08:00", text
            assert interval.end_at.isoformat() == "2026-10-19T21:00:00+08:00", text
            checked += 1
    assert checked == 110


def test_equivalent_shift_phrasings_preserve_same_interval():
    variants = [
        "I'll work tomorrow from 7am to 3pm.",
        "My shift's tomorrow, seven till three.",
        "Tomorrow I work 7am-3pm.",
        "Work tomorrow from 0700 to 1500.",
        "Tomorrow, work from 7 AM until 3 PM.",
    ]
    for text in variants:
        doc = parse_temporal(text, NOW)
        row = next(x for x in doc.constraints if x.kind == "interval")
        assert row.start_at.isoformat() == "2026-10-08T07:00:00+08:00", text
        assert row.end_at.isoformat() == "2026-10-08T15:00:00+08:00", text


def test_start_deadline_paraphrases_share_the_same_0700_boundary():
    start_phrases = [
        "Need to be at work at seven tomorrow.",
        "Work starts at 7am tomorrow.",
        "At 7am tomorrow I start work.",
        "Tomorrow I need to reach work at 0700.",
    ]
    for text in start_phrases:
        doc = parse_temporal(text, NOW)
        point = next(x for x in doc.constraints if x.kind == "point")
        assert point.start_at.isoformat() == "2026-10-08T07:00:00+08:00", text

    deadline_phrases = [
        "Tomorrow morning I gotta reach work by 7.",
        "Need to clock in before 7am tomorrow.",
        "Tomorrow be at work no later than 7am.",
    ]
    for text in deadline_phrases:
        doc = parse_temporal(text, NOW)
        row = next(x for x in doc.constraints if x.kind in {"deadline", "not_after"})
        assert row.latest_at.isoformat() == "2026-10-08T07:00:00+08:00", text


def test_adversarial_clock_sentences_do_not_gain_create_authority():
    cases = [
        "I worked from 7am to 3pm yesterday.",
        "What if I worked at 7am tomorrow?",
        "I don't work tomorrow at 7am.",
        "Maybe I work at 7am tomorrow.",
        "I'm free from 7am to 3pm tomorrow.",
        "Don't schedule work from 7am to 3pm tomorrow.",
        "Can I work from 7am to 3pm tomorrow?",
    ]
    for text in cases:
        parsed = language_intake.parse_language(text, [], CFG, NOW)
        creates = [x for x in parsed.get("tasks", []) if x.get("action") == "create"]
        assert not creates, (text, creates)


def test_temporal_ir_itself_preserves_optional_negative_and_hypothetical_authority():
    optional = parse_temporal("Maybe I work at 7am tomorrow", NOW)
    assert any(x.optional for x in optional.constraints if x.kind == "point")

    negative = parse_temporal("I don't work at 7am tomorrow", NOW)
    assert any(x.negated for x in negative.constraints if x.kind == "point")

    hypothetical = parse_temporal("What if I worked at 7am tomorrow?", NOW)
    assert any(x.hypothetical for x in hypothetical.constraints if x.kind == "point")

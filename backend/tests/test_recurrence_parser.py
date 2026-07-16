"""
Phase 10 Part 2 — recurrence parser (app/core/recurrence_parser.py).

Deterministic, conservative parsing of a recurring-schedule phrase. Only the
recognized shapes produce a spec; everything else returns None so the routine
is saved WITHOUT a schedule (never-guess).
"""
from app.core.recurrence_parser import parse_recurrence, strip_recurrence


def test_weekly_with_meridiem():
    spec = parse_recurrence("every friday at 4pm")
    assert spec is not None
    assert spec.schedule_type == "weekly"
    assert spec.schedule_weekday == 4
    assert spec.schedule_hour == 16
    assert spec.schedule_minute == 0


def test_weekly_24h_and_minutes():
    spec = parse_recurrence("each tuesday at 16:30")
    assert spec.schedule_type == "weekly"
    assert spec.schedule_weekday == 1
    assert spec.schedule_hour == 16 and spec.schedule_minute == 30


def test_weekly_plural_day():
    spec = parse_recurrence("on mondays at 9am")
    assert spec.schedule_type == "weekly" and spec.schedule_weekday == 0
    assert spec.schedule_hour == 9


def test_daily_variants():
    for text in ("every day at 8am", "daily at 8am", "everyday at 8am"):
        spec = parse_recurrence(text)
        assert spec is not None and spec.schedule_type == "daily"
        assert spec.schedule_hour == 8 and spec.schedule_minute == 0


def test_daily_evening_hint_sets_pm():
    spec = parse_recurrence("every evening at 7")
    assert spec.schedule_type == "daily" and spec.schedule_hour == 19


def test_daily_morning_hint():
    spec = parse_recurrence("every morning at 6:30")
    assert spec.schedule_type == "daily" and spec.schedule_hour == 6 and spec.schedule_minute == 30


def test_bare_hour_default_bands():
    # 1–6 → PM
    assert parse_recurrence("every day at 4").schedule_hour == 16
    # 7–11 → AM
    assert parse_recurrence("every day at 9").schedule_hour == 9
    # explicit 24h passes through
    assert parse_recurrence("every day at 18").schedule_hour == 18


def test_interval_minutes_and_hours():
    assert parse_recurrence("every 30 minutes").schedule_interval_minutes == 30
    assert parse_recurrence("every 2 hours").schedule_interval_minutes == 120
    assert parse_recurrence("every 90 mins").schedule_interval_minutes == 90


def test_interval_clamped():
    # Below the 5-minute floor is clamped up.
    assert parse_recurrence("every 1 minute").schedule_interval_minutes == 5


def test_no_recurrence_returns_none():
    for text in ("weekly report", "do the thing", "clean my desktop", ""):
        assert parse_recurrence(text) is None


def test_strip_recurrence_cleans_name():
    cleaned, spec = strip_recurrence('weekly report that runs every friday at 4pm')
    assert cleaned == "weekly report"
    assert spec is not None and spec.schedule_type == "weekly" and spec.schedule_weekday == 4


def test_strip_recurrence_quoted_name():
    cleaned, spec = strip_recurrence('"digest" that runs every day at 8am')
    assert cleaned.strip('"') == "digest"
    assert spec is not None and spec.schedule_type == "daily"


def test_strip_recurrence_no_schedule():
    cleaned, spec = strip_recurrence("cleanup desktop")
    assert cleaned == "cleanup desktop" and spec is None

"""
Phase 10 Part 1 — pattern mining (app/core/pattern_mining.py).

detect_cadence is pure and timezone-agnostic (operates on .weekday()/.hour), so
it is tested directly with controlled local datetimes. mine_task_patterns /
cadence_for_goal run against a shared in-memory DB of completed Task rows; a
uniform UTC→local offset preserves same-weekday/same-hour clustering, so the
cadence KIND is assertable regardless of the machine timezone.
"""
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.pattern_mining import (
    PatternCadence,
    cadence_for_goal,
    cadence_to_schedule,
    describe_cadence,
    detect_cadence,
    format_task_patterns,
    mine_task_patterns,
    teach_phrase_cadence,
)
from app.db.database import Base
from app.db.models import Task, utc_now


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _add_completed(factory, goal: str, finished_at):
    async with factory() as db:
        db.add(Task(goal=goal, status="completed", finished_at=finished_at))
        await db.commit()


# =========================================================== detect_cadence

def test_detect_weekly():
    # Four Fridays at 16:00 + one stray Monday — dominant weekday + hour cluster.
    fridays = [datetime(2026, 7, 3, 16, 0) + timedelta(weeks=i) for i in range(4)]
    fridays.append(datetime(2026, 7, 6, 9, 0))  # a Monday outlier
    cadence = detect_cadence(fridays)
    assert cadence is not None
    assert cadence.kind == "weekly"
    assert cadence.weekday == 4  # Friday
    assert cadence.hour == 16


def test_detect_daily_across_weekdays():
    # 08:00 on five different weekdays → daily (spread ≥ 3 distinct weekdays).
    dts = [datetime(2026, 7, 6, 8, 0) + timedelta(days=i) for i in range(5)]
    cadence = detect_cadence(dts)
    assert cadence is not None
    assert cadence.kind == "daily"
    assert cadence.hour == 8


def test_detect_none_when_scattered():
    dts = [
        datetime(2026, 7, 6, 8, 0),
        datetime(2026, 7, 7, 14, 0),
        datetime(2026, 7, 9, 22, 0),
    ]
    assert detect_cadence(dts) is None


def test_detect_none_below_threshold():
    assert detect_cadence([datetime(2026, 7, 3, 16, 0), datetime(2026, 7, 10, 16, 0)]) is None


def test_detect_hour_band_tolerance():
    # 15:30 / 16:00 / 16:30 same weekday — within the 2h band → weekly.
    dts = [
        datetime(2026, 7, 3, 15, 30),
        datetime(2026, 7, 10, 16, 0),
        datetime(2026, 7, 17, 16, 30),
    ]
    cadence = detect_cadence(dts)
    assert cadence is not None and cadence.kind == "weekly" and cadence.weekday == 4


# ============================================================== rendering

def test_describe_and_teach_phrase_weekly():
    c = PatternCadence(kind="weekly", weekday=4, hour=16, minute=0)
    assert describe_cadence(c) == "every Friday around 4:00 PM"
    assert teach_phrase_cadence(c) == "every friday at 4:00pm"


def test_cadence_to_schedule():
    c = PatternCadence(kind="weekly", weekday=4, hour=16, minute=30)
    assert cadence_to_schedule(c) == {
        "schedule_type": "weekly", "schedule_weekday": 4,
        "schedule_hour": 16, "schedule_minute": 30,
    }
    d = PatternCadence(kind="daily", weekday=None, hour=8, minute=0)
    assert cadence_to_schedule(d) == {
        "schedule_type": "daily", "schedule_hour": 8, "schedule_minute": 0,
    }
    assert cadence_to_schedule(None) is None


def test_format_task_patterns():
    from app.core.pattern_mining import PatternCandidate
    cands = [
        PatternCandidate("compile the week", "Compile the week", 4,
                         PatternCadence(kind="weekly", weekday=4, hour=16, minute=0)),
        PatternCandidate("water plants", "Water the plants", 3, None),
    ]
    out = format_task_patterns(cands)
    assert "Compile the week" in out and "done 4 times" in out
    assert "every Friday around 4:00 PM" in out
    assert "Water the plants" in out


def test_format_task_patterns_empty():
    assert format_task_patterns([]) == ""


# ========================================================= DB-backed queries

async def test_mine_groups_and_counts(factory):
    base = datetime(2026, 7, 3, 16, 0)
    for i in range(4):
        await _add_completed(factory, "Compile the week", base + timedelta(weeks=i))
    await _add_completed(factory, "one-off thing", utc_now())

    async with factory() as db:
        patterns = await mine_task_patterns(db)

    goals = {p.normalized_goal: p for p in patterns}
    assert "compile the week" in goals
    assert goals["compile the week"].count == 4
    assert goals["compile the week"].cadence is not None
    assert goals["compile the week"].cadence.kind == "weekly"
    # A goal seen once is below MIN_OCCURRENCES — not a pattern.
    assert "one off thing" not in goals


async def test_cadence_for_goal(factory):
    base = datetime(2026, 7, 3, 16, 0)
    for i in range(4):
        await _add_completed(factory, "Compile the week", base + timedelta(weeks=i))
    async with factory() as db:
        cadence = await cadence_for_goal(db, "compile the WEEK!")  # normalized match
    assert cadence is not None and cadence.kind == "weekly"


async def test_cadence_for_goal_missing(factory):
    async with factory() as db:
        assert await cadence_for_goal(db, "never done") is None

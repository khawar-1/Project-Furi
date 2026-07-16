"""
Phase 9 — the Initiative Engine (app/core/initiative.py).

Real JarvisScheduler on a shared in-memory DB (the test_daily_briefing.py
pattern): app_settings, scheduled_jobs, suggestions, and the handler's own
AsyncSessionLocal all point at one engine; the app-wide `scheduler` singleton
is swapped so initiative (which imports it directly) uses the test one; and
create_provider is stubbed so no test hits a real LLM.

Covers: interval next-run; sync arm/cancel/replace; the governor (quiet-hours,
budget, rate-limit skips) BEFORE the LLM; dedupe; the autonomy policy matrix;
gather best-effort; compose validate-retry-else-empty; dispatch (suggest/ask
rows + push, act → start_task); the disabled-now / stale-job guards; the
governor re-arms even when it skips; ensure_initiative_job arm/sweep; run-now.
"""
import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import initiative as ini
from app.core.app_settings import (
    InitiativeConfig,
    get_initiative_config,
    get_initiative_job_id,
    set_initiative_config,
    set_initiative_job_id,
)
from app.core.initiative import (
    INITIATIVE_JOB_KIND,
    classify_autonomy,
    compose_initiatives,
    ensure_initiative_job,
    next_initiative_run_at,
    run_initiative_now,
    sync_initiative_job,
    _in_quiet_hours,
)
from app.core.push import push_manager
from app.core.scheduler import JarvisScheduler, utc_now
from app.db.database import Base
from app.db.models import Message, Suggestion, Task
from tests.test_push_channel import FakeSocket


# ================================================================= fixtures

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


@pytest_asyncio.fixture
async def sched(factory):
    s = JarvisScheduler(session_factory=factory)
    yield s
    await s.shutdown()


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeProvider:
    """Returns a fixed content string (JSON) or raises. Records call count."""
    def __init__(self, content="", exc=None):
        self._content, self._exc = content, exc
        self.calls = 0

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._content)


def _one_initiative_json(autonomy="ask", action="Draft a reply to Bob about lunch"):
    return json.dumps({"initiatives": [{
        "title": "Reply to Bob",
        "category": "email_followup",
        "rationale": "Bob asked about lunch and is waiting on you.",
        "body": "Want me to draft a reply to Bob?",
        "proposed_action": action,
        "suggested_autonomy": autonomy,
        "priority": "normal",
    }]})


@pytest.fixture
def fake_provider():
    return _FakeProvider(content=_one_initiative_json())


@pytest.fixture(autouse=True)
def _wire(factory, sched, monkeypatch):
    import app.core.scheduler as scheduler_module

    monkeypatch.setattr("app.db.database.AsyncSessionLocal", factory)
    monkeypatch.setattr(ini, "scheduler", sched)
    monkeypatch.setattr(scheduler_module, "scheduler", sched)
    sched.register_handler(INITIATIVE_JOB_KIND, ini._initiative_job_handler)
    push_manager._connections.clear()
    yield
    push_manager._connections.clear()


# ------------------------------------------------------------------- helpers

async def _pending(sched):
    return await sched.list_jobs(status="pending", kind=INITIATIVE_JOB_KIND, limit=100)


async def _enable(factory, sched, **overrides):
    cfg = InitiativeConfig(
        enabled=overrides.get("enabled", True),
        autonomy=overrides.get("autonomy", "ask"),
        interval_minutes=overrides.get("interval_minutes", 45),
        daily_budget=overrides.get("daily_budget", 5),
        quiet_start_hour=overrides.get("quiet_start_hour", _non_quiet()[0]),
        quiet_end_hour=overrides.get("quiet_end_hour", _non_quiet()[1]),
        min_gap_minutes=overrides.get("min_gap_minutes", 30),
    )
    async with factory() as db:
        await set_initiative_config(db, cfg)
    return cfg


def _non_quiet():
    """A quiet window that does NOT include the current hour."""
    h = datetime.now().hour
    return ((h + 2) % 24, (h + 3) % 24)


def _quiet_now():
    """A quiet window that DOES include the current hour."""
    h = datetime.now().hour
    return (h, (h + 1) % 24)


async def _arm_at(factory, sched, run_at, cfg):
    async with factory() as db:
        await set_initiative_config(db, cfg)
        job_id = await sched.schedule_at(
            run_at, INITIATIVE_JOB_KIND, {"interval_minutes": cfg.interval_minutes}
        )
        await set_initiative_job_id(db, job_id)
    return job_id


async def _seed_signal(factory):
    """A completed task + a chat session so gather is non-empty and there is a
    latest session id to attach suggestions to."""
    async with factory() as db:
        db.add(Message(session_id="s-chat", role="user", content="hi"))
        db.add(Task(goal="Tidy my desktop", status="completed", finished_at=utc_now()))
        await db.commit()


# ============================================================ occurrence math

def test_next_run_is_pure_interval():
    now = datetime(2026, 7, 16, 10, 0)
    assert next_initiative_run_at(45, now) == now + timedelta(minutes=45)


def test_next_run_floors_at_one_minute():
    now = datetime(2026, 7, 16, 10, 0)
    assert next_initiative_run_at(0, now) == now + timedelta(minutes=1)


# =============================================================== quiet hours

def test_quiet_hours_wraparound():
    assert _in_quiet_hours(datetime(2026, 7, 16, 23, 0), 22, 8) is True
    assert _in_quiet_hours(datetime(2026, 7, 16, 3, 0), 22, 8) is True
    assert _in_quiet_hours(datetime(2026, 7, 16, 12, 0), 22, 8) is False


def test_quiet_hours_same_start_end_is_never_quiet():
    assert _in_quiet_hours(datetime(2026, 7, 16, 12, 0), 9, 9) is False


def test_quiet_hours_non_wrapping():
    assert _in_quiet_hours(datetime(2026, 7, 16, 10, 0), 9, 17) is True
    assert _in_quiet_hours(datetime(2026, 7, 16, 20, 0), 9, 17) is False


# ========================================================== autonomy policy

def test_policy_off_drops_everything():
    assert classify_autonomy("act", True, "off") == "drop"
    assert classify_autonomy("suggest", False, "off") == "drop"


def test_policy_no_goal_is_always_suggest():
    assert classify_autonomy("act", False, "act") == "suggest"
    assert classify_autonomy("ask", False, "ask") == "suggest"


def test_policy_ceiling_downgrades_never_upgrades():
    # act proposal capped by the ceiling
    assert classify_autonomy("act", True, "act") == "act"
    assert classify_autonomy("act", True, "ask") == "ask"
    assert classify_autonomy("act", True, "suggest") == "suggest"
    # a low proposal is never raised past what it asked for
    assert classify_autonomy("ask", True, "act") == "ask"
    assert classify_autonomy("suggest", True, "act") == "suggest"


# ================================================================ sync hooks

async def test_sync_arms_when_enabled(factory, sched):
    await _enable(factory, sched)
    async with factory() as db:
        await sync_initiative_job(db)
        pointer = await get_initiative_job_id(db)
    jobs = await _pending(sched)
    assert len(jobs) == 1 and jobs[0]["id"] == pointer


async def test_sync_disabled_arms_nothing(factory, sched):
    await _enable(factory, sched, enabled=False)
    async with factory() as db:
        await sync_initiative_job(db)
        assert await get_initiative_job_id(db) is None
    assert await _pending(sched) == []


async def test_sync_replaces_pointer(factory, sched):
    await _enable(factory, sched)
    async with factory() as db:
        await sync_initiative_job(db)
        first = await get_initiative_job_id(db)
    async with factory() as db:
        await sync_initiative_job(db)
        second = await get_initiative_job_id(db)
    assert first != second
    jobs = await _pending(sched)
    assert len(jobs) == 1 and jobs[0]["id"] == second


# ================================================================ composition

async def test_compose_parses_valid_json(fake_provider):
    signals = {"events": [{"summary": "x", "when": "now", "location": ""}],
               "unread_emails": [], "birthdays": [], "memories": [],
               "cadence": {}, "world": {}, "affinities": {}, "recent_suggestions": []}
    out = await compose_initiatives(signals, provider=fake_provider)
    assert len(out) == 1 and out[0].title == "Reply to Bob"


async def test_compose_empty_signals_skips_llm():
    prov = _FakeProvider(exc=AssertionError("must not be called"))
    out = await compose_initiatives({"events": [], "unread_emails": [], "birthdays": [],
                                     "memories": [], "cadence": {}, "world": {}}, provider=prov)
    assert out == []
    assert prov.calls == 0


async def test_compose_retries_once_then_gives_up():
    prov = _FakeProvider(content="not json at all")
    signals = {"memories": ["something today"], "events": [], "unread_emails": [],
               "birthdays": [], "cadence": {}, "world": {}, "affinities": {},
               "recent_suggestions": []}
    out = await compose_initiatives(signals, provider=prov)
    assert out == []          # never fabricates
    assert prov.calls == 2     # tried, fed the error back, tried once more


async def test_compose_drops_unusable_candidates():
    prov = _FakeProvider(content=json.dumps({"initiatives": [
        {"title": "", "body": "", "rationale": ""},                       # blank shell
        {"title": "Real", "body": "Do it", "rationale": "because",
         "category": "general", "suggested_autonomy": "suggest"},
    ]}))
    signals = {"memories": ["x"], "events": [], "unread_emails": [], "birthdays": [],
               "cadence": {}, "world": {}, "affinities": {}, "recent_suggestions": []}
    out = await compose_initiatives(signals, provider=prov)
    assert [c.title for c in out] == ["Real"]


# ================================================================== firing

async def test_fire_ask_creates_suggestion_pushes_and_rearms(factory, sched, monkeypatch):
    monkeypatch.setattr(ini, "create_provider", lambda: _FakeProvider(_one_initiative_json("ask")))
    await _seed_signal(factory)
    cfg = await _enable(factory, sched, autonomy="ask")
    job_id = await _arm_at(factory, sched, utc_now(), cfg)

    sock = FakeSocket()
    await push_manager.connect(sock)
    await sched._fire(job_id)

    async with factory() as db:
        rows = (await db.execute(select(Suggestion))).scalars().all()
        assert len(rows) == 1
        assert rows[0].autonomy == "ask" and rows[0].status == "pending"
        assert rows[0].goal  # ask keeps its goal
        pointer = await get_initiative_job_id(db)
    assert any(s["type"] == "suggestion" for s in sock.sent)
    # re-armed
    assert pointer is not None and pointer != job_id
    assert len(await _pending(sched)) == 1


async def test_fire_act_starts_task(factory, sched, monkeypatch):
    monkeypatch.setattr(ini, "create_provider", lambda: _FakeProvider(_one_initiative_json("act")))

    started = {}

    class _FakeTask:
        id = "task-xyz"

    async def _fake_start(db, goal, session_id, conversation="", memory="", provider=None):
        started["goal"] = goal
        return _FakeTask()

    import app.agents as agents
    monkeypatch.setattr(agents, "start_task", _fake_start)
    monkeypatch.setattr(agents, "planner_memory_context", lambda db, goal: _async_str())

    await _seed_signal(factory)
    cfg = await _enable(factory, sched, autonomy="act")
    job_id = await _arm_at(factory, sched, utc_now(), cfg)
    await sched._fire(job_id)

    assert started.get("goal")  # start_task was called with the goal string
    async with factory() as db:
        rows = (await db.execute(select(Suggestion))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "acted" and rows[0].task_id == "task-xyz"


async def _async_str():
    return ""


# ============================================================ governor skips

async def test_quiet_hours_skips_but_rearms(factory, sched, monkeypatch):
    monkeypatch.setattr(ini, "create_provider",
                        lambda: _FakeProvider(exc=AssertionError("LLM must not run in quiet hours")))
    await _seed_signal(factory)
    qs, qe = _quiet_now()
    cfg = await _enable(factory, sched, quiet_start_hour=qs, quiet_end_hour=qe)
    job_id = await _arm_at(factory, sched, utc_now(), cfg)

    await sched._fire(job_id)  # must not raise → LLM never called
    async with factory() as db:
        assert (await db.execute(select(Suggestion))).scalars().all() == []
        pointer = await get_initiative_job_id(db)
    assert pointer != job_id and len(await _pending(sched)) == 1  # re-armed


async def test_budget_exhausted_skips_before_llm(factory, sched, monkeypatch):
    monkeypatch.setattr(ini, "create_provider",
                        lambda: _FakeProvider(exc=AssertionError("budget exhausted → no LLM")))
    await _seed_signal(factory)
    cfg = await _enable(factory, sched, daily_budget=1)
    # Already one suggestion today → budget used up.
    async with factory() as db:
        db.add(Suggestion(title="prior", body="b", category="general", status="pending",
                          autonomy="suggest", created_at=utc_now()))
        await db.commit()
    job_id = await _arm_at(factory, sched, utc_now(), cfg)
    await sched._fire(job_id)  # must not raise
    async with factory() as db:
        rows = (await db.execute(select(Suggestion))).scalars().all()
        assert len(rows) == 1  # no new one created
        assert len(await _pending(sched)) == 1  # re-armed


async def test_rate_limited_skips(factory, sched, monkeypatch):
    monkeypatch.setattr(ini, "create_provider",
                        lambda: _FakeProvider(exc=AssertionError("rate-limited → no LLM")))
    await _seed_signal(factory)
    cfg = await _enable(factory, sched, min_gap_minutes=60, daily_budget=10)
    async with factory() as db:
        db.add(Suggestion(title="recent", body="b", category="general", status="pending",
                          autonomy="suggest", created_at=utc_now()))
        await db.commit()
    job_id = await _arm_at(factory, sched, utc_now(), cfg)
    await sched._fire(job_id)
    async with factory() as db:
        assert len(await _pending(sched)) == 1


async def test_disabled_now_does_not_fire_or_rearm(factory, sched):
    cfg = await _enable(factory, sched)
    job_id = await _arm_at(factory, sched, utc_now(), cfg)
    async with factory() as db:
        await set_initiative_config(db, InitiativeConfig(
            enabled=False, autonomy="ask", interval_minutes=45, daily_budget=5,
            quiet_start_hour=22, quiet_end_hour=8, min_gap_minutes=30))
    sock = FakeSocket()
    await push_manager.connect(sock)
    await sched._fire(job_id)
    assert sock.sent == []
    assert await _pending(sched) == []


async def test_stale_job_does_not_fire(factory, sched):
    cfg = await _enable(factory, sched)
    job_id = await _arm_at(factory, sched, utc_now(), cfg)
    async with factory() as db:
        await set_initiative_job_id(db, "some-other-job")
    sock = FakeSocket()
    await push_manager.connect(sock)
    await sched._fire(job_id)
    assert sock.sent == []


# ===================================================== startup reconciliation

async def test_ensure_arms_when_enabled(factory, sched):
    await _enable(factory, sched)
    await ensure_initiative_job()
    jobs = await _pending(sched)
    assert len(jobs) == 1
    async with factory() as db:
        assert await get_initiative_job_id(db) == jobs[0]["id"]


async def test_ensure_default_disabled_arms_nothing(factory, sched):
    # Never configured → default is disabled. ensure arms nothing.
    await ensure_initiative_job()
    assert await _pending(sched) == []


async def test_ensure_sweeps_strays(factory, sched):
    await _enable(factory, sched)
    stray = await sched.schedule_at(utc_now() + timedelta(hours=1), INITIATIVE_JOB_KIND, {})
    await ensure_initiative_job()
    ids = {j["id"] for j in await _pending(sched)}
    assert stray not in ids


async def test_ensure_expires_stale_suggestions(factory, sched):
    async with factory() as db:
        db.add(Suggestion(title="old", body="b", category="general", status="pending",
                          autonomy="suggest", created_at=utc_now() - timedelta(days=3),
                          expires_at=utc_now() - timedelta(days=2)))
        await db.commit()
    await ensure_initiative_job()
    async with factory() as db:
        row = (await db.execute(select(Suggestion))).scalars().one()
        assert row.status == "expired"


# =================================================================== run-now

async def test_run_now_surfaces(factory, sched, monkeypatch):
    monkeypatch.setattr(ini, "create_provider", lambda: _FakeProvider(_one_initiative_json("suggest")))
    await _seed_signal(factory)
    await _enable(factory, sched, autonomy="suggest")
    async with factory() as db:
        n = await run_initiative_now(db)
    assert n == 1
    async with factory() as db:
        assert len((await db.execute(select(Suggestion))).scalars().all()) == 1


async def test_run_now_noop_when_off(factory, sched):
    await _enable(factory, sched, autonomy="off")
    async with factory() as db:
        assert await run_initiative_now(db) == 0


# ============================ Phase 10 signals: patterns + prep

def test_render_includes_patterns_and_prep():
    from app.core.pattern_mining import PatternCadence, PatternCandidate
    signals = {
        "world": {}, "events": [], "unread_emails": [], "birthdays": [],
        "memories": [], "cadence": {}, "affinities": {}, "recent_suggestions": [],
        "patterns": [PatternCandidate(
            "compile the week", "Compile the week", 4,
            PatternCadence(kind="weekly", weekday=4, hour=16, minute=0))],
        "prep": {
            "meetings": [{"summary": "Design review", "when": "2026-07-16 15:00"}],
            "inbox_triage": True,
        },
    }
    block = ini._render_signal_block(signals)
    assert "RECURRING PATTERNS" in block and "Compile the week" in block
    assert "UPCOMING MEETINGS" in block and "Design review" in block
    assert "INBOX:" in block


def test_is_empty_considers_patterns_and_prep():
    base = {"world": {}, "events": [], "unread_emails": [], "birthdays": [],
            "memories": [], "cadence": {}, "affinities": {}, "recent_suggestions": []}
    assert ini._is_empty({**base, "patterns": [], "prep": {"meetings": [], "inbox_triage": False}})
    assert not ini._is_empty({**base, "patterns": [object()], "prep": {}})
    assert not ini._is_empty({**base, "patterns": [], "prep": {"inbox_triage": True}})


def test_morning_triage_window(monkeypatch):
    import app.core.initiative as m
    from datetime import datetime as _dt

    class _Clock(_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 16, 9, 0)  # 09:00 local — inside the window

    monkeypatch.setattr(m, "datetime", _Clock)
    assert m._morning_triage_due(3) is True
    assert m._morning_triage_due(0) is False  # no unread → no opportunity


async def test_gather_patterns_from_tasks(factory):
    from datetime import timedelta
    base = datetime(2026, 7, 3, 16, 0)
    async with factory() as db:
        for i in range(3):
            db.add(Task(goal="compile the week", status="completed",
                        finished_at=base + timedelta(weeks=i)))
        await db.commit()
        patterns = await ini._gather_patterns(db)
    assert any(p.normalized_goal == "compile the week" for p in patterns)


# ==================== Phase 11 signals: people cadence + threads + callbacks

def test_render_includes_relationship_signals():
    signals = {
        "world": {}, "events": [], "unread_emails": [], "birthdays": [],
        "memories": [], "cadence": {}, "affinities": {}, "recent_suggestions": [],
        "patterns": [], "prep": {},
        "people_cadence": [{"name": "Jamil", "weeks_since": 6, "relationship_type": "friend"}],
        "goal_threads": [{"title": "The deadline", "description": "worried", "event_date": None}],
        "memory_callbacks": [],
    }
    block = ini._render_signal_block(signals)
    assert "PEOPLE YOU HAVEN'T CAUGHT UP WITH" in block and "Jamil" in block
    assert "OPEN THREADS" in block and "The deadline" in block


def test_is_empty_considers_relationship_signals():
    base = {"world": {}, "events": [], "unread_emails": [], "birthdays": [],
            "memories": [], "cadence": {}, "affinities": {}, "recent_suggestions": [],
            "patterns": [], "prep": {}}
    assert ini._is_empty({**base, "people_cadence": [], "goal_threads": [], "memory_callbacks": []})
    assert not ini._is_empty({**base, "people_cadence": [{"name": "X", "weeks_since": 5}]})
    assert not ini._is_empty({**base, "goal_threads": [{"title": "t"}]})


async def test_gather_goal_threads_marks_nudged(factory):
    from datetime import timedelta
    from app.core.goal_threads import get_thread, upsert_thread
    async with factory() as db:
        t = await upsert_thread(db, "worried about the deadline")
        t.next_check_at = utc_now() - timedelta(days=1)  # make it due
        await db.commit()
        tid = t.id

        out = await ini._gather_goal_threads(db)
        assert any(x["title"] == "worried about the deadline" for x in out)

        # It was marked nudged, so it is no longer due (no re-nudge next heartbeat).
        refreshed = await get_thread(db, tid)
        assert refreshed.last_nudged_at is not None
        assert refreshed.next_check_at > utc_now()


# ==================== Phase 13: affective governor (high-load surfacing bar)

def _cfg_high_gap():
    return InitiativeConfig(
        enabled=True, autonomy="ask", interval_minutes=45, daily_budget=5,
        quiet_start_hour=_non_quiet()[0], quiet_end_hour=_non_quiet()[1],
        min_gap_minutes=30,
    )


def _priority_json(priority: str):
    return json.dumps({"initiatives": [{
        "title": "Reply to Bob", "category": "email_followup",
        "rationale": "Bob is waiting.", "body": "Draft a reply?",
        "proposed_action": "Draft a reply to Bob", "suggested_autonomy": "ask",
        "priority": priority,
    }]})


async def test_under_load_drops_non_high_priority(factory, monkeypatch):
    """Busy+confident world → a normal-priority candidate is filtered out."""
    async def _signals(db):
        return {"world": {"user_state": {"load": "busy", "confidence": 0.67}},
                "events": [{"when": "now", "summary": "x"}]}
    monkeypatch.setattr(ini, "gather_initiative_signals", _signals)
    async with factory() as db:
        surfaced = await ini._run_pass(db, _cfg_high_gap(),
                                       provider=_FakeProvider(_priority_json("normal")))
    assert surfaced == 0
    async with factory() as db:
        assert len((await db.execute(select(Suggestion))).scalars().all()) == 0


async def test_under_load_keeps_high_priority(factory, monkeypatch):
    """A HIGH-priority candidate still surfaces even under load."""
    async def _signals(db):
        return {"world": {"user_state": {"load": "stressed", "confidence": 0.67}},
                "events": [{"when": "now", "summary": "x"}]}
    monkeypatch.setattr(ini, "gather_initiative_signals", _signals)
    async with factory() as db:
        surfaced = await ini._run_pass(db, _cfg_high_gap(),
                                       provider=_FakeProvider(_priority_json("high")))
    assert surfaced == 1


async def test_low_confidence_load_does_not_filter(factory, monkeypatch):
    """A busy read below the confidence gate never changes surfacing."""
    async def _signals(db):
        return {"world": {"user_state": {"load": "busy", "confidence": 0.2}},
                "events": [{"when": "now", "summary": "x"}]}
    monkeypatch.setattr(ini, "gather_initiative_signals", _signals)
    async with factory() as db:
        surfaced = await ini._run_pass(db, _cfg_high_gap(),
                                       provider=_FakeProvider(_priority_json("normal")))
    assert surfaced == 1


def test_under_load_helper_gates_on_confidence():
    assert ini._under_load({"world": {"user_state": {"load": "busy", "confidence": 0.5}}}) is True
    assert ini._under_load({"world": {"user_state": {"load": "busy", "confidence": 0.1}}}) is False
    assert ini._under_load({"world": {}}) is False
    assert ini._under_load({}) is False

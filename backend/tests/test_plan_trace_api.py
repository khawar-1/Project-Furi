"""GET /api/activity/plans — reading the plan-trace audit trail over real HTTP.

Real HTTP (httpx ASGITransport, the test_routing_api.py pattern) over an
in-memory DB. No test touches the real jarvis.db.

The load-bearing test here is the same ORDERING one its sibling has: this router
owns GET /{session_id}, so a /plans declared after it would be read as a session
id and return an empty list — which looks EXACTLY like "no plan has ever failed",
i.e. it would fail silently in the one way that matters.
"""
import json
from datetime import timedelta

import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import plan_trace as pt
from app.core.dependencies import get_db
from app.db.database import Base
from app.db.models import PlanTrace, utc_now
from main import app


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.factory = factory  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(client, rows: list[dict]) -> None:
    async with client.factory() as db:
        for i, spec in enumerate(rows):
            row = PlanTrace(**spec)
            # Deterministic ordering: later entries are newer.
            row.created_at = utc_now() - timedelta(minutes=len(rows) - i)
            db.add(row)
        await db.commit()


async def test_plans_is_not_swallowed_by_the_session_id_route(client):
    """⚠️ THE ORDERING TEST. If /plans came after /{session_id}, this would 200
    with [] — indistinguishable from a working endpoint on an empty table."""
    await _seed(client, [
        {"session_id": "s1", "goal": "delete the temp files", "status": "failed",
         "fail_class": pt.FAIL_REPLAN_CAP},
    ])

    r = await client.get("/api/activity/plans")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1, "the /plans path was matched as a session id"
    assert body[0]["goal"] == "delete the temp files"
    assert body[0]["fail_class"] == pt.FAIL_REPLAN_CAP


async def test_rows_come_back_newest_first_with_the_full_diagnosis(client):
    await _seed(client, [
        {"session_id": "s1", "goal": "older", "status": "completed"},
        {"session_id": "s1", "goal": "newer", "status": "failed",
         "fail_class": pt.FAIL_UNROUTED_STEP, "agent_key": "file",
         "entry": pt.ENTRY_RESUME, "execution": "background",
         "replan_count": 2, "steps_total": 3, "steps_failed": 1,
         "failed_tool": "move_files", "failed_error": "destination is a file",
         "rejections": json.dumps([{"guard": pt.GUARD_SCOPE, "feedback": "narrowed"}]),
         "rejection_count": 1, "duration_ms": 4200},
    ])

    body = (await client.get("/api/activity/plans")).json()

    assert [row["goal"] for row in body] == ["newer", "older"]
    top = body[0]
    assert top["fail_class"] == pt.FAIL_UNROUTED_STEP
    assert top["agent_key"] == "file"
    assert top["entry"] == pt.ENTRY_RESUME
    assert top["execution"] == "background"
    assert top["replan_count"] == 2
    assert top["failed_tool"] == "move_files"
    assert top["failed_error"] == "destination is a file"
    # The rejections come back as a LIST, not the stored JSON string — the
    # diagnosis has to be readable without a second parse.
    assert top["rejections"] == [{"guard": pt.GUARD_SCOPE, "feedback": "narrowed"}]
    assert top["created_at"].endswith("+00:00"), "timestamps must carry the UTC offset"


async def test_the_fail_classes_are_separately_queryable(client):
    """The whole point of the table: 'the draft LLM returned junk', 'a real step
    failed and no replan routed around it' and 'the replan budget ran out' all
    produce a FAILED plan and need different fixes."""
    await _seed(client, [
        {"goal": "draft", "status": "failed", "fail_class": pt.FAIL_DRAFT_UNUSABLE},
        {"goal": "unrouted", "status": "failed", "fail_class": pt.FAIL_UNROUTED_STEP},
        {"goal": "cap", "status": "failed", "fail_class": pt.FAIL_REPLAN_CAP},
    ])

    for fail_class, expected in (
        (pt.FAIL_DRAFT_UNUSABLE, "draft"),
        (pt.FAIL_UNROUTED_STEP, "unrouted"),
        (pt.FAIL_REPLAN_CAP, "cap"),
    ):
        body = (await client.get(
            f"/api/activity/plans?fail_class={fail_class}"
        )).json()
        assert [row["goal"] for row in body] == [expected]


async def test_one_plans_whole_story_is_readable_in_order(client):
    """Granularity is per invocation, so `plan_id` is how the episodes of one
    plan are joined back together."""
    await _seed(client, [
        {"plan_id": "p1", "goal": "g", "status": "awaiting_approval", "entry": pt.ENTRY_START},
        {"plan_id": "p2", "goal": "other", "status": "completed"},
        {"plan_id": "p1", "goal": "g", "status": "failed", "entry": pt.ENTRY_RESUME,
         "fail_class": pt.FAIL_UNROUTED_STEP},
    ])

    body = (await client.get("/api/activity/plans?plan_id=p1")).json()

    assert len(body) == 2
    assert [row["entry"] for row in body] == [pt.ENTRY_RESUME, pt.ENTRY_START]


async def test_filters_by_status_tool_and_session(client):
    await _seed(client, [
        {"session_id": "s1", "goal": "a", "status": "completed"},
        {"session_id": "s2", "goal": "b", "status": "failed", "failed_tool": "delete_file"},
        {"session_id": "s2", "goal": "c", "status": "failed", "failed_tool": "browse"},
    ])

    by_status = (await client.get("/api/activity/plans?status=failed")).json()
    assert {row["goal"] for row in by_status} == {"b", "c"}

    by_tool = (await client.get("/api/activity/plans?failed_tool=browse")).json()
    assert [row["goal"] for row in by_tool] == ["c"]

    by_session = (await client.get("/api/activity/plans?session_id=s1")).json()
    assert [row["goal"] for row in by_session] == ["a"]


async def test_a_corrupt_rejections_blob_never_reaches_the_caller(client):
    """The column is opaque text. A half-written blob must degrade to an empty
    list, not surface as a raw string the UI would render as one giant item."""
    await _seed(client, [{"goal": "g", "status": "failed", "rejections": "{not json"}])

    body = (await client.get("/api/activity/plans")).json()

    assert body[0]["rejections"] == []


async def test_limit_is_bounded(client):
    assert (await client.get("/api/activity/plans?limit=0")).status_code == 422
    assert (await client.get("/api/activity/plans?limit=99999")).status_code == 422


async def test_an_ordinary_session_lookup_still_works(client):
    """The sibling route must be unaffected by the one declared above it."""
    r = await client.get("/api/activity/some-session-id")
    assert r.status_code == 200
    assert r.json() == []

"""GET /api/activity/routing — reading the routing audit trail over real HTTP.

Real HTTP (httpx ASGITransport, the test_index_api.py pattern) over an in-memory
DB. No test touches the real jarvis.db.

The load-bearing test here is the ROUTE ORDERING one. FastAPI matches routes in
registration order, and this router already owns GET /{session_id} — so if
/routing were declared after it, "routing" would be read as a session id and the
endpoint would return an empty list. Which looks EXACTLY like "nothing has been
routed yet", i.e. it would fail silently in the one way that matters.
"""
from datetime import timedelta

import httpx
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import routing_trace as rt
from app.core.dependencies import get_db
from app.db.database import Base
from app.db.models import RoutingDecision, utc_now
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
            row = RoutingDecision(**spec)
            # Deterministic ordering: later entries are newer.
            row.created_at = utc_now() - timedelta(minutes=len(rows) - i)
            db.add(row)
        await db.commit()


async def test_routing_is_not_swallowed_by_the_session_id_route(client):
    """⚠️ THE ORDERING TEST. /api/activity/{session_id} is declared in the same
    router; if /routing came after it, this would 200 with [] — indistinguishable
    from a working endpoint on an empty table."""
    await _seed(client, [
        {"session_id": "s1", "message": "how are you", "outcome": rt.OUTCOME_CHAT,
         "fail_open_reason": rt.FAIL_GATE_CLOSED, "gate_fired": False},
    ])

    r = await client.get("/api/activity/routing")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1, "the /routing path was matched as a session id"
    assert body[0]["message"] == "how are you"
    assert body[0]["fail_open_reason"] == rt.FAIL_GATE_CLOSED


async def test_rows_come_back_newest_first_with_the_full_decision(client):
    await _seed(client, [
        {"session_id": "s1", "message": "older", "outcome": rt.OUTCOME_CHAT},
        {"session_id": "s1", "message": "newer", "outcome": rt.OUTCOME_TASK_BACKGROUND,
         "label": "TASK", "mode": "DELEGATE", "agent": "file", "execution": "delegate",
         "gate_fired": True, "gate_reason": "strong_domain", "classifier_ms": 812,
         "classifier_model": "deepseek-v4-flash", "route_ms": 900},
    ])

    body = (await client.get("/api/activity/routing")).json()

    assert [row["message"] for row in body] == ["newer", "older"]
    top = body[0]
    assert top["label"] == "TASK"
    assert top["agent"] == "file"
    assert top["execution"] == "delegate"
    assert top["gate_reason"] == "strong_domain"
    assert top["classifier_ms"] == 812
    assert top["classifier_model"] == "deepseek-v4-flash"
    assert top["created_at"].endswith("+00:00"), "timestamps must carry the UTC offset"


async def test_the_three_chat_causes_are_separately_queryable(client):
    """The whole point of the table: 'the gate never fired', 'the model said
    conversation' and 'the model call failed' all produce a chat reply and need
    different fixes."""
    await _seed(client, [
        {"session_id": "s", "message": "gate", "outcome": rt.OUTCOME_CHAT,
         "fail_open_reason": rt.FAIL_GATE_CLOSED},
        {"session_id": "s", "message": "judged", "outcome": rt.OUTCOME_CHAT,
         "fail_open_reason": rt.FAIL_CLASSIFIER_CHAT},
        {"session_id": "s", "message": "broke", "outcome": rt.OUTCOME_CHAT,
         "fail_open_reason": rt.FAIL_CLASSIFIER_ERROR,
         "classifier_error": "ConnectError: "},
    ])

    for reason, expected in (
        (rt.FAIL_GATE_CLOSED, "gate"),
        (rt.FAIL_CLASSIFIER_CHAT, "judged"),
        (rt.FAIL_CLASSIFIER_ERROR, "broke"),
    ):
        body = (await client.get(
            f"/api/activity/routing?fail_open_reason={reason}"
        )).json()
        assert [row["message"] for row in body] == [expected]


async def test_filters_by_outcome_label_and_session(client):
    await _seed(client, [
        {"session_id": "s1", "message": "a", "outcome": rt.OUTCOME_CHAT},
        {"session_id": "s2", "message": "b", "outcome": rt.OUTCOME_TASK_BACKGROUND,
         "label": "TASK"},
        {"session_id": "s2", "message": "c", "outcome": rt.OUTCOME_TASK_BACKGROUND,
         "label": "BROWSE"},
    ])

    by_outcome = (await client.get(
        f"/api/activity/routing?outcome={rt.OUTCOME_TASK_BACKGROUND}"
    )).json()
    assert {row["message"] for row in by_outcome} == {"b", "c"}

    by_label = (await client.get("/api/activity/routing?label=browse")).json()
    assert [row["message"] for row in by_label] == ["c"], "label filter is case-insensitive"

    by_session = (await client.get("/api/activity/routing?session_id=s1")).json()
    assert [row["message"] for row in by_session] == ["a"]


async def test_limit_is_bounded(client):
    assert (await client.get("/api/activity/routing?limit=0")).status_code == 422
    assert (await client.get("/api/activity/routing?limit=99999")).status_code == 422


async def test_an_ordinary_session_lookup_still_works(client):
    """The sibling route must be unaffected by the one declared above it."""
    r = await client.get("/api/activity/some-session-id")
    assert r.status_code == 200
    assert r.json() == []

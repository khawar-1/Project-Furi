"""
2026-07-13 — the "silent evening" incident class.

The production DB was missing messages.embedded_at (migration never applied):
every Message INSERT failed "non-critically", the failed flush poisoned the
request session, and the next write on it (start_task) died — "I couldn't
start that as a background task". Chat history had been silently lost for a
day. Three defenses, each tested here:
1. persist_message_best_effort ROLLS BACK on failure — the session stays
   usable for whatever follows.
2. verify_schema() names any ORM column missing from the live DB (the drift
   alarm that makes the silent-loss class impossible).
3. The jarvis_test incident guards: create-target steps are excluded from
   the search-for-it recovery, and a parent-that-is-a-FILE is named clearly.
"""
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.planner import _missing_target, _nonexistent_path_error
from app.agents.schemas import PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.db.database import Base
from app.db.migrate import verify_schema
from app.db.models import Message, Task
from app.db.persist import persist_message_best_effort


@pytest.fixture
async def engine_and_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield engine, session
    await engine.dispose()


# ------------------------------------------------- persist: rollback semantics

async def test_failed_persist_rolls_back_and_session_stays_usable(engine_and_session):
    """The regression: a failed Message write must not poison the session —
    the very next write (here a Task row, as in start_task) must succeed."""
    engine, session = engine_and_session

    # Sabotage exactly one insert: drop the messages table out from under it
    # (the live incident was a missing column; any failed flush poisons the
    # same way).
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE messages RENAME TO messages_gone"))

    msg = await persist_message_best_effort(
        session, "s-poison", "user", "this write must fail",
    )
    assert msg is None  # failed, swallowed, logged

    # Restore the table and prove the session was NOT left broken:
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE messages_gone RENAME TO messages"))

    task = Task(goal="the write after the failure", session_id="s-poison")
    session.add(task)
    await session.commit()  # would raise PendingRollbackError before the fix

    rows = (await session.execute(select(Task))).scalars().all()
    assert len(rows) == 1

    # And a normal persist works again on the same session.
    msg = await persist_message_best_effort(session, "s-poison", "user", "hello")
    assert msg is not None
    assert msg.content == "hello"


async def test_successful_persist_returns_the_row(engine_and_session):
    _, session = engine_and_session
    msg = await persist_message_best_effort(
        session, "s-ok", "assistant", "answer", model="m1",
    )
    assert msg is not None
    stored = (await session.execute(select(Message))).scalars().one()
    assert stored.id == msg.id
    assert stored.model == "m1"


# ------------------------------------------------------- the drift alarm

async def test_verify_schema_clean_database_reports_nothing(engine_and_session):
    engine, _ = engine_and_session
    assert await verify_schema(engine) == []


async def test_verify_schema_names_a_missing_column(tmp_path):
    """The exact live drift: messages exists but without embedded_at."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drift.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE messages DROP COLUMN embedded_at"))

    missing = await verify_schema(engine)
    assert "messages.embedded_at" in missing
    await engine.dispose()


async def test_verify_schema_names_a_missing_table(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notable.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE routines"))

    missing = await verify_schema(engine)
    assert "routines (entire table)" in missing
    await engine.dispose()


# --------------------------------------- jarvis_test incident: planner guards

def _failed_step(tool: str, error: str, **params) -> PlanStep:
    return PlanStep(
        description="a step", tool=tool, parameters=params,
        permission_level=PermissionLevel.WRITE, requires_approval=True,
        status=StepStatus.FAILED,
        result=ToolResult(
            success=False, output=None, error=error,
            permission_level=PermissionLevel.WRITE,
        ),
    )


def test_missing_target_never_fires_for_create_steps():
    """A create step's target doesn't exist BY DESIGN — 'search for it' is
    never the recovery (live 2026-07-12: after notes.txt failed to create
    inside a fake folder, the replan SEARCHED for notes.txt)."""
    step = _failed_step(
        "create_file",
        "'C:\\Users\\DELL\\Desktop\\jarvis_test' is a FILE, not a folder — "
        "nothing can be created inside it. Create a real folder with create_folder.",
        path="C:\\Users\\DELL\\Desktop\\jarvis_test\\notes.txt", content="test note",
    )
    assert _missing_target(step) is None

    folder_step = _failed_step(
        "create_folder", "'X' does not exist", path="C:\\nope\\newdir",
    )
    assert _missing_target(folder_step) is None


def test_missing_target_still_fires_for_reads_and_moves():
    step = _failed_step(
        "move_file", "Source not found: 'C:\\stuff\\report.pdf'",
        source="C:\\stuff\\report.pdf", destination="C:\\elsewhere",
    )
    assert _missing_target(step) == ("C:\\stuff\\report.pdf", "report.pdf")


def test_preflight_guard_names_a_file_posing_as_a_folder(tmp_path):
    """The pre-flight parent check used to accept ANY existing parent — the
    0-byte create_file posing as jarvis_test passed it, and the failure only
    surfaced tool-side. Now the guard says what's wrong before approval."""
    imposter = tmp_path / "jarvis_test"
    imposter.write_text("")
    error = _nonexistent_path_error(
        "create_file", {"path": str(imposter / "notes.txt")},
    )
    assert error is not None
    assert "FILE, not a folder" in error
    assert "create_folder" in error


def test_preflight_guard_still_accepts_a_real_folder(tmp_path):
    real = tmp_path / "real_folder"
    real.mkdir()
    assert _nonexistent_path_error(
        "create_file", {"path": str(real / "notes.txt")},
    ) is None

"""
Phase 5 Part 2 — /api/contacts HTTP behavior for email + birthday.

Exercised over real HTTP (httpx ASGITransport, the test_reminder_router.py
pattern): manual edits get explicit 400s where the extractor gets silent
drops, POST persists birthday (regression — create_contact_manual used to
ignore it), values are stored in canonical form, PUT can clear a field and
never counts as an interaction.
"""
import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.dependencies import get_db, get_qdrant
from app.db.database import Base
from main import app


@pytest_asyncio.fixture
async def client(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("contacts-api-db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_dir / 'contacts-api-test.db'}")
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
    app.dependency_overrides[get_qdrant] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def create(client, **payload) -> dict:
    response = await client.post("/api/contacts", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ================================================================== create
async def test_create_persists_birthday(client):
    """Regression: create_contact_manual silently dropped birthday."""
    created = await create(client, name="Jamil Ali", birthday="03-04")
    assert created["birthday"] == "03-04"

    fetched = (await client.get(f"/api/contacts/{created['id']}")).json()
    assert fetched["birthday"] == "03-04"


async def test_create_stores_canonical_forms(client):
    created = await create(
        client, name="Jamil Ali", email="Jamil@Example.COM", birthday="March 4, 1990"
    )
    assert created["email"] == "Jamil@example.com"
    assert created["birthday"] == "1990-03-04"


async def test_create_rejects_invalid_email(client):
    response = await client.post(
        "/api/contacts", json={"name": "Jamil Ali", "email": "not-an-email"}
    )
    assert response.status_code == 400
    assert "Invalid email address" in response.json()["detail"]


async def test_create_rejects_invalid_birthday(client):
    response = await client.post(
        "/api/contacts", json={"name": "Jamil Ali", "birthday": "02-30"}
    )
    assert response.status_code == 400
    assert "Invalid birthday" in response.json()["detail"]


async def test_create_with_empty_strings_is_just_not_provided(client):
    created = await create(client, name="Jamil Ali", email="", birthday="")
    assert created["email"] is None
    assert created["birthday"] is None


# ================================================================== update
async def test_put_updates_email_and_birthday(client):
    created = await create(client, name="Jamil Ali")
    response = await client.put(
        f"/api/contacts/{created['id']}",
        json={"email": "jamil@example.com", "birthday": "March 4"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "jamil@example.com"
    assert body["birthday"] == "03-04"


async def test_put_rejects_invalid_values_without_saving(client):
    created = await create(client, name="Jamil Ali", email="jamil@example.com")

    response = await client.put(
        f"/api/contacts/{created['id']}", json={"email": "broken@"}
    )
    assert response.status_code == 400

    response = await client.put(
        f"/api/contacts/{created['id']}", json={"birthday": "13-04"}
    )
    assert response.status_code == 400

    fetched = (await client.get(f"/api/contacts/{created['id']}")).json()
    assert fetched["email"] == "jamil@example.com"  # untouched
    assert fetched["birthday"] is None


async def test_put_empty_string_clears_field(client):
    """A human clearing a typo'd field must actually clear it."""
    created = await create(
        client, name="Jamil Ali", email="jamil@example.com", birthday="03-04"
    )
    response = await client.put(
        f"/api/contacts/{created['id']}", json={"email": "", "birthday": ""}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] is None
    assert body["birthday"] is None


async def test_put_does_not_bump_interaction_count(client):
    """Correcting a field is not an interaction with the person."""
    created = await create(client, name="Jamil Ali")
    assert created["interaction_count"] == 0
    response = await client.put(
        f"/api/contacts/{created['id']}", json={"email": "jamil@example.com"}
    )
    assert response.json()["interaction_count"] == 0


async def test_put_unknown_contact_is_404(client):
    response = await client.put(
        "/api/contacts/no-such-id", json={"email": "jamil@example.com"}
    )
    assert response.status_code == 404

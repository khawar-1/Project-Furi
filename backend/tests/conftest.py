"""Shared fixtures for the Jarvis OS backend test suite."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.database import Base
from app.memory.engine import MemoryEngine
from app.memory.conversation_state import CONVERSATION_SESSIONS


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite session for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
def engine(db_session):
    """MemoryEngine with no Qdrant (SQLite-only mode)."""
    return MemoryEngine(db=db_session, qdrant=None)


@pytest.fixture
def session_id():
    """Fresh conversation session id, cleaned up after the test."""
    sid = str(uuid.uuid4())
    yield sid
    CONVERSATION_SESSIONS.pop(sid, None)


@pytest.fixture(autouse=True)
def _hermetic_auth(tmp_path_factory):
    """The API auth gate (app/core/auth.py) guards every route on the real
    app that API tests import from main. Tests must never present tokens or
    touch the real ~/.jarvis/auth_token: auth is disabled and the token path
    repointed at an empty scratch dir. test_auth.py re-enables it explicitly
    to cover enforcement."""
    from app.core import auth

    enabled, token_path = auth.ENABLED, auth.TOKEN_PATH
    auth.ENABLED = False
    auth.TOKEN_PATH = tmp_path_factory.mktemp("auth") / "auth_token"
    auth.reset_auth()
    yield
    auth.ENABLED, auth.TOKEN_PATH = enabled, token_path
    auth.reset_auth()


@pytest.fixture(autouse=True)
def _hermetic_question_gate(tmp_path_factory):
    """The planner's question self-resolution gate verifies questions with a
    REAL filesystem search that defaults to the user's home directory. Tests
    must never walk the real home: every test gets an empty scratch root
    (gate finds nothing → questions pass through unchanged). Tests exercising
    the gate point SEARCH_ROOTS at a populated tmp dir themselves."""
    from app.agents import question_gate

    original = question_gate.SEARCH_ROOTS
    question_gate.SEARCH_ROOTS = [str(tmp_path_factory.mktemp("question-gate"))]
    yield
    question_gate.SEARCH_ROOTS = original


@pytest.fixture(autouse=True)
def _hermetic_folder_resolver(tmp_path_factory):
    """The same-named-folder guard probes the machine's home + drive roots for
    duplicate well-known folders. Tests must never touch real drives: every
    test gets an empty scratch home and no drives (guard finds nothing → no
    action). Tests exercising the guard set HOME/DRIVES themselves."""
    from app.agents import folder_resolver

    home, drives = folder_resolver.HOME, folder_resolver.DRIVES
    folder_resolver.HOME = tmp_path_factory.mktemp("folder-resolver-home")
    folder_resolver.DRIVES = []
    yield
    folder_resolver.HOME, folder_resolver.DRIVES = home, drives


@pytest.fixture(autouse=True)
def _hermetic_voice_stt():
    """voice_stt's default factory downloads a ~500MB whisper model on first
    load. Tests must never trigger that: every test starts with pristine
    module state and a factory that refuses outright. Voice tests swap in
    their own fakes on top."""
    from app.core import voice_stt

    def _refuse(model_name: str):
        raise RuntimeError("test tried to load a real STT model")

    voice_stt.reset_stt()
    voice_stt.STT_MODEL_FACTORY = _refuse
    yield
    voice_stt.reset_stt()


@pytest.fixture(autouse=True)
def _hermetic_voice_tts():
    """voice_tts's default factory imports onnxruntime + downloads the Kokoro
    model on first load. Tests must never trigger that: every test starts with
    pristine module state and a factory that refuses outright. Voice tests swap
    in their own fakes on top. (The factory takes no arguments.)"""
    from app.core import voice_tts

    def _refuse():
        raise RuntimeError("test tried to load a real TTS engine")

    voice_tts.reset_tts()
    voice_tts.TTS_ENGINE_FACTORY = _refuse
    yield
    voice_tts.reset_tts()


@pytest.fixture(autouse=True)
def _hermetic_screen_ocr():
    """screen_ocr's default factory imports rapidocr_onnxruntime + loads OCR
    models. Tests must never trigger that: every test starts with a cleared
    engine and a factory that refuses outright. OCR tests swap in their own
    fake (returning canned text) on top."""
    from app.core import screen_ocr

    def _refuse():
        raise RuntimeError("test tried to load a real OCR engine")

    screen_ocr.reset_screen_ocr()
    screen_ocr.OCR_ENGINE_FACTORY = _refuse
    yield
    screen_ocr.reset_screen_ocr()


@pytest.fixture(autouse=True)
def _hermetic_context_store():
    """The Phase 8 world model holds sensed device/OCR state in module globals
    (retention=none). Reset it around every test so one test's signal never
    leaks into another's world model."""
    from app.core import context_store

    context_store.reset_context_store()
    yield
    context_store.reset_context_store()


@pytest.fixture(autouse=True)
def _hermetic_google_auth(tmp_path_factory):
    """Google auth defaults its token file to the real ~/.jarvis directory.
    Tests must never read/write it (or hit Google): every test gets a manager
    pointed at an empty scratch token path — status is "not connected" unless
    a test writes its own fake token there."""
    from app.integrations import google_auth

    original = google_auth.AUTH_MANAGER
    google_auth.AUTH_MANAGER = google_auth.GoogleAuthManager(
        token_path=tmp_path_factory.mktemp("google-auth") / "google_token.json"
    )
    yield
    google_auth.AUTH_MANAGER = original

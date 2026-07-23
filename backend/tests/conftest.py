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
def _hermetic_reading_enumerator():
    """The planner asks reading_enumerator whether a goal is ambiguous whenever
    a draft carries a single-query web_search, and that costs a provider call.

    Unlike _hermetic_browser below, this is NOT about the network — the
    enumerator has no default factory and only ever uses the provider it is
    handed, which in tests is always a fake. The hazard is ISOLATION: the
    planner's FakeProvider is a scripted queue, so an unrelated enumeration call
    silently eats the next scripted response and breaks the call-count
    assertions that prove things like "escalation costs no LLM call" (it broke
    exactly those three tests in test_evidence_resolver.py when this shipped).
    Default to "no readings" — the goal is unambiguous, nothing is widened, no
    call is made. Fan-out tests patch this with their own enumeration."""
    from app.agents import reading_enumerator

    real = reading_enumerator.enumerate_readings
    real_rank = reading_enumerator.rank_readings

    async def _no_readings(goal, provider, now=None):
        return []

    async def _no_rank(goal, readings, rows, provider, now=None):
        return ""

    # Both halves are stubbed: the ranking call fires from _execute_node the
    # moment a fanned-out search completes, so a test that patches only the
    # enumeration would still spend a scripted response on the ranking.
    reading_enumerator.enumerate_readings = _no_readings
    reading_enumerator.rank_readings = _no_rank
    yield
    reading_enumerator.enumerate_readings = real
    reading_enumerator.rank_readings = real_rank


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
def _hermetic_secrets():
    """secrets_store's default backend is the real Windows DPAPI (Win32 API +
    a per-user key). Tests must never depend on the OS crypto or platform: swap
    in a reversible, always-available fake so autofill secret round-trips are
    deterministic everywhere. The fake still produces the dpapi: prefix, so
    'is it encrypted at rest?' assertions hold; it is trivially reversible, so
    decrypt recovers the plaintext."""
    from app.core import secrets_store

    class _ReversibleBackend:
        # A visible, reversible transform (byte-invert) standing in for DPAPI —
        # NOT real crypto, only enough to prove the stored form differs from the
        # plaintext and that decrypt undoes it.
        def available(self) -> bool:
            return True

        def protect(self, data: bytes) -> bytes:
            return bytes(b ^ 0xFF for b in data)

        def unprotect(self, data: bytes) -> bytes:
            return bytes(b ^ 0xFF for b in data)

        def __repr__(self) -> str:  # pragma: no cover - debug aid
            return "<ReversibleBackend fake>"

    original = secrets_store.CRYPTO_BACKEND
    secrets_store.CRYPTO_BACKEND = _ReversibleBackend()
    secrets_store._warned = False
    yield
    secrets_store.CRYPTO_BACKEND = original
    secrets_store._warned = False


@pytest.fixture(autouse=True)
def _hermetic_browser():
    """browser_tools' default paths go to the REAL internet (Tavily, then the
    DuckDuckGo scrapers, then any URL a plan names). Both factories default to
    None module-wide, so nothing but a test's own patching stood between the
    suite and the network.

    That was survivable while only test_browser_tools.py exercised these paths.
    It stopped being survivable when the planner gained the power to SPLICE a
    read_webpage step of its own accord (evidence_resolver, 2026-07-16): any
    planner test whose fake web_search returns thin rows would now try a real
    fetch. Refuse outright so such a test fails LOUDLY instead of flaking on
    someone's network. Web tests swap in their own fakes on top."""
    from app.tools import browser_tools

    def _refuse_fetch(url: str):
        raise RuntimeError(f"test tried to fetch a real URL: {url}")

    def _refuse_search(query: str, max_results: int):
        raise RuntimeError(f"test tried a real web search: {query!r}")

    browser_tools.HTTP_FETCH_FACTORY = _refuse_fetch
    browser_tools.SEARCH_PROVIDER_FACTORY = _refuse_search
    yield
    browser_tools.HTTP_FETCH_FACTORY = None
    browser_tools.SEARCH_PROVIDER_FACTORY = None


@pytest.fixture(autouse=True)
def _hermetic_browser_session():
    """browser_session's default factory LAUNCHES A REAL CHROMIUM against the
    user's own ~/.jarvis/browser profile — a visible window, on their machine,
    holding whatever they are logged into. That is categorically worse than the
    stray fetch _hermetic_browser exists to stop, and the same reasoning applies
    with more force: the planner already splices steps of its own accord, so it
    is not enough that no test calls browse_page on purpose.

    Refuse outright. Browser tests swap in their own fake page/context on top.
    The host cache is cleared too — it memoizes DNS answers, so a cached verdict
    could otherwise leak between tests.

    The 15.3 vision seam gets the same belt: VISION_PROVIDER_FACTORY is pointed at
    a refuser so no test ever builds a real Gemini vision client. It only fires
    when vision is ENABLED (build_vision_provider short-circuits to None when
    disabled), so the default disabled suite is untouched; a test that enables
    vision through the tool path gets a caught refusal → None (DOM-only), never a
    real client. Vision-loop tests pass their own fake `vision` straight to
    run_browse and never touch this seam."""
    from app.browser import registry as browser_registry
    from app.core import browser_session
    from app.providers import vision

    def _refuse():
        raise RuntimeError("test tried to launch a real browser")

    def _refuse_vision(_config):
        raise RuntimeError("test tried to build a real vision provider")

    browser_session.BROWSER_FACTORY = _refuse
    vision.VISION_PROVIDER_FACTORY = _refuse_vision
    # The clean hand-off window launches a REAL Chrome subprocess in production
    # (2026-07-19). Its launcher seam stays None here, and _clean_login_enabled()
    # gates the clean path on BROWSER_FACTORY being None — which the refuser above
    # is not — so the suite stays on the Playwright fake path and never spawns a
    # process. A test that exercises the clean path injects CLEAN_BROWSER_LAUNCHER.
    browser_session.CLEAN_BROWSER_LAUNCHER = None
    # reclaim_orphaned_profile() enumerates the machine's processes and kills the
    # ones on the ~/.jarvis/browser profile (2026-07-20). Point the reaper seam at
    # a no-op that finds NOTHING, so a browse/startup/shutdown path that reclaims
    # never touches a real process. A test exercising reclaim injects its own.
    browser_session._PROFILE_REAPER = lambda _marker: []
    # The shared Playwright driver (started once, reused across browses) must never
    # spawn a real Node driver in the suite, and a driver cached in one test must
    # not leak into the next. Refuse the starter seam and clear the singleton (the
    # BROWSER_FACTORY refuser + reset_host_cache hygiene).
    async def _refuse_driver():
        raise RuntimeError("test tried to start a real Playwright driver")
    browser_session._PLAYWRIGHT_STARTER = _refuse_driver
    browser_session._shared_playwright = None
    browser_session.reset_host_cache()
    # The held-session registries keep a live session across tool calls (media,
    # result window, commit, challenge, discovery). A fake session left in any
    # slot would leak into the next test, so drop them all — inert here (tests
    # only ever register fakes), the reset_host_cache hygiene. One call covers
    # every slot by construction.
    browser_registry.reset_for_tests()
    browser_session._login_browser = None
    browser_session._clean_login_proc = None
    yield
    browser_session.BROWSER_FACTORY = None
    vision.VISION_PROVIDER_FACTORY = None
    browser_session.CLEAN_BROWSER_LAUNCHER = None
    browser_session._PROFILE_REAPER = None
    browser_session._PLAYWRIGHT_STARTER = None
    browser_session._shared_playwright = None
    browser_session.reset_host_cache()
    browser_registry.reset_for_tests()
    browser_session._login_browser = None
    browser_session._clean_login_proc = None


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

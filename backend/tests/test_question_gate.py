"""
Question self-resolution gate (planner hardening, 2026-07-10).
Live incident: "list all the files in the desktop and delete all the files
from phase3test and after doing all this remind me" — the draft paused on
"What is the full path of the phase3test folder?" with no options, making
the user do Jarvis's research. The gate answers such questions with a REAL
search before they ever reach the user: found on attempt 1 → the question is
rejected and the retry feedback carries the verified paths; found on attempt
2 → the question goes through with the paths as verified clickable options;
found nothing → the honest question passes unchanged.
"""
import json
from typing import AsyncIterator, List, Optional

import pytest

import app.tools  # noqa: F401 — registers the real file/terminal/memory tools
from app.agents import question_gate
from app.agents.planner import AgentPlanner, MAX_REPLANS
from app.agents.question_gate import extract_shared_names, locate_name
from app.agents.schemas import PlanQuestion, PlanStatus, StepStatus
from app.providers.base import (
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)

GOAL = (
    "list all the files in the desktop and delete all the files from "
    "phase3test and after doing all this remind me"
)
QUESTION = "What is the full path of the phase3test folder?"


class RecordingProvider(LLMProvider):
    """Scripted responses; records the FULL message list of every call so
    tests can assert on structural retry feedback."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.message_log: List[List[str]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        self.calls += 1
        self.message_log.append([m.content for m in messages])
        if not self._responses:
            raise AssertionError(f"RecordingProvider exhausted after {self.calls - 1} responses")
        return LLMResponse(
            content=self._responses.pop(0), model="fake-model", provider="fake",
        )

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        yield ""

    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 384, model="fake", provider="fake")


def step(description: str, tool: str, **parameters) -> dict:
    return {"description": description, "tool": tool, "parameters": parameters}


def plan_json(steps: list, question: Optional[dict] = None) -> str:
    return json.dumps(
        {"steps": steps, "unachievable_reason": None, "question": question}
    )


def question_json(text: str, options: Optional[list] = None) -> str:
    return plan_json([], question={"text": text, "options": options or []})


@pytest.fixture
def gate_root(tmp_path):
    """Point the gate's verification search at a per-test tmp root."""
    original = question_gate.SEARCH_ROOTS
    question_gate.SEARCH_ROOTS = [str(tmp_path)]
    yield tmp_path
    question_gate.SEARCH_ROOTS = original


# ------------------------------------------------------------ name extraction

def test_extract_shared_names_grounds_in_the_goal():
    # "phase3test" is the one name the question and the goal share; the
    # filler ("the", "files", "folder", "full", "path") never becomes a name.
    assert extract_shared_names(GOAL, QUESTION) == ["phase3test"]


def test_extract_ignores_generic_words_entirely():
    assert extract_shared_names(GOAL, "Which folder do you mean?") == []
    assert extract_shared_names("delete the thing", "What is the full path?") == []


def test_extract_never_takes_names_the_user_did_not_say():
    # The model cannot steer the gate toward things absent from the goal.
    assert extract_shared_names(GOAL, "Where is the secrets folder?") == []


def test_extract_quoted_multiword_span():
    names = extract_shared_names(
        "delete the project notes folder from my pc",
        "Where is 'project notes' located?",
    )
    assert names == ["project notes"]


def test_extract_skips_concrete_paths():
    # A drive-rooted path in the question is not a name to search for.
    assert extract_shared_names(
        r"delete C:\data\x", r"Should I delete C:\data\x?"
    ) == []


# ------------------------------------------------------------------- locating

async def test_locate_name_prefers_exact_matches(db_session, gate_root):
    target = gate_root / "phase3test"
    target.mkdir()
    (gate_root / "phase3test_old.txt").write_text("decoy")

    paths = await locate_name("phase3test", db_session, None)
    assert paths == [str(target)]  # exact folder name beats the substring hit


async def test_locate_name_returns_empty_when_nothing_matches(db_session, gate_root):
    assert await locate_name("phase3test", db_session, None) == []


# ------------------------------------------------------------------- verdicts

async def test_question_with_several_options_is_never_touched(db_session, gate_root):
    (gate_root / "phase3test").mkdir()
    q = PlanQuestion(text=QUESTION, options=["2026-03-04", "2026-04-03"])
    resolution = await question_gate.self_resolve(q, GOAL, 1, db_session, None, {})
    assert resolution.action == "pass"


async def test_single_option_confirmed_by_search_answers_itself(db_session, gate_root):
    """Live run 2026-07-10: the draft guessed two paths, option verification
    dropped the invented one, and 'what is the full path?' reached the user
    carrying the single surviving guess. One option + a search confirming it
    is the ONLY match = the question answers itself."""
    target = gate_root / "phase3test"
    target.mkdir()
    q = PlanQuestion(text=QUESTION, options=[str(target)])
    resolution = await question_gate.self_resolve(q, GOAL, 1, db_session, None, {})
    assert resolution.action == "reject"
    assert str(target) in resolution.feedback
    assert "answers itself" in resolution.feedback


async def test_single_option_passes_when_search_finds_several(db_session, gate_root):
    """The model offering ONE of several real matches under-offers — but
    auto-answering could pick the wrong one, so the question still passes."""
    (gate_root / "phase3test").mkdir()
    other = gate_root / "old" / "phase3test"
    other.mkdir(parents=True)
    q = PlanQuestion(text=QUESTION, options=[str(gate_root / "phase3test")])
    resolution = await question_gate.self_resolve(q, GOAL, 1, db_session, None, {})
    assert resolution.action == "pass"


async def test_single_unverifiable_option_passes(db_session, gate_root):
    # A lone plain-text option ("March 4") is a real question, not a path —
    # nothing to confirm, so it reaches the user untouched.
    (gate_root / "phase3test").mkdir()
    q = PlanQuestion(text=QUESTION, options=["March 4"])
    resolution = await question_gate.self_resolve(q, GOAL, 1, db_session, None, {})
    assert resolution.action == "pass"


async def test_single_option_passes_on_the_second_attempt(db_session, gate_root):
    target = gate_root / "phase3test"
    target.mkdir()
    q = PlanQuestion(text=QUESTION, options=[str(target)])
    resolution = await question_gate.self_resolve(q, GOAL, 2, db_session, None, {})
    assert resolution.action == "pass"  # bounded: never an endless reject loop


async def test_unfindable_question_passes_unchanged(db_session, gate_root):
    q = PlanQuestion(text=QUESTION, options=[])
    resolution = await question_gate.self_resolve(q, GOAL, 1, db_session, None, {})
    assert resolution.action == "pass"  # honest question — nothing found


async def test_first_attempt_rejects_with_the_verified_path(db_session, gate_root):
    target = gate_root / "phase3test"
    target.mkdir()
    q = PlanQuestion(text=QUESTION, options=[])
    cache: dict = {}
    resolution = await question_gate.self_resolve(q, GOAL, 1, db_session, None, cache)
    assert resolution.action == "reject"
    assert str(target) in resolution.feedback
    assert "verified to exist" in resolution.feedback
    assert cache["phase3test"] == [str(target)]  # walk result cached for the retry


async def test_second_attempt_attaches_verified_options_from_cache(db_session):
    # attempt 2 must reuse the cached walk — prove it by faking the cache.
    q = PlanQuestion(text=QUESTION, options=[])
    cached = {"phase3test": [r"C:\from\the\cache\phase3test"]}
    resolution = await question_gate.self_resolve(q, GOAL, 2, db_session, None, cached)
    assert resolution.action == "answer"
    assert resolution.options == [r"C:\from\the\cache\phase3test"]


# --------------------------------------------------- end-to-end planner flows

async def test_draft_location_question_is_self_answered(db_session, gate_root):
    """THE live incident, fixed end to end: the draft asks where phase3test
    is; the gate searches, finds it, rejects the question, and hands the
    model the real path — the user never sees a question."""
    target = gate_root / "phase3test"
    target.mkdir()
    (target / "a.txt").write_text("x")
    # Criterion-less but folder-scoped: valid, and faithful to the "all the
    # files" goal (a .txt filter would trip the scope guard — rule 13).
    search = [step("Find the files in phase3test", "search_files",
                   directory=str(target))]
    provider = RecordingProvider([
        question_json(QUESTION),   # lazy draft: asks instead of searching
        plan_json(search),         # retry, after being handed the real path
        plan_json(search),         # reflect
    ])

    plan = await AgentPlanner(db_session, provider).start(GOAL)

    assert plan.status == PlanStatus.COMPLETED
    assert plan.question is None
    assert plan.steps[0].status == StepStatus.COMPLETED
    # The structural rejection carried the VERIFIED path to the model
    retry_feedback = provider.message_log[1][-1]
    assert str(target) in retry_feedback
    assert "NEVER ask the user where" in retry_feedback


async def test_reasked_question_carries_verified_options(db_session, gate_root):
    """A model that insists on asking still cannot make the user do the
    research: the question reaches them WITH the found paths as options."""
    target = gate_root / "phase3test"
    target.mkdir()
    provider = RecordingProvider([question_json(QUESTION), question_json(QUESTION)])

    plan = await AgentPlanner(db_session, provider).start(GOAL)

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.options == [str(target)]
    assert provider.calls == 2


async def test_question_about_a_truly_missing_name_still_reaches_the_user(
    db_session, gate_root
):
    """Ask-don't-guess survives: when the search really finds nothing, the
    question passes through options-free on the FIRST attempt."""
    provider = RecordingProvider([question_json(QUESTION)])

    plan = await AgentPlanner(db_session, provider).start(GOAL)

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.text == QUESTION
    assert plan.question.options == []
    assert provider.calls == 1


async def test_fallback_question_gains_verified_options(db_session, gate_root):
    """The ask-not-fail 'where is it?' question also self-resolves first: a
    plan dead-ending on a wrong path offers the REAL matches it found."""
    target = gate_root / "desktop" / "phase3test"
    target.mkdir(parents=True)
    bad_dir = str(gate_root / "missing" / "phase3test")
    bad = [step("Search", "search_files", directory=bad_dir, file_type=".txt")]
    provider = RecordingProvider([plan_json(bad)] * (2 + MAX_REPLANS))

    plan = await AgentPlanner(db_session, provider).start(
        "find the txt files in phase3test"
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question.options == [str(target)]
    assert "found 1 match(es) for 'phase3test'" in plan.question.text

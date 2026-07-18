"""
Phase 14.5 — COMMIT mode (submit ONE approved web form, and nothing else).

Two halves are pinned here:

  - the PLANNER flow: a browse_commit step discovers the form (READ), the plan
    PAUSES for signature approval with the code-read form contract rendered into
    action_detail AND baked into the signature, and approving runs exactly one
    submit; an ungrounded site is refused before discovery, a sign-in wall pauses
    for manual login;
  - the SUBMIT orchestration (browser_commit.perform): it verifies the held form
    is unchanged, arms the interceptor, submits, and fails closed on a mismatch
    or an expired session.

The interceptor's one-shot arming — the security core — is pinned in
test_browser_session.py.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import browser_commit, browser_loop, planner as planner_mod
from app.agents.planner import AgentPlanner
from app.agents.schemas import PlanStatus, StepStatus
from app.core import browser_runtime, browser_session, dom_observe
from app.core.base_tool import PermissionLevel, ToolResult
from app.core.browser_session import BrowserSession, _commit_fingerprint

from tests.test_agent_planner import FakeProvider, plan_json, step
from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page


_STATE = {
    "url": "https://example.com/comment",
    "method": "POST",
    "fields": [{"name": "comment", "value": "hello world"}],
}


def _commit_step() -> dict:
    return step(
        "Post the comment",
        "browse_commit",
        goal="post 'hello world' as a comment on example.com",
        start_url="https://example.com/post",
        allowed_origins=["example.com"],
    )


def _record_exec(calls: list):
    async def fake_exec(tool, params, db, session_id=None, approved=False):
        calls.append({"tool": tool, "approved": approved, "params": dict(params)})
        return ToolResult(
            success=True,
            output={"submitted": True, "message": "Submitted the approved form."},
            permission_level=PermissionLevel.DESTRUCTIVE,
        )

    return fake_exec


# --------------------------------------------------------------- planner flow
async def test_a_browse_commit_step_discovers_the_form_then_pauses_for_approval(
    db_session, monkeypatch
):
    """DISCOVER runs (READ), the code-read contract lands in action_detail AND the
    signature, and NOTHING executes — the plan waits for approval."""

    async def fake_discover(params, session_id=None):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-commit").start(
        "post 'hello world' as a comment on example.com"
    )

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    s = plan.steps[0]
    # The card shows the exact contract — method, URL, and the real field value.
    assert s.action_detail is not None
    assert "POST https://example.com/comment" in s.action_detail
    assert "hello world" in s.action_detail
    # The approval binds to the discovered values (they are in the signature).
    assert browser_commit.COMMIT_PARAM in s.parameters
    assert "hello world" in s.signature()
    # Nothing ran — no submit before approval.
    assert calls == []


async def test_approving_the_form_runs_exactly_one_submit(db_session, monkeypatch):
    """On approval the same step re-enters — discovery is NOT repeated — and runs
    the one submit, approved, carrying the exact approved field values."""

    async def fake_discover(params, session_id=None):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-commit2")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    assert plan.status == PlanStatus.AWAITING_APPROVAL

    resumed = await planner.resume(plan, approved=True)

    assert resumed.status == PlanStatus.COMPLETED
    assert len(calls) == 1
    assert calls[0]["tool"] == "browse_commit"
    assert calls[0]["approved"] is True
    approved = calls[0]["params"][browser_commit.COMMIT_PARAM]
    assert approved["fields"][0]["value"] == "hello world"
    assert approved["url"] == "https://example.com/comment"


async def test_cancelling_the_approval_submits_nothing(db_session, monkeypatch):
    async def fake_discover(params, session_id=None):
        return browser_commit.CommitDiscovery(state=dict(_STATE))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    provider = FakeProvider([plan_json([_commit_step()])])
    planner = AgentPlanner(db_session, provider, session_id="s-commit3")
    plan = await planner.start("post 'hello world' as a comment on example.com")
    resumed = await planner.resume(plan, approved=False)

    assert resumed.status == PlanStatus.CANCELLED
    assert calls == []  # a declined submit runs nothing


async def test_a_browse_commit_to_an_unnamed_site_is_refused_before_discovery(
    db_session, monkeypatch
):
    """Origin grounding applies to browse_commit with a write behind it: a step
    targeting a site the user never named is rejected by the planner, and
    discovery never runs."""

    async def boom(params, session_id=None):  # pragma: no cover - must not run
        raise AssertionError("discovery ran for an ungrounded browse_commit step")

    monkeypatch.setattr(browser_commit, "discover", boom)

    bad = step(
        "Post it",
        "browse_commit",
        goal="post a comment on example.com",
        start_url="https://attacker.com/x",
        allowed_origins=["attacker.com"],
    )
    provider = FakeProvider([plan_json([bad]), plan_json([bad])])
    plan = await AgentPlanner(db_session, provider, session_id="s-bad").start(
        "post a comment on example.com"
    )
    assert plan.status == PlanStatus.FAILED


async def test_a_sign_in_wall_during_discovery_pauses_for_manual_login(
    db_session, monkeypatch
):
    async def fake_discover(params, session_id=None):
        return browser_commit.CommitDiscovery(
            login_required=True,
            login_site="accounts.google.com",
            error="sign-in required at accounts.google.com",
        )

    async def fake_open(site):
        return True

    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(
        planner_mod.AgentPlanner, "_open_commit_login", staticmethod(fake_open)
    )

    provider = FakeProvider([plan_json([_commit_step()])])
    plan = await AgentPlanner(db_session, provider, session_id="s-login").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert "sign in" in plan.question.text.lower()


async def test_a_discovery_failure_replans_not_submits(db_session, monkeypatch):
    """A discovery that cannot reach a submittable form fails the step into the
    replan loop — it never pauses for approval on nothing."""

    async def fake_discover(params, session_id=None):
        return browser_commit.CommitDiscovery(error="I couldn't find a form to submit.")

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    # Draft the commit step; the revise round gives up honestly.
    provider = FakeProvider(
        [plan_json([_commit_step()]), plan_json([], reason="no form to submit")]
    )
    plan = await AgentPlanner(db_session, provider, session_id="s-nofrm").start(
        "post 'hello world' as a comment on example.com"
    )
    assert plan.status != PlanStatus.AWAITING_APPROVAL
    assert calls == []  # nothing was ever submitted


# ------------------------------------------------------------ tool-level gate
async def test_the_commit_tool_will_not_submit_without_an_approved_form():
    """browse_commit.execute is the SUBMIT phase only — with no approved contract
    (invoked out of sequence) it refuses, never submits on a guess."""
    from app.tools.browser_agent_tools import BrowseCommitTool

    result = await BrowseCommitTool().execute(goal="x", start_url="https://example.com")
    assert result.success is False
    assert "approved" in (result.error or "").lower()


async def test_execute_tool_refuses_browse_commit_without_approval(db_session):
    """The structural gate: browse_commit is DESTRUCTIVE, so execute_tool refuses
    it without approved=True — the submit can never run unapproved."""
    from app.tools.registry import execute_tool

    result = await execute_tool("browse_commit", {}, db_session, approved=False)
    assert result.success is False
    assert result.requires_approval is True


# ------------------------------------------------------ perform() orchestration
class StubCommitSession:
    """A held session as browser_commit.perform sees it — no real browser."""

    def __init__(self, *, verify=True, fired=True):
        self._verify = verify
        self._fired = fired
        self.armed = None
        self.submitted = False
        self.closed = False
        self.playback = False  # set iff enter_playback_mode is ever called
        self.page = object()
        self.stats = browser_session.InterceptStats()

    async def enter_playback_mode(self, *, reload=True):
        # A kept-open result window must NEVER lift interception (unlike media) —
        # this flag lets a test prove perform() leaves the window read-only.
        self.playback = True

    async def verify_commit(self, approved):
        return self._verify

    def arm_commit(self, method, url):
        self.armed = (method, url)

    async def submit_commit(self):
        self.submitted = True

    async def settle(self):
        pass

    def commit_fired(self):
        return self._fired

    async def close(self):
        self.closed = True


async def _fake_observe(page):
    return dom_observe.Observation(
        observation_id="o",
        url="https://example.com/thanks",
        title="Thanks",
        elements=[],
        element_total=0,
        page_text="Posted!",
        text_truncated=False,
    )


@pytest.fixture
def _direct_browser_runtime(monkeypatch):
    async def fake_run_browser(coro):
        return await coro  # the stubs are loop-agnostic

    monkeypatch.setattr(browser_runtime, "run_browser", fake_run_browser)
    monkeypatch.setattr(dom_observe, "observe", _fake_observe)


async def test_perform_verifies_arms_submits_and_closes(_direct_browser_runtime):
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is True
    assert stub.armed == ("POST", "https://example.com/comment")  # armed for the approved request
    assert stub.submitted is True
    assert stub.closed is True                       # one submit per session, then closed
    assert browser_session.pending_commit() is None  # registry emptied


async def test_perform_refuses_when_the_form_changed(_direct_browser_runtime):
    """Fail closed: if the held form no longer matches what was approved, nothing
    is armed and nothing is sent."""
    stub = StubCommitSession(verify=False)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is False
    assert "changed" in result["error"].lower()
    assert stub.armed is None       # never armed the interceptor
    assert stub.closed is True


async def test_perform_reports_an_expired_session(_direct_browser_runtime):
    """A restart/timeout dropped the held session — the submit says so and sends
    nothing (never invents a submission)."""
    result = await browser_commit.perform(dict(_STATE))
    assert result["submitted"] is False
    assert "expired" in result["error"].lower()


async def test_perform_reports_a_submission_that_did_not_fire(_direct_browser_runtime):
    """The form was armed and submit() called, but no matching request went out
    (a JS handler swallowed it) — honest 'not sent', never a false success."""
    stub = StubCommitSession(verify=True, fired=False)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is False
    assert "did not go through" in result["error"]
    assert stub.closed is True


async def test_perform_grounds_the_confirmation_in_the_server_response(_direct_browser_runtime):
    """14.6: the completion is grounded in what the site ACTUALLY returned (the
    observed page's visible prose), not the goal — the fix for the ungrounded
    'All done' that looked like a fabrication."""
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE))

    assert result["submitted"] is True
    assert result["response_text"] == "Posted!"   # from _fake_observe's page_text


async def test_perform_keep_open_leaves_the_window_read_only_and_registered(_direct_browser_runtime):
    """14.6: with keep_open the fired session is NOT closed — it is handed to the
    result-window registry so the user can see the response — and it stays
    READ-ONLY (enter_playback_mode is never called; the spent commit arm is what
    keeps it safe, not closing it)."""
    stub = StubCommitSession(verify=True, fired=True)
    await browser_session.hold_commit(stub, state=dict(_STATE))
    try:
        result = await browser_commit.perform(dict(_STATE), keep_open=True)

        assert result["window_open"] is True
        assert stub.closed is False             # handed off, not closed
        assert stub.playback is False           # interception never lifted — a viewer only
        active = browser_session.active_result_window()
        assert active is not None
        assert active["url"] == result["url"]
    finally:
        await browser_session.close_result_window()
    assert stub.closed is True                  # the registry closes it on demand
    assert browser_session.active_result_window() is None


async def test_perform_keep_open_still_closes_when_the_submit_did_not_fire(_direct_browser_runtime):
    """A submission that never went out must not leave a window lingering — only a
    REAL, fired submit earns the kept-open result window."""
    stub = StubCommitSession(verify=True, fired=False)
    await browser_session.hold_commit(stub, state=dict(_STATE))

    result = await browser_commit.perform(dict(_STATE), keep_open=True)

    assert result["submitted"] is False
    assert result["window_open"] is False
    assert stub.closed is True
    assert browser_session.active_result_window() is None


# ============================================================ 14.6 — file upload
# The file is attached during the READ discovery (set_input_files sends nothing),
# folded into the approved commit contract, and only LEAVES on the approved
# submit. So upload-without-approval is impossible by construction, and the
# approval binds to the exact file.

# ------------------------------------------------- BrowserSession.upload_file
class _FileHandle:
    def __init__(self, name="attachment"):
        self._name = name
        self.files = None

    async def set_input_files(self, path):
        self.files = path

    async def get_attribute(self, attr):
        return self._name if attr == "name" else None


class _FilePage:
    def __init__(self, handle):
        self.handle = handle
        self.url = "https://example.com/form"

    async def query_selector(self, selector):
        return self.handle  # dom_observe.resolve returns this handle


def _file_obs() -> dom_observe.Observation:
    return dom_observe.Observation(
        observation_id="o", url="https://example.com/form", title="",
        elements=[dom_observe.Element(index=1, role="file", name="attachment")],
        element_total=1, page_text="", text_truncated=False,
    )


async def test_upload_file_sets_a_grounded_file_and_records_it(tmp_path):
    """A safe path: set_input_files is called with the RESOLVED path and the
    attachment is recorded so it can be folded into the approved contract."""
    from app.tools.file_tools import _resolve_path

    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    handle = _FileHandle()
    session = BrowserSession(browser=object(), page=_FilePage(handle), allowlist={"example.com"})

    ok, note = await session.upload_file(_file_obs(), 1, str(f))

    resolved = str(_resolve_path(str(f)))
    assert ok is True
    assert handle.files == resolved
    assert session.uploads == [{"name": "attachment", "path": resolved}]


async def test_upload_file_refuses_an_unsafe_path_and_sets_nothing(tmp_path):
    """The defense-in-depth backstop: even though the planner already checked the
    path, the one place that touches the filesystem re-refuses a bad path — and
    set_input_files is NEVER called for it."""
    handle = _FileHandle()
    session = BrowserSession(browser=object(), page=_FilePage(handle), allowlist={"example.com"})

    ok, note = await session.upload_file(_file_obs(), 1, r"C:\definitely\missing.pdf")

    assert ok is False
    assert "does not exist" in note
    assert handle.files is None          # nothing was attached
    assert session.uploads == []


# --------------------------------------------- fingerprint / verify bind files
def test_commit_fingerprint_binds_the_attached_file():
    base = {"url": "https://x/s", "method": "POST", "fields": [{"name": "a", "value": "b"}]}
    with_file = {**base, "uploads": [{"name": "f", "path": "C:/x/resume.pdf"}]}
    other_file = {**base, "uploads": [{"name": "f", "path": "C:/x/OTHER.pdf"}]}
    assert _commit_fingerprint(with_file) == _commit_fingerprint(dict(with_file))
    assert _commit_fingerprint(with_file) != _commit_fingerprint(other_file)
    # Backwards compatible: a pre-14.6 state (no uploads key) == empty uploads.
    assert _commit_fingerprint(base) == _commit_fingerprint({**base, "uploads": []})


class _RereadPage:
    def __init__(self, form):
        self._form = form

    async def evaluate(self, js):
        return self._form


async def test_verify_commit_binds_the_file_and_fails_closed_on_a_swap():
    form = {"action": "https://example.com/upload", "method": "POST",
            "fields": [{"name": "note", "value": "hi"}], "has_password": False}
    session = BrowserSession(browser=object(), page=_RereadPage(form), allowlist={"example.com"})
    session.uploads = [{"name": "attachment", "path": "C:/x/resume.pdf"}]

    ok = {"url": "https://example.com/upload", "method": "POST",
          "fields": [{"name": "note", "value": "hi"}],
          "uploads": [{"name": "attachment", "path": "C:/x/resume.pdf"}]}
    assert await session.verify_commit(ok) is True

    swapped = {**ok, "uploads": [{"name": "attachment", "path": "C:/x/OTHER.pdf"}]}
    assert await session.verify_commit(swapped) is False


# ------------------------------------------------- the loop's upload action
class _FakeCommitSession:
    """A commit-mode session for the loop: it records upload_file calls and hands
    back a form contract on read_commit_target, so the loop can fold the attached
    file into commit_state."""

    def __init__(self, page, form):
        self.page = page
        self.stats = browser_session.InterceptStats()
        self.allowlist = {"example.com"}
        self.uploads: list = []
        self._form = form
        self.upload_calls: list = []

    async def settle(self):
        pass

    async def goto(self, url):
        self.page.navigate(url)

    async def upload_file(self, obs, index, path):
        self.upload_calls.append((index, path))
        self.uploads = [u for u in self.uploads if u.get("name") != "attachment"]
        self.uploads.append({"name": "attachment", "path": path})
        return True, ""

    async def read_commit_target(self, obs, index):
        return dict(self._form)


async def test_the_loop_attaches_the_file_then_folds_it_into_commit_state(tmp_path):
    """upload → submit: the loop attaches the pre-grounded file, and the returned
    commit_state carries it so the approval binds to it. The path is fixed
    (upload_path) — the model only chose which input."""
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    page = ScriptedPage([
        _page([_el(1, role="file", name="attachment"), _el(2, role="button", name="Upload")],
              url="https://example.com/form"),
    ])
    form = {"action": "https://example.com/upload", "method": "POST", "fields": [],
            "has_password": False}
    session = _FakeCommitSession(page, form)
    provider = LoopProvider(['{"action":"upload","index":1}', '{"action":"submit","index":2}'])

    outcome = await browser_loop.run_browse(
        session, "upload resume.pdf to example.com", provider,
        commit=True, upload_path=str(f),
    )

    assert outcome.commit_required is True
    assert session.upload_calls == [(1, str(f))]
    assert outcome.commit_state["uploads"] == [{"name": "attachment", "path": str(f)}]


async def test_the_loop_never_offers_upload_without_a_file():
    """Read-only mode (or a commit with no upload_path) can never attach a file:
    an 'upload' action with nothing behind it is a no-op the loop notes and moves
    past, and the fixed file is what makes this safe."""
    page = ScriptedPage([_page([_el(1, role="file", name="attachment")], url="https://example.com/form")])
    form = {"action": "https://example.com/upload", "method": "POST", "fields": [], "has_password": False}
    session = _FakeCommitSession(page, form)
    # commit=True but NO upload_path → the action is offered nowhere; even a stray
    # 'upload' does nothing (can_upload is False).
    provider = LoopProvider(['{"action":"upload","index":1}', '{"action":"done","reason":"stop"}'])

    outcome = await browser_loop.run_browse(session, "post a comment", provider, commit=True)

    assert session.upload_calls == []  # no file was ever attached


# ------------------------------------------------- planner end-to-end + gate
async def test_a_grounded_upload_discovers_then_pauses_showing_the_file(
    db_session, monkeypatch, tmp_path
):
    """The whole flow: a grounded, safe upload_path reaches discovery, the code-read
    contract (with the file) lands in action_detail AND the signature, and the plan
    PAUSES — nothing is sent before approval."""
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    upload_path = str(f)
    state = {
        "url": "https://example.com/upload", "method": "POST",
        "fields": [{"name": "note", "value": "here it is"}],
        "uploads": [{"name": "attachment", "path": upload_path}],
    }

    async def fake_discover(params, session_id=None):
        assert params.get("upload_path") == upload_path  # the grounded path reaches discovery
        return browser_commit.CommitDiscovery(state=dict(state))

    calls: list = []
    monkeypatch.setattr(browser_commit, "discover", fake_discover)
    monkeypatch.setattr(planner_mod, "execute_tool", _record_exec(calls))

    upload_step = step(
        "Upload the file", "browse_commit",
        goal=f"upload {f.name} to example.com",
        start_url="https://example.com/careers",
        allowed_origins=["example.com"],
        upload_path=upload_path,
    )
    provider = FakeProvider([plan_json([upload_step])])
    plan = await AgentPlanner(db_session, provider, session_id="s-upload").start(
        f"upload {f.name} to example.com"
    )

    assert plan.status == PlanStatus.AWAITING_APPROVAL
    s = plan.steps[0]
    assert "attach file:" in s.action_detail
    assert upload_path in s.action_detail        # the exact file on the card
    # The approval binds to the file: it is inside the signed contract (the path
    # is JSON-escaped in signature(), so match on the basename + the uploads key).
    assert '"uploads"' in s.signature()
    assert f.name in s.signature()
    assert calls == []                           # nothing sent before approval


async def test_an_ungrounded_upload_is_refused_before_discovery(
    db_session, monkeypatch, tmp_path
):
    """A real, safe file the user never named is refused by the planner gate —
    discovery never runs (a page can never name a file to exfiltrate)."""
    f = tmp_path / "secret.pdf"
    f.write_text("x")

    async def boom(params, session_id=None):  # pragma: no cover - must not run
        raise AssertionError("discovery ran for an ungrounded upload")

    monkeypatch.setattr(browser_commit, "discover", boom)

    bad = step(
        "Upload it", "browse_commit",
        goal="upload a file to example.com",   # never names secret.pdf
        start_url="https://example.com/x",
        allowed_origins=["example.com"],
        upload_path=str(f),
    )
    provider = FakeProvider([plan_json([bad]), plan_json([bad])])
    plan = await AgentPlanner(db_session, provider, session_id="s-uplbad").start(
        "upload a file to example.com"
    )
    assert plan.status == PlanStatus.FAILED


async def test_an_upload_submit_is_blocked_without_approval(db_session):
    """The structural proof the file never leaves unapproved: the only path that
    sends is the DESTRUCTIVE browse_commit submit, and execute_tool refuses it
    without approved=True."""
    from app.tools.registry import execute_tool

    result = await execute_tool(
        "browse_commit", {"upload_path": r"C:\x\resume.pdf"}, db_session, approved=False
    )
    assert result.success is False
    assert result.requires_approval is True

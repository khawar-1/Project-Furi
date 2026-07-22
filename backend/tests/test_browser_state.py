"""Typed browse state (browser refactor Phase 3a): the Handoff vocabulary and
the commit-param write discipline.

The properties pinned here:
- flag structs (BrowseOutcome / CommitDiscovery) derive EXACTLY ONE payload,
  with the planner's dispatch precedence;
- a payload survives the JSON round trip a parked plan or push event needs;
- the "_commit"/"_commits_done" stamps have one writer whose start_url helper
  REFUSES an approval-bound step (the invariant that used to be a comment).
"""
from app.agents.browser_commit import CommitDiscovery
from app.agents.browser_loop import BrowseOutcome
from app.browser import state
from app.browser.state import Handoff, HandoffPayload


def _outcome(**kwargs) -> BrowseOutcome:
    kwargs.setdefault("success", False)
    return BrowseOutcome(actions_taken=1, **kwargs)


# ------------------------------------------------------------- derivation
def test_a_plain_ended_run_carries_no_handoff():
    assert state.handoff_from_outcome(_outcome(success=True)) is None


def test_login_wall_derives_login():
    payload = state.handoff_from_outcome(
        _outcome(login_required=True, login_site="linkedin.com", login_url="https://l/x")
    )
    assert payload.reason is Handoff.LOGIN
    assert payload.site == "linkedin.com"
    assert payload.url == "https://l/x"


def test_signup_wall_kind_derives_signup():
    payload = state.handoff_from_outcome(
        _outcome(login_required=True, wall_kind="signup", login_site="indeed.com")
    )
    assert payload.reason is Handoff.SIGNUP


def test_fill_beats_every_other_flag():
    """Precedence is the planner's dispatch order — fill first. A struct with
    several flags raised must derive the same winner the if-chains picked."""
    payload = state.handoff_from_outcome(
        _outcome(
            fill_required=True,
            fill_field="Phone",
            fill_value="123",
            login_required=True,
            challenge_required=True,
            origin_approval_required=True,
            commit_required=True,
        )
    )
    assert payload.reason is Handoff.FILL_FIELD
    assert payload.field == "Phone"
    assert payload.suggested_value == "123"


def test_action_approval_derives_and_round_trips():
    """A world-acting gesture hand-off (2026-07-22) derives ACTION_APPROVAL and
    survives the JSON round trip a parked plan needs (action_desc + site)."""
    payload = state.handoff_from_outcome(
        _outcome(
            action_approval_required=True,
            action_description='send "hi anas"',
            action_site="linkedin.com",
        )
    )
    assert payload.reason is Handoff.ACTION_APPROVAL
    assert payload.action_desc == 'send "hi anas"'
    assert payload.site == "linkedin.com"
    revived = HandoffPayload.from_dict(payload.to_dict())
    assert revived == payload


def test_auth_offer_derives_with_capabilities():
    payload = state.handoff_from_outcome(
        _outcome(
            auth_offer_required=True,
            auth_offer_signin=True,
            auth_offer_signup=False,
            auth_offer_site="remote.co",
            auth_offer_url="https://remote.co/apply",
        )
    )
    assert payload.reason is Handoff.AUTH_OFFER
    assert payload.auth_signin and not payload.auth_signup
    assert payload.site == "remote.co"


def test_challenge_carries_kind_and_mode():
    payload = state.handoff_from_outcome(
        _outcome(
            challenge_required=True,
            challenge_kind="Cloudflare",
            challenge_mode="embedded",
            challenge_site="wwr.com",
        )
    )
    assert payload.reason is Handoff.CHALLENGE
    assert payload.challenge_kind == "Cloudflare"
    assert payload.challenge_mode == "embedded"


def test_origin_approval_carries_candidate_and_url():
    payload = state.handoff_from_outcome(
        _outcome(
            origin_approval_required=True,
            origin_candidate="greenhouse.io",
            origin_url="https://boards.greenhouse.io/x",
        )
    )
    assert payload.reason is Handoff.ORIGIN_APPROVAL
    assert payload.origin == "greenhouse.io"


def test_commit_carries_the_contract():
    contract = {"url": "https://e.com/f", "method": "POST", "fields": []}
    payload = state.handoff_from_outcome(
        _outcome(commit_required=True, commit_state=contract)
    )
    assert payload.reason is Handoff.COMMIT
    assert payload.commit_state == contract


def test_discovery_derives_through_the_same_function():
    payload = state.handoff_from_discovery(
        CommitDiscovery(fill_required=True, fill_field="City")
    )
    assert payload.reason is Handoff.FILL_FIELD
    assert payload.field == "City"


# ------------------------------------------------------------- serialization
def test_payload_round_trips_through_json_dict():
    original = HandoffPayload(
        reason=Handoff.CHALLENGE,
        site="wwr.com",
        challenge_kind="reCAPTCHA",
        challenge_mode="interstitial",
    )
    assert HandoffPayload.from_dict(original.to_dict()) == original


def test_from_dict_tolerates_junk():
    assert HandoffPayload.from_dict({}) is None
    assert HandoffPayload.from_dict({"reason": "no-such-reason"}) is None
    assert HandoffPayload.from_dict({"reason": None}) is None


# ------------------------------------------------------------- commit params
def test_stamp_and_read_the_contract():
    params: dict = {}
    contract = {"url": "https://e.com/f", "method": "POST", "fields": []}
    state.stamp_commit_contract(params, contract)
    assert state.commit_contract(params) == contract
    assert state.commits_done(params) == 0          # absent until a re-arm


def test_a_rearm_stamps_the_done_count():
    params: dict = {}
    state.stamp_commit_contract(params, {"url": "u"}, done=2)
    assert state.commits_done(params) == 2


def test_clear_drops_both_stamps():
    params = {"goal": "apply", state.COMMIT_PARAM: {"url": "u"},
              state.COMMITS_DONE_PARAM: 1}
    state.clear_commit_params(params)
    assert state.commit_contract(params) is None
    assert state.commits_done(params) == 0
    assert params["goal"] == "apply"                # only the stamps die


def test_stamp_start_url_points_a_pre_discovery_step():
    params = {"start_url": "https://old.example.com"}
    assert state.stamp_start_url(params, "https://approved.example.com/x") is True
    assert params["start_url"] == "https://approved.example.com/x"


def test_stamp_start_url_refuses_an_approval_bound_step():
    """The bug-8 regression: a step carrying a commit contract is approval-bound
    — re-pointing its start_url would mutate an approved signature's step
    without a fresh approval. The helper must refuse, not comply."""
    params = {
        "start_url": "https://old.example.com",
        state.COMMIT_PARAM: {"url": "https://e.com/f", "method": "POST"},
    }
    assert state.stamp_start_url(params, "https://approved.example.com/x") is False
    assert params["start_url"] == "https://old.example.com"

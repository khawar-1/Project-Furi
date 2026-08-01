"""
"Did you mean…?" — a named site that does not exist (2026-08-01).

THE INCIDENT, spoken into the mic:
    "Go to junaidjamshed.com and add Jhanan Sports 100 ML in cart."
transcribed as
    "Go to junitjamsheed.com and add Jhanan Sports 100 ML in cart."

Chromium answered ERR_NAME_NOT_RESOLVED, commit_flow turned that into a flat
step failure, both replans failed, and the task died with no recourse. Google,
given the identical misspelling, puts the real site on the first screen.

Two halves are pinned here:
  - the LOOKUP (browser/did_you_mean.py): which candidates are offered, and the
    property that it cannot fabricate one;
  - the PLANNER flow: the pause, the code-decided answer, the code-enforced
    resume, and — the part that matters most — every way it must NOT fire.

⚠️ Row fixtures marked "REAL" are the verbatim output of the live provider for
the incident's own query, captured 2026-08-01. They are what make the ordering
argument checkable: the right answer was THIRD.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import planner as planner_mod
from app.agents.planner import AgentPlanner
from app.agents.schemas import AgentPlan, PlanStatus, StepStatus
from app.browser import did_you_mean
from app.browser import state as browse_state

from tests.test_agent_planner import FakeProvider, plan_json, step


# The live provider's answer to a search for "junitjamsheed", in its real order.
# junaidjamshed.com is #3 — behind Wikipedia and Instagram.
REAL_ROWS = [
    {"url": "https://en.wikipedia.org/wiki/Junaid_Jamshed", "title": "Junaid Jamshed - Wikipedia"},
    {"url": "https://www.instagram.com/j.junaidjamshed?hl=en", "title": "J. Junaid Jamshed (@j.junaidjamshed)"},
    {"url": "https://www.junaidjamshed.com", "title": "J. Junaid Jamshed Official Website"},
    {"url": "https://junit.org", "title": "JUnit"},
    {"url": "https://www.parasoft.com/video/generate-junit-tests", "title": "Generate JUnit Tests"},
    {"url": "https://www.wearedevelopers.com/jobs/ls/junit", "title": "JUnit Developer Jobs"},
    {"url": "https://us.junaidjamshed.com", "title": "J. Junaid Jamshed US"},
    {"url": "https://www.trustpilot.com/review/junaidjamshed.com", "title": "Trustpilot"},
]


def _browse_step(start="https://junitjamsheed.com", origins=("junitjamsheed.com",)) -> dict:
    return step(
        "Add the perfume to the cart",
        "browse_commit",
        goal="add Jhanan Sports 100 ML to the cart",
        start_url=start,
        allowed_origins=list(origins),
    )


@pytest.fixture
def offer(monkeypatch):
    """Point the lookup at fixed rows and a resolver that says yes, so a test
    exercises the RANKING rather than the network."""

    def _install(rows=REAL_ROWS, resolves=True):
        async def _search(query, limit):
            return list(rows)

        async def _resolve(host):
            return resolves(host) if callable(resolves) else bool(resolves)

        monkeypatch.setattr(did_you_mean, "SEARCH_FACTORY", _search)
        monkeypatch.setattr(did_you_mean, "RESOLVER_FACTORY", _resolve)

    return _install


# ==================================================================== ranking
def test_the_incident_is_ranked_out_of_the_real_search_results():
    """THE INCIDENT, FROZEN. Out of the eight rows the live provider actually
    returned for 'junitjamsheed', exactly one is offered — the site the user
    meant."""
    got = did_you_mean.rank_candidates("junitjamsheed.com", REAL_ROWS)
    assert [s.host for s in got] == ["junaidjamshed.com"]
    assert got[0].score == pytest.approx(84.6, abs=0.1)


def test_search_rank_alone_would_have_offered_wikipedia():
    """WHY THE FLOOR IS THE FILTER, not "take result #1". The right answer is
    THIRD in the real results; the first two are a Wikipedia article and an
    Instagram profile ABOUT the brand. Both are rejected on similarity, which
    is the only signal that separates them."""
    assert did_you_mean._candidate_host(REAL_ROWS[0]) == "wikipedia.org"
    assert [s.host for s in did_you_mean.rank_candidates("junitjamsheed.com", REAL_ROWS[:2])] == []


def test_the_worst_near_miss_is_measured_and_rejected():
    """junit.org shares a five-letter prefix with the typo and is the closest
    piece of NOISE in the real results. Pinned as a number so a floor change
    that would admit it fails loudly."""
    assert did_you_mean._similarity("junitjamsheed", "junit") == pytest.approx(55.6, abs=0.1)
    assert did_you_mean._similarity("junitjamsheed", "junaidjamshed") == pytest.approx(84.6, abs=0.1)
    assert 55.6 < did_you_mean.SIMILARITY_FLOOR < 84.6


def test_every_offered_host_came_from_a_search_row():
    """THE NO-FABRICATION PROPERTY. Every value offered is a slice of a search
    result — the extract.py rule applied to hostnames. There is no path by
    which this module can invent a domain."""
    rows = REAL_ROWS + [{"url": "https://shop.junaidjamshed.com/x", "title": "Shop"}]
    row_text = " ".join(r["url"] for r in rows)
    for s in did_you_mean.rank_candidates("junitjamsheed.com", rows):
        assert s.host.split(".")[0] in row_text


def test_subdomains_collapse_to_one_registrable_site():
    """www.junaidjamshed.com, us.junaidjamshed.com and shop.junaidjamshed.com are
    ONE site. What the user approves is a site, so that is the dedupe key too."""
    got = did_you_mean.rank_candidates("junitjamsheed.com", REAL_ROWS)
    assert [s.host for s in got] == ["junaidjamshed.com"]


def test_the_host_that_failed_is_never_offered_back():
    """A search engine happily returns reviews and social pages ABOUT a dead
    domain (the real results include a Trustpilot review page). Offering the
    address that just failed would be a loop."""
    rows = [{"url": "https://junitjamsheed.com/about", "title": "About"}] + REAL_ROWS
    assert "junitjamsheed.com" not in [
        s.host for s in did_you_mean.rank_candidates("junitjamsheed.com", rows)
    ]


def test_order_is_search_order_not_score_order():
    """Rank answers "which of these is the real site"; distance answers "is this
    the same word". Re-ordering by distance would promote a lookalike over the
    genuine site it imitates, since a squat is by construction spelled closer."""
    rows = [
        {"url": "https://junaidjamshed.com", "title": "official"},
        {"url": "https://junitjamsheee.com", "title": "squat"},   # closer to the typo
    ]
    got = did_you_mean.rank_candidates("junitjamsheed.com", rows)
    assert got[0].host == "junaidjamshed.com"
    assert got[1].score > got[0].score  # the squat really is the closer string


def test_a_name_too_short_to_misspell_suggests_nothing():
    assert did_you_mean.rank_candidates("abc.com", REAL_ROWS) == []


def test_the_offer_is_capped():
    rows = [{"url": f"https://junaidjamshe{i}.com", "title": ""} for i in range(9)]
    assert len(did_you_mean.rank_candidates("junitjamsheed.com", rows)) <= did_you_mean.MAX_SUGGESTIONS


# ============================================================ verify + degrade
async def test_an_offered_option_is_dns_verified(offer):
    """THE _validated_question RULE, applied to hostnames: an option written as
    a concrete thing must EXIST. Answering "that address doesn't resolve" with a
    second address that also does not resolve is a fabricated clickable fact."""
    offer(resolves=False)
    assert await did_you_mean.suggest_sites("junitjamsheed.com") == []

    offer(resolves=True)
    got = await did_you_mean.suggest_sites("junitjamsheed.com")
    assert [s.host for s in got] == ["junaidjamshed.com"]


async def test_a_search_failure_offers_nothing_rather_than_raising(monkeypatch):
    """Best-effort in every direction: the lookup can only ever ADD a question,
    never turn a working path into a failing one."""

    async def _boom(query, limit):
        raise RuntimeError("provider down")

    monkeypatch.setattr(did_you_mean, "SEARCH_FACTORY", _boom)
    assert await did_you_mean.suggest_sites("junitjamsheed.com") == []


async def test_the_correctly_spelled_host_suggests_nothing(offer):
    """A live check: searching 'junaidjamshed' returns junaidjamshed.com, which
    IS the typed host, so there is nothing to suggest — no spurious "did you
    mean the site you just named?"."""
    offer(rows=[{"url": "https://www.junaidjamshed.com", "title": "official"}])
    assert await did_you_mean.suggest_sites("junaidjamshed.com") == []


# ============================================================== planner flow
def _discovery(**kw):
    from app.browser.commit_flow import CommitDiscovery

    return CommitDiscovery(**kw)


def _install_discovery(monkeypatch, discovery):
    calls = {"n": 0}

    async def fake_discover(params, session_id=None, **kwargs):
        calls["n"] += 1
        return discovery

    monkeypatch.setattr(
        "app.agents.browser_commit.discover", fake_discover, raising=False
    )
    return calls


async def test_the_incident_pauses_and_asks_instead_of_dying(db_session, monkeypatch, offer):
    """THE INCIDENT END TO END. A commit discovery whose opening navigation hit
    NXDOMAIN now PAUSES with the real site offered, instead of failing the step
    into two doomed replans."""
    offer()
    _install_discovery(
        monkeypatch,
        _discovery(
            site_unresolved=True,
            unresolved_host="junitjamsheed.com",
            error="Couldn't load junitjamsheed.com: that address doesn't resolve.",
        ),
    )
    provider = FakeProvider([plan_json([_browse_step()])] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-dym")

    plan = await planner.start(
        "Go to junitjamsheed.com and add Jhanan Sports 100 ML in card."
    )

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    assert plan.question.kind == "site_correction"
    assert "junitjamsheed.com" in plan.question.text
    assert "junaidjamshed.com" in plan.question.text
    assert "junaidjamshed.com" in plan.question.options
    assert plan.site_corrections == 1
    assert plan.pending_site_correction == "junitjamsheed.com"
    # Nothing ran, and the step is still pending for the resume.
    assert all(s.status == StepStatus.PENDING for s in plan.steps)


async def test_answering_repoints_the_browse_in_code(db_session, monkeypatch, offer):
    """THE RESUME IS CODE-ENFORCED, and this is the reason: the GOAL STRING
    still says 'junitjamsheed.com'. A revise round would be handed the typo as
    the most authoritative text in its prompt — the 2026-07-12 folder_resolver
    lesson (the model was trusted to carry a user's answer forward, and kept the
    original value). So the answer must re-point the step directly."""
    offer()
    _install_discovery(
        monkeypatch,
        _discovery(site_unresolved=True, unresolved_host="junitjamsheed.com", error="x"),
    )
    provider = FakeProvider([plan_json([_browse_step()])] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-dym2")
    plan = await planner.start("Go to junitjamsheed.com and add Jhanan Sports 100 ML in card.")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    calls_before = provider.calls
    # The corrected site now resolves, so discovery gets past the navigation.
    _install_discovery(monkeypatch, _discovery(state={"url": "https://junaidjamshed.com/cart",
                                                     "method": "POST", "fields": []}))
    plan = await planner.answer(plan, "junaidjamshed.com")

    browse = next(s for s in plan.steps if s.tool == "browse_commit")
    assert "junaidjamshed.com" in browse.parameters["start_url"]
    assert "junitjamsheed" not in browse.parameters["start_url"]
    origins = [o.lower() for o in browse.parameters["allowed_origins"]]
    assert "junaidjamshed.com" in origins
    assert "junitjamsheed.com" not in origins
    # Grounded for any LATER replan too — revise drops pending steps, so an
    # origin living only on the step would not survive one.
    assert "junaidjamshed.com" in plan.approved_origins
    # And it cost NO planning call: the answer re-entered EXECUTE directly.
    assert provider.calls == calls_before


async def test_the_approval_card_never_names_the_dead_site(db_session, monkeypatch, offer):
    """FOUND BY THE LIVE RUN, 2026-08-01. The re-point was correct and the
    binding contract said POST https://www.junaidjamshed.com/cart/add — but the
    card's own sentence still read "Open junitjamsheed.com, find …", because
    `description` is LLM-authored at draft time and only the parameters had been
    moved. Cosmetic here (action_detail is what binds), and the SAME defect as
    the folder round one day earlier, where a substituted path left the card
    naming the old drive. A card that names one site while acting on another is
    a card the user cannot rely on."""
    offer()
    _install_discovery(
        monkeypatch,
        _discovery(site_unresolved=True, unresolved_host="junitjamsheed.com", error="x"),
    )
    drafted = _browse_step()
    drafted["description"] = (
        "Open junitjamsheed.com, find 'Jhanan Sports 100 ML', and submit its "
        "add-to-cart form for your approval."
    )
    provider = FakeProvider([plan_json([drafted])] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-dym-card")
    plan = await planner.start("Go to junitjamsheed.com and add Jhanan Sports 100 ML in card.")

    _install_discovery(monkeypatch, _discovery(state={"url": "https://www.junaidjamshed.com/cart/add",
                                                     "method": "POST", "fields": []}))
    plan = await planner.answer(plan, "junaidjamshed.com")

    browse = next(s for s in plan.steps if s.tool == "browse_commit")
    assert "junitjamsheed" not in browse.description
    assert "junaidjamshed.com" in browse.description
    # The contract and the prose now agree.
    assert "junitjamsheed" not in (browse.action_detail or "")


async def test_declining_stops_honestly_and_guesses_nothing(db_session, monkeypatch, offer):
    offer()
    _install_discovery(
        monkeypatch,
        _discovery(site_unresolved=True, unresolved_host="junitjamsheed.com", error="x"),
    )
    provider = FakeProvider([plan_json([_browse_step()])] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-dym3")
    plan = await planner.start("Go to junitjamsheed.com and add Jhanan Sports 100 ML in card.")

    plan = await planner.answer(plan, "No — none of these")

    assert plan.status == PlanStatus.CANCELLED
    assert "junitjamsheed.com" in (plan.message or "")
    assert all(s.status == StepStatus.SKIPPED for s in plan.pending_steps())


# ================================================== the ways it must NOT fire
async def test_a_reachable_domain_that_merely_failed_never_gets_a_suggestion(
    db_session, monkeypatch, offer
):
    """THE KEY SAFETY TEST. A cert error, a refused connection, a timeout — all
    mean the domain EXISTS and the user named it correctly. Only NXDOMAIN says
    "nothing answers to that NAME". commit_flow sets the flag for the "dns"
    class alone, so these fail exactly as they did before."""
    offer()
    _install_discovery(
        monkeypatch,
        _discovery(error="Couldn't load junitjamsheed.com: its HTTPS certificate isn't valid."),
    )
    provider = FakeProvider([plan_json([_browse_step()])] * 6)
    planner = AgentPlanner(db_session, provider, session_id="s-dym4")
    plan = await planner.start("Go to junitjamsheed.com and add Jhanan Sports 100 ML in card.")

    assert plan.status != PlanStatus.AWAITING_CHOICE
    assert plan.site_corrections == 0
    assert plan.pending_site_correction is None


async def test_only_the_dns_class_is_flagged_by_the_discovery():
    """The producer half of the rule above, at the source: the same exception
    type with a different class must not set the flag."""
    from app.core.browser_session import UNREACHABLE_DNS, BrowserUnreachable

    dns = BrowserUnreachable("x", kind=UNREACHABLE_DNS, host="junitjamsheed.com")
    cert = BrowserUnreachable("x", kind="cert", host="junitjamsheed.com")
    offline = BrowserUnreachable("x", kind="offline", host="junitjamsheed.com")

    assert dns.kind == "dns"
    assert cert.kind != UNREACHABLE_DNS
    # Our own network being down would fail every candidate lookup anyway; the
    # honest report is "no internet", never a spelling suggestion.
    assert offline.kind != UNREACHABLE_DNS


async def test_no_plausible_candidate_falls_through_to_the_old_failure(
    db_session, monkeypatch, offer
):
    """When the search finds nothing similar, the step keeps its own honest
    "that address doesn't resolve" and the plan behaves exactly as it did
    before this feature existed. No regression path."""
    offer(rows=[{"url": "https://wikipedia.org/x", "title": "unrelated"}])
    _install_discovery(
        monkeypatch,
        _discovery(
            site_unresolved=True,
            unresolved_host="junitjamsheed.com",
            error="Couldn't load junitjamsheed.com: that address doesn't resolve.",
        ),
    )
    provider = FakeProvider([plan_json([_browse_step()])] * 6)
    planner = AgentPlanner(db_session, provider, session_id="s-dym5")
    plan = await planner.start("Go to junitjamsheed.com and add Jhanan Sports 100 ML in card.")

    assert plan.status != PlanStatus.AWAITING_CHOICE
    assert plan.site_corrections == 0


async def test_the_correction_budget_terminates_a_chain(db_session, monkeypatch, offer):
    """Bounded, terminal, non-spinning. If the CORRECTED site also fails to
    resolve we ask once more; a third would be chaining guesses off guesses."""
    offer()
    _install_discovery(
        monkeypatch,
        _discovery(site_unresolved=True, unresolved_host="junitjamsheed.com", error="x"),
    )
    provider = FakeProvider([plan_json([_browse_step()])] * 8)
    planner = AgentPlanner(db_session, provider, session_id="s-dym6")
    plan = await planner.start("Go to junitjamsheed.com and add Jhanan Sports 100 ML in card.")

    seen = 0
    for _ in range(planner_mod._MAX_SITE_CORRECTIONS + 2):
        if plan.status != PlanStatus.AWAITING_CHOICE:
            break
        seen += 1
        plan = await planner.answer(plan, "junaidjamshed.com")

    # Not vacuous: it really does ask (>=1) and really does stop (<= the cap).
    assert 1 <= seen <= planner_mod._MAX_SITE_CORRECTIONS
    assert plan.status != PlanStatus.AWAITING_CHOICE
    assert plan.site_corrections <= planner_mod._MAX_SITE_CORRECTIONS


# ===================================================== the answer, in code
@pytest.mark.parametrize(
    "reply,offered,expected",
    [
        ("junaidjamshed.com", ["junaidjamshed.com"], "junaidjamshed.com"),
        ("junaidjamshed", ["junaidjamshed.com"], "junaidjamshed.com"),
        ("yes", ["junaidjamshed.com"], "junaidjamshed.com"),
        # "yes" answers nothing when several were offered.
        ("yes", ["a-site.com", "b-site.com"], ""),
        ("b-site.com", ["a-site.com", "b-site.com"], "b-site.com"),
        ("no", ["junaidjamshed.com"], ""),
        ("No — none of these", ["junaidjamshed.com"], ""),
        ("neither", ["junaidjamshed.com"], ""),
        ("what?", ["junaidjamshed.com"], ""),
        ("", ["junaidjamshed.com"], ""),
        # The user naming a site in their own words is the STRONGEST grounding
        # there is — stronger than any option we offered — so it wins even when
        # it is not on the list, and even after a leading "no".
        ("no, it's actually junaidjamshed.com.pk", ["junaidjamshed.com"], "junaidjamshed.com.pk"),
    ],
)
def test_the_reply_is_decided_in_code_and_fails_closed(reply, offered, expected):
    assert planner_mod._match_site_choice(reply, offered) == expected


def test_the_pause_survives_being_parked(offer):
    """The ask PARKS the plan, so the question's own options and the budget
    counter have to survive a serialize/restore round trip — otherwise the
    answer has nothing to match against and the chain restarts from zero."""
    plan = AgentPlan(goal="g")
    plan.pending_site_correction = "junitjamsheed.com"
    plan.pending_site_candidates = ["junaidjamshed.com"]
    plan.site_corrections = 1

    restored = AgentPlan.model_validate(plan.model_dump())

    assert restored.pending_site_correction == "junitjamsheed.com"
    assert restored.pending_site_candidates == ["junaidjamshed.com"]
    assert restored.site_corrections == 1


def test_a_plan_parked_before_this_feature_still_deserializes():
    payload = AgentPlan(goal="g").model_dump()
    for key in ("pending_site_correction", "pending_site_candidates", "site_corrections"):
        payload.pop(key, None)
    restored = AgentPlan.model_validate(payload)
    assert restored.pending_site_correction is None
    assert restored.site_corrections == 0


def test_the_read_browse_path_raises_the_same_handoff():
    """browse (READ) and browse_commit reach the same pause through the ONE
    taxonomy — the whole point of browser/state.py. The signal is narrow: the
    flag AND the host, from the tool's own output, never page text."""
    from app.core.base_tool import PermissionLevel, ToolResult

    st = planner_mod.PlanStep(
        description="look",
        tool="browse",
        parameters={},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
    )
    out = {"site_unresolved": True, "unresolved_host": "junitjamsheed.com"}
    result = ToolResult(success=False, output=out, error="x")

    assert planner_mod._browse_site_unresolved_signal(st, result) == out

    payload = browse_state.handoff_from_flags(out)
    assert payload is not None
    assert payload.reason is browse_state.Handoff.SITE_UNRESOLVED
    assert payload.site == "junitjamsheed.com"

    # …and a browse that merely failed some other way raises nothing.
    assert planner_mod._browse_site_unresolved_signal(
        st, ToolResult(success=False, output={"site_unreachable": True}, error="x")
    ) is None


def test_an_unresolved_site_outranks_every_other_pause_flag():
    """Precedence: if the address has no DNS record there was never a page to
    hit a wall or a form on, so every other flag on that outcome is stale."""
    payload = browse_state.handoff_from_flags(
        {
            "site_unresolved": True,
            "unresolved_host": "junitjamsheed.com",
            "login_required": True,
            "commit_required": True,
        }
    )
    assert payload.reason is browse_state.Handoff.SITE_UNRESOLVED

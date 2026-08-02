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
import json

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


# ============================================ the glued verb (2026-08-02)
# The live provider's answer to a search for "openjunetjamshed", captured
# 2026-08-02. The right site is 7th of 8, behind two YouTube videos, a reseller,
# Wikipedia and the BBC.
GLUED_ROWS = [
    {"url": "https://www.youtube.com/watch?v=lkryrHfgmns",
     "title": "Grand Opening of Junaid Jamshed Store New Jersey, Mixed Reactions of JJ Fans"},
    {"url": "https://www.youtube.com/watch?v=GKbnaRbKSYU",
     "title": "J.  Junaid Jamshed Grand Opening Ceremony | iTVusa"},
    {"url": "https://www.786shop.com/brands/junaid-jamshed",
     "title": "Junaid Jamshed | Buy Dresses Online in USA"},
    {"url": "https://en.wikipedia.org/wiki/Junaid_Jamshed", "title": "Junaid Jamshed"},
    {"url": "https://www.bbc.com/news/world-asia-38246109",
     "title": "Junaid Jamshed: Pakistan pop icon turned preacher - BBC News"},
    {"url": "https://muslimmatters.org/2016/12/08/junaid-jamshed-inspired-a-generation",
     "title": "Junaid Jamshed Inspired A Generation of Struggling Souls - MuslimMatters.org"},
    {"url": "https://www.junaidjamshed.com", "title": "J. Junaid Jamshed Official Website"},
    {"url": "https://us.junaidjamshed.com", "title": "J. Junaid Jamshed US"},
]

# The live provider's answer to a search for "amazn", captured 2026-08-02.
# amazon.com is NOT IN IT — every row is an article, a stock quote or a profile.
# This is the shape tier 1 structurally cannot serve.
AMAZN_ROWS = [
    {"url": "https://en.wikipedia.org/wiki/Amazon_(company)", "title": "Amazon (company)"},
    {"url": "https://www.ebsco.com/research-starters/amazon-company",
     "title": "Amazon (company) | Business and Management"},
    {"url": "https://finance.yahoo.com/quote/AMZN", "title": "Amazon.com, Inc. (AMZN)"},
    {"url": "https://www.globaldata.com/company-profile/amazoncom-inc",
     "title": "Amazon.com Inc - Company Profile"},
    {"url": "https://www.google.com/finance/beta/quote/AMZN:NASDAQ",
     "title": "Amazon.com Inc (AMZN) Stock Price and News - Google Finance"},
    {"url": "https://finance.yahoo.com/quote/AMZN/profile",
     "title": "Amazon.com, Inc. (AMZN) Company Profile and Facts"},
    {"url": "https://www.cnn.com/markets/stocks/AMZN", "title": "AMZN Stock Quote Price and Forecast"},
    {"url": "https://www.techtarget.com/whatis/definition/Amazon",
     "title": "What is Amazon? Definition and Company History"},
]


def test_the_glued_verb_misses_the_floor_by_one_point():
    """THE MEASUREMENT THAT FORCED TIER 1, frozen as numbers. Speech has no
    spaces, so STT welds "open" onto a proper noun it does not know. The search
    still returns the right site; the score does not clear the floor."""
    assert did_you_mean._similarity(
        "openjunetjamshed", "junaidjamshed"
    ) == pytest.approx(69.0, abs=0.1)
    assert did_you_mean._similarity(
        "junetjamshed", "junaidjamshed"
    ) == pytest.approx(80.0, abs=0.1)
    assert 69.0 < did_you_mean.SIMILARITY_FLOOR < 80.0
    # …and that one point is the whole difference between a dead end and an answer.
    assert did_you_mean.rank_candidates("openjunetjamshed.com", GLUED_ROWS) == []


def test_the_glued_verb_incident_is_offered_once_the_verb_is_stripped():
    """THE INCIDENT, FROZEN, against the rows the live provider really returned."""
    got = did_you_mean.rank_candidates(
        "openjunetjamshed.com", GLUED_ROWS, extra_names=["junetjamshed"]
    )
    assert [s.host for s in got] == ["junaidjamshed.com"]
    assert got[0].score == pytest.approx(80.0, abs=0.1)


@pytest.mark.parametrize(
    "host,expected",
    [
        # A verb welded on is stripped…
        ("openjunetjamshed.com", ["openjunetjamshed", "junetjamshed"]),
        ("opengithb.com", ["opengithb", "githb"]),
        ("gotodaraz.pk", ["gotodaraz", "daraz"]),
        ("visitjunaidjamshed.com", ["visitjunaidjamshed", "junaidjamshed"]),
        # …but never when what is left is too short to be a name. THE CASE THAT
        # MATTERS: openai is not "open" + "ai".
        ("openai.com", ["openai"]),
        ("gopro.com", ["gopro"]),
        # Nothing to strip.
        ("junetjamshed.com", ["junetjamshed"]),
        ("amazn.com", ["amazn"]),
        # Too short to guess about at all.
        ("abc.com", []),
    ],
)
def test_typed_variants_strips_only_a_leading_verb(host, expected):
    assert did_you_mean.typed_variants(host) == expected


def test_the_strip_is_a_fallback_and_cannot_change_a_working_answer():
    """A clean name yields the same single candidate it always did — the extra
    form is only ever consulted when the plain one found nothing, so a strip can
    turn "no suggestion" into "a suggestion" and nothing else."""
    plain = did_you_mean.rank_candidates("junitjamsheed.com", REAL_ROWS)
    assert [s.host for s in plain] == ["junaidjamshed.com"]
    assert did_you_mean.typed_variants("junitjamsheed.com") == ["junitjamsheed"]


# ============================================ tier 2: the name in the titles
def test_tier_two_finds_a_site_the_search_never_returned():
    """THE SHAPE TIER 1 CANNOT SERVE. amazon.com is absent from every row; the
    TITLES say "Amazon" seven times. That is what Google's "showing results
    for…" is doing, rebuilt out of the results we already have."""
    assert did_you_mean.rank_candidates("amazn.com", AMAZN_ROWS) == []

    got = did_you_mean.brand_candidates("amazn.com", AMAZN_ROWS)
    assert got[0].host == "amazon.com"
    assert got[0].score == pytest.approx(90.9, abs=0.1)


def test_tier_two_cannot_fabricate_a_name():
    """The property extract.py holds for records, applied to hostnames: every
    name offered is a token that appeared in a returned TITLE. Nothing else can
    come out of it."""
    # Every word of every title, and every ADJACENT PAIR run together — the
    # complete set of names this tier is structurally able to produce.
    sayable: set[str] = set()
    for row in AMAZN_ROWS:
        words = did_you_mean._title_words(str(row["title"]))
        for i, word in enumerate(words):
            sayable.add(word)
            if i + 1 < len(words):
                sayable.add(word + words[i + 1])

    got = did_you_mean.brand_candidates("amazn.com", AMAZN_ROWS)
    assert got  # the test is worthless if nothing was produced
    for suggestion in got:
        assert suggestion.host.rsplit(".", 1)[0] in sayable


def test_tier_two_needs_agreement_and_similarity_together():
    """Two independent signals, exactly as tier 1 demands. A name only one title
    mentions is a passing reference; a name several titles agree on but which is
    spelled nothing like the typed one is a different thing entirely."""
    once = [
        {"url": "https://x.com", "title": "Amazon rainforest"},
        {"url": "https://y.com", "title": "Rivers of Brazil"},
    ]
    assert did_you_mean.brand_candidates("amazn.com", once) == []

    agreed_but_unlike = [
        {"url": "https://x.com", "title": "Kayaking rivers guide"},
        {"url": "https://y.com", "title": "Rivers kayaking"},
        {"url": "https://z.com", "title": "Kayaking rivers again"},
    ]
    assert did_you_mean.brand_candidates("amazn.com", agreed_but_unlike) == []


def test_tier_two_never_offers_the_dead_host_back():
    rows = [
        {"url": "https://a.com", "title": "Junetjamshed store"},
        {"url": "https://b.com", "title": "Junetjamshed reviews"},
    ]
    got = did_you_mean.brand_candidates("junetjamshed.com", rows)
    assert "junetjamshed.com" not in [s.host for s in got]


def test_tier_two_uses_the_suffix_the_user_typed():
    """A .com.pk typo should be offered its own second-level domain first — the
    suffix is the one part of the address the user did get right."""
    rows = [
        {"url": "https://a.example", "title": "Junaid Jamshed store"},
        {"url": "https://b.example", "title": "Junaid Jamshed reviews"},
    ]
    got = [s.host for s in did_you_mean.brand_candidates("junetjamshed.com.pk", rows)]
    assert got[0] == "junaidjamshed.com.pk"
    assert "junaidjamshed.com" in got


def test_tier_two_is_bounded():
    rows = [
        {"url": f"https://s{i}.example", "title": "Junaid Jamshed Junaid Jamshed"}
        for i in range(12)
    ]
    got = did_you_mean.brand_candidates("junetjamshed.com", rows)
    assert len(got) <= did_you_mean.BRAND_MAX_HOSTS


# ============================================ the tiers, end to end
async def test_tier_two_runs_only_when_the_earlier_tiers_found_nothing(offer):
    """Order is the whole safety argument: a host the search actually returned
    always beats a host we synthesised from prose."""
    offer(rows=REAL_ROWS, resolves=True)
    got = await did_you_mean.suggest_sites("junitjamsheed.com")
    # Tier 1 answered; nothing synthetic ("jamshed.com") crept in beside it.
    assert [s.host for s in got] == ["junaidjamshed.com"]


async def test_the_incident_end_to_end_offers_the_real_site(offer):
    offer(rows=GLUED_ROWS, resolves=lambda h: h == "junaidjamshed.com")
    got = await did_you_mean.suggest_sites("openjunetjamshed.com")
    assert [s.host for s in got] == ["junaidjamshed.com"]


async def test_amazn_end_to_end_offers_amazon(offer):
    offer(rows=AMAZN_ROWS, resolves=lambda h: h == "amazon.com")
    got = await did_you_mean.suggest_sites("amazn.com")
    assert [s.host for s in got] == ["amazon.com"]


async def test_a_synthesised_host_that_does_not_resolve_is_never_offered(offer):
    """The _validated_question rule, and the reason tier 2 is allowed to build a
    host at all: DNS is what turns a guess into a fact."""
    offer(rows=AMAZN_ROWS, resolves=False)
    assert await did_you_mean.suggest_sites("amazn.com") == []


# ================================ the correction must survive a replan (08-02)
# _apply_site_correction re-points the steps that are PENDING when the user
# answers. It never touches plan.goal — and ground_origins reads the goal, so
# the dead host stays permanently GROUNDED and _browse_origin_violation will
# happily let a re-drafted step aim straight back at it. revise DROPS and
# re-drafts pending steps, which is exactly when that happens.
def test_a_correction_is_recorded_even_with_no_step_to_repoint():
    """The question gate asks at DRAFT time, when the plan has no steps at all.
    The mapping still has to be remembered, or the next draft is unguarded."""
    plan = AgentPlan(goal="openjunetjamshed.com")

    assert planner_mod._apply_site_correction(
        plan, "openjunetjamshed.com", "junaidjamshed.com"
    ) is False  # nothing to re-point yet…
    # …but the plan now knows which address is dead.
    assert plan.site_corrections_applied == {
        "openjunetjamshed.com": "junaidjamshed.com"
    }
    assert "junaidjamshed.com" in plan.approved_origins


def test_a_redrafted_step_is_repointed_off_the_dead_host():
    """THE RESURRECTION, CLOSED. A revise round re-drafts from the goal string,
    which still says the misheard address. Code moves it back."""
    plan = AgentPlan(goal="Go to junitjamsheed.com and add the perfume")
    plan.site_corrections_applied = {"junitjamsheed.com": "junaidjamshed.com"}
    plan.steps = [
        planner_mod.PlanStep(
            description="Open junitjamsheed.com and add the perfume",
            tool="browse",
            parameters={
                "goal": "add the perfume",
                "start_url": "https://junitjamsheed.com",
                "allowed_origins": ["junitjamsheed.com"],
            },
            permission_level=planner_mod.PermissionLevel.READ,
            requires_approval=False,
        )
    ]

    planner_mod._inject_site_corrections(plan)

    step = plan.steps[0]
    assert step.parameters["start_url"] == "https://junaidjamshed.com/"
    assert step.parameters["allowed_origins"] == ["junaidjamshed.com"]
    # The approval card quotes the address out of the LLM's own sentence, so the
    # prose has to move with the contract — a card that names one site while
    # acting on another is a card the user cannot rely on (2026-08-01).
    assert "junitjamsheed.com" not in step.description
    assert "junaidjamshed.com" in step.description


def test_injection_leaves_an_unrelated_step_alone():
    plan = AgentPlan(goal="Go to junitjamsheed.com")
    plan.site_corrections_applied = {"junitjamsheed.com": "junaidjamshed.com"}
    plan.steps = [
        planner_mod.PlanStep(
            description="Open daraz",
            tool="browse",
            parameters={
                "goal": "look",
                "start_url": "https://daraz.pk",
                "allowed_origins": ["daraz.pk"],
            },
            permission_level=planner_mod.PermissionLevel.READ,
            requires_approval=False,
        )
    ]

    planner_mod._inject_site_corrections(plan)

    assert plan.steps[0].parameters["start_url"] == "https://daraz.pk"
    assert plan.steps[0].parameters["allowed_origins"] == ["daraz.pk"]


def test_injection_never_re_aims_an_approval_bound_step():
    """A step already carrying a commit contract is what the user said yes to.
    Re-pointing it would change the approved action — the stamp_start_url rule."""
    plan = AgentPlan(goal="Go to junitjamsheed.com and buy it")
    plan.site_corrections_applied = {"junitjamsheed.com": "junaidjamshed.com"}
    plan.steps = [
        planner_mod.PlanStep(
            description="Submit the form",
            tool="browse_commit",
            parameters={
                "goal": "buy it",
                "start_url": "https://junitjamsheed.com",
                "allowed_origins": ["junitjamsheed.com"],
                "_commit": {"method": "POST", "url": "https://junitjamsheed.com/cart/add"},
            },
            permission_level=planner_mod.PermissionLevel.DESTRUCTIVE,
            requires_approval=True,
        )
    ]

    planner_mod._inject_site_corrections(plan)

    assert plan.steps[0].parameters["start_url"] == "https://junitjamsheed.com"


def test_the_correction_record_survives_the_park():
    """The ask PARKS the plan. A correction forgotten across the round trip is
    the bug this record exists to close."""
    plan = AgentPlan(goal="g")
    plan.site_corrections_applied = {"junitjamsheed.com": "junaidjamshed.com"}

    restored = AgentPlan.model_validate(plan.model_dump())

    assert restored.site_corrections_applied == {
        "junitjamsheed.com": "junaidjamshed.com"
    }

    older = AgentPlan(goal="g").model_dump()
    older.pop("site_corrections_applied", None)
    assert AgentPlan.model_validate(older).site_corrections_applied == {}


# ============================ the DRAFT-TIME ask, end to end (2026-08-02)
# The 2026-08-01 flow above is the model DRAFTING a browse step and the browser
# reporting NXDOMAIN. The 2026-08-02 incident is the other shape entirely: the
# model asked a QUESTION instead of drafting anything, so nothing ever
# navigated, so none of the machinery above could run. The stored plan proves
# it — "site_corrections": 0 — and the user was handed two addresses that do not
# exist.
_QUESTION_DRAFT = json.dumps(
    {
        "steps": [],
        "question": {
            "text": (
                "Did you mean to open junetjamshed.com, or is the site literally "
                "openjunetjamshed.com?"
            ),
            "options": ["https://junetjamshed.com", "https://openjunetjamshed.com"],
        },
    }
)


async def test_a_draft_time_question_never_offers_an_address_that_does_not_exist(
    db_session, offer
):
    """THE 2026-08-02 INCIDENT, END TO END, through the real graph."""
    offer(rows=GLUED_ROWS, resolves=lambda h: h == "junaidjamshed.com")
    provider = FakeProvider([_QUESTION_DRAFT] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-dym-draft")

    plan = await planner.start("openjunetjamshed.com")

    assert plan.status == PlanStatus.AWAITING_CHOICE
    assert plan.question is not None
    # The fabrications never reach the user…
    assert not any("junetjamshed.com" in o for o in plan.question.options)
    # …and what replaced them is the site a real search found and DNS confirmed.
    assert "junaidjamshed.com" in plan.question.options
    assert plan.question.kind == "site_correction"
    # The reply will be decided in code, not handed to a revise round as prose.
    assert plan.pending_site_correction == "openjunetjamshed.com"
    assert plan.pending_site_candidates == ["junaidjamshed.com"]


async def test_answering_the_draft_time_question_records_the_dead_address(
    db_session, offer
):
    """There is no step to re-point yet, so the answer flows to revise — where
    it is authoritative. What must NOT be lost is the knowledge that the goal's
    own address is dead, because the goal string keeps saying it."""
    offer(rows=GLUED_ROWS, resolves=lambda h: h == "junaidjamshed.com")
    provider = FakeProvider(
        [_QUESTION_DRAFT, plan_json([_browse_step(
            start="https://junaidjamshed.com", origins=("junaidjamshed.com",)
        )])] * 2
    )
    planner = AgentPlanner(db_session, provider, session_id="s-dym-draft2")

    plan = await planner.start("openjunetjamshed.com")
    assert plan.status == PlanStatus.AWAITING_CHOICE

    plan = await planner.answer(plan, "junaidjamshed.com")

    assert plan.site_corrections_applied == {
        "openjunetjamshed.com": "junaidjamshed.com"
    }
    assert "junaidjamshed.com" in plan.approved_origins


async def test_a_dead_address_the_user_declines_stops_honestly(db_session, offer):
    """Fail-closed, exactly like the origin approval: "none of these" means we
    do not guess, and nothing was opened."""
    offer(rows=GLUED_ROWS, resolves=lambda h: h == "junaidjamshed.com")
    provider = FakeProvider([_QUESTION_DRAFT] * 3)
    planner = AgentPlanner(db_session, provider, session_id="s-dym-draft3")

    plan = await planner.start("openjunetjamshed.com")
    plan = await planner.answer(plan, "No — none of these")

    assert plan.status == PlanStatus.CANCELLED
    assert "won't guess which site you meant" in plan.message

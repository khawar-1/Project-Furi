"""
An address offered as a question option must EXIST (2026-08-02).

THE INCIDENT, spoken into the mic:
    "open junaidjamshed.com"
transcribed as
    "openjunetjamshed.com"        <- "open" GLUED to the host; speech has no spaces

At DRAFT time — before any browser existed — the planner asked a perfectly
sensible question and offered two addresses as clickable options:

    Did you mean to open junetjamshed.com, or is the site literally
    openjunetjamshed.com?
      1. https://junetjamshed.com
      2. https://openjunetjamshed.com

NEITHER EXISTS. The user clicked into a dead end and had to work the real domain
out themselves. Two rules that already existed both stopped one step short:

  - _option_is_dead_path enforces "an option written as a concrete thing must
    EXIST" (2026-07-10, after a draft offered two invented PATHS and the plan
    died on the one the user clicked). Its first line is a filesystem-path
    regex, so its whole notion of "concrete thing" is a path.
  - did_you_mean is search-backed, similarity-filtered and DNS-verified, and is
    MEASURED right on this exact input — but it is wired only to a live
    navigation NXDOMAIN. The model asked INSTEAD of drafting a browse step, so
    nothing navigated and it never ran. The stored plan proves it:
    "site_corrections": 0. question_gate.self_resolve could not cover it either
    — its first line passes on any question with more than one option, and a
    "did you mean A or B?" always has two.

What is pinned here: the dead options never reach the user, the answer comes
from the real suggester instead, and — the part that matters most — every way
this must NOT fire.
"""
import pytest

import app.tools  # noqa: F401 — registers the real tools
from app.agents import planner as planner_mod
from app.agents.planner import _verified_site_question
from app.agents.schemas import PlanQuestion
from app.browser import did_you_mean


GOAL = "openjunetjamshed.com"

# The two options the model actually offered, verbatim from the stored plan.
INCIDENT_OPTIONS = ["https://junetjamshed.com", "https://openjunetjamshed.com"]

# The live provider's answer to a search for "openjunetjamshed", captured
# 2026-08-02. junaidjamshed.com is 7th of 8 — behind YouTube, a reseller,
# Wikipedia, the BBC and a blog. Taking the top hit would offer YouTube.
GLUED_ROWS = [
    {"url": "https://www.youtube.com/watch?v=lkryrHfgmns",
     "title": "Grand Opening of Junaid Jamshed Store New Jersey, Mixed Reactions of JJ Fans"},
    {"url": "https://www.youtube.com/watch?v=GKbnaRbKSYU",
     "title": "J.  Junaid Jamshed Grand Opening Ceremony | iTVusa"},
    {"url": "https://www.786shop.com/brands/junaid-jamshed",
     "title": "Junaid Jamshed | Buy Dresses Online in USA"},
    {"url": "https://en.wikipedia.org/wiki/Junaid_Jamshed", "title": "Junaid Jamshed"},
    {"url": "https://www.bbc.com/news/world-asia-38246109",
     "title": "Junaid Jamshed: Pakistan's pop icon turned preacher - BBC News"},
    {"url": "https://muslimmatters.org/2016/12/08/junaid-jamshed-inspired-a-generation",
     "title": "Junaid Jamshed Inspired A Generation of Struggling Souls - MuslimMatters.org"},
    {"url": "https://www.junaidjamshed.com", "title": "J. Junaid Jamshed Official Website"},
    {"url": "https://us.junaidjamshed.com", "title": "J. Junaid Jamshed US"},
]


@pytest.fixture
def lookup(monkeypatch):
    """Point the suggester at fixed rows and a resolver with a fixed opinion of
    which hosts exist. Nothing here touches the network."""

    def _install(rows=GLUED_ROWS, alive=("junaidjamshed.com", "jamshed.com")):
        calls = {"search": 0, "resolve": []}

        async def _search(query, limit):
            calls["search"] += 1
            calls.setdefault("queries", []).append(query)
            return list(rows)

        async def _resolve(host):
            calls["resolve"].append(host)
            return host in alive

        monkeypatch.setattr(did_you_mean, "SEARCH_FACTORY", _search)
        monkeypatch.setattr(did_you_mean, "RESOLVER_FACTORY", _resolve)
        return calls

    return _install


def _question(options=INCIDENT_OPTIONS, text="Did you mean to open junetjamshed.com?"):
    return PlanQuestion(text=text, options=list(options))


# ============================================================ the incident
async def test_the_incident_is_answered_instead_of_asked(lookup):
    """THE INCIDENT, FROZEN. Both offered addresses are dead, so the question is
    replaced by the code-authored "did you mean…?" carrying the site the real
    suggester found and DNS-verified."""
    lookup()
    got, error = await _verified_site_question(_question(), GOAL, attempt=1)

    assert error is None
    assert got is not None
    assert "junaidjamshed.com" in got.options
    # The fabrications are gone — neither survives into what the user sees.
    assert not any("junetjamshed.com" in o for o in got.options)
    assert got.kind == "site_correction"
    assert got.about_host == "openjunetjamshed.com"
    assert "doesn't exist" in got.text


async def test_the_replacement_arms_the_deterministic_answer_path(lookup):
    """about_host is what lets _pause_on_question arm pending_site_correction,
    so the reply is decided by _match_site_choice in code (fail-closed) rather
    than handed to a revise round as free text."""
    lookup()
    got, _ = await _verified_site_question(_question(), GOAL, attempt=1)

    plan = planner_mod.AgentPlan(goal=GOAL)
    planner_mod.AgentPlanner._pause_on_question(plan, got)

    assert plan.pending_site_correction == "openjunetjamshed.com"
    assert plan.pending_site_candidates == ["junaidjamshed.com"]
    # "No — none of these" is not an address and is never offered as a candidate.
    assert "No — none of these" in got.options
    assert "No — none of these" not in plan.pending_site_candidates


async def test_the_suggester_is_asked_about_the_address_the_GOAL_named(lookup):
    """Both offered hosts are dead, but only one of them is the address the
    user's own words named. A suggestion for a host the MODEL invented would be
    a guess about a guess."""
    calls = lookup()
    await _verified_site_question(_question(), GOAL, attempt=1)

    # The search query is the grounded host's name, not the model's other guess.
    assert calls["queries"] == ["openjunetjamshed"]


# ==================================================== when nothing is found
async def test_attempt_one_is_rejected_with_feedback_when_nothing_is_found(lookup):
    """No verified alternative: the first attempt is rejected outright and the
    feedback tells the model to draft the browse step anyway — the browser
    reports an unresolvable host and the VERIFIED ask happens there."""
    lookup(alive=())
    got, error = await _verified_site_question(_question(), GOAL, attempt=1)

    assert got is None
    assert error is not None
    assert "do not exist" in error
    assert "https://junetjamshed.com" in error


async def test_attempt_two_keeps_an_honest_question_without_the_fabrications(lookup):
    """An honest free-form question beats fabricated clickable facts — the
    _option_is_dead_path rule, unchanged."""
    lookup(alive=())
    got, error = await _verified_site_question(_question(), GOAL, attempt=2)

    assert error is None
    assert got is not None
    assert got.options == []
    assert got.text  # the question itself survives; only the dead options go


# =========================================================== partial death
async def test_a_real_address_survives_and_only_the_dead_one_is_dropped(lookup):
    """Some addresses are real: drop the fabrications and let the user choose
    between the truths. No suggester call is needed or made."""
    calls = lookup(alive=("junaidjamshed.com",))
    question = _question(["https://junaidjamshed.com", "https://openjunetjamshed.com"])
    got, error = await _verified_site_question(question, GOAL, attempt=1)

    assert error is None
    assert got.options == ["https://junaidjamshed.com"]
    assert calls["search"] == 0


# ===================================================== every way it must NOT fire
@pytest.mark.parametrize(
    "options",
    [
        ["Yes — send \"the Junaid Jamshed website\"", "No — don't"],
        ["March 4", "March 5"],
        [r"C:\Users\DELL\Desktop", r"D:\Desktop"],
        ["report.txt", "notes.md"],
        ["the one on d drive", "the other one"],
        [],
    ],
)
async def test_a_question_without_addresses_costs_nothing(lookup, options):
    """The first check is a regex over the options. A path question, a date
    question, a yes/no — none of them may pay a DNS lookup, and `report.txt`
    has the same SHAPE as a domain (the 2026-08-01 lesson) so it is the case
    that matters most."""
    calls = lookup()
    question = _question(options)
    got, error = await _verified_site_question(question, "some goal", attempt=1)

    assert error is None
    assert got is question
    assert got.options == options
    assert calls["resolve"] == []
    assert calls["search"] == 0


async def test_addresses_that_all_resolve_are_left_exactly_alone(lookup):
    """Two real sites IS the legitimate rule-11 choice. Verification confirms
    them and changes nothing."""
    calls = lookup(alive=("junaidjamshed.com", "daraz.pk"))
    question = _question(["junaidjamshed.com", "daraz.pk"])
    got, error = await _verified_site_question(question, "shop somewhere", attempt=1)

    assert error is None
    assert got.options == ["junaidjamshed.com", "daraz.pk"]
    assert calls["search"] == 0


async def test_a_verification_failure_leaves_the_question_untouched(monkeypatch):
    """Best-effort in one direction only: if DNS itself blows up we ask the
    question we were going to ask. An optional check can never break a plan."""

    async def _boom(hosts):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(did_you_mean, "verify_hosts", _boom)
    question = _question()
    got, error = await _verified_site_question(question, GOAL, attempt=1)

    assert error is None
    assert got is question
    assert got.options == INCIDENT_OPTIONS


async def test_a_suggester_failure_still_strips_the_dead_options(lookup, monkeypatch):
    """The lookup is optional; the fabrication is not. If suggest_sites raises,
    attempt 1 still rejects rather than shipping addresses that do not exist."""
    lookup(alive=())

    async def _boom(host, **kw):
        raise RuntimeError("search exploded")

    monkeypatch.setattr(did_you_mean, "suggest_sites", _boom)
    got, error = await _verified_site_question(_question(), GOAL, attempt=1)

    assert got is None
    assert "do not exist" in error


# ============================================ what counts as an address option
@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://junaidjamshed.com", "junaidjamshed.com"),
        ("http://www.junaidjamshed.com/", "junaidjamshed.com"),
        ("junaidjamshed.com", "junaidjamshed.com"),
        ("us.junaidjamshed.com", "junaidjamshed.com"),
        ("outfitters.com.pk", "outfitters.com.pk"),
        # NOT addresses — the same shape, a different thing.
        ("report.txt", ""),
        ("index.html", ""),
        ("notes.md", ""),
        ("junaidjamshed", ""),
        ("No — none of these", ""),
        ("March 4", ""),
        (r"C:\Users\DELL\Desktop", ""),
        # Prose that merely MENTIONS a domain is prose, not an address option.
        ("Open junaidjamshed.com in a browser", ""),
    ],
)
def test_option_host_reads_only_a_whole_address(text, expected):
    assert did_you_mean.option_host(text) == expected


async def test_verify_hosts_is_bounded_and_drops_what_it_cannot_check(monkeypatch):
    """A lookup that errors is NOT verified — a failure can only ever drop an
    option, never add one."""
    seen = []

    async def _resolve(host):
        seen.append(host)
        if host == "boom.com":
            raise RuntimeError("dns exploded")
        return host == "real.com"

    monkeypatch.setattr(did_you_mean, "RESOLVER_FACTORY", _resolve)
    alive = await did_you_mean.verify_hosts(
        ["real.com", "dead.com", "boom.com", "real.com"]
    )

    assert alive == {"real.com"}
    assert seen.count("real.com") == 1  # deduped before any lookup


async def test_verify_hosts_never_resolves_more_than_its_cap(monkeypatch):
    """A clarifying question offers a handful of options; anything past that is
    not a question. The bound is what keeps a malformed draft from turning into
    a DNS sweep."""
    seen = []

    async def _resolve(host):
        seen.append(host)
        return False

    monkeypatch.setattr(did_you_mean, "RESOLVER_FACTORY", _resolve)
    await did_you_mean.verify_hosts([f"h{i}.com" for i in range(40)])

    assert len(seen) == did_you_mean.MAX_VERIFY_HOSTS

"""
Phase 14 Part 2 — browse origin grounding.

The exfiltration bound: a browse loop may visit ONLY sites the USER named, never
one a page steered it toward. These pin that the grounded set comes from the
user's words alone and that the planner rejects an ungrounded origin — the
navigation mirror of the recipient lock.
"""
from app.agents import browser_grounding
from app.agents.browser_grounding import (
    ground_origins,
    origin_is_grounded,
    ungrounded_origin,
)
from app.agents.planner import _browse_origin_violation
from app.agents.schemas import AgentPlan
from app.core.base_tool import PermissionLevel
from app.agents.schemas import PlanStep


def _browse_step(**params) -> PlanStep:
    return PlanStep(
        description="browse",
        tool="browse",
        parameters=params,
        permission_level=PermissionLevel.READ,
        requires_approval=False,
    )


# --------------------------------------------------------------- grounding
def test_a_domain_written_verbatim_is_grounded():
    assert ground_origins("open youtube.com and search") == {"youtube.com"}
    assert "linkedin.com" in ground_origins("go to www.linkedin.com/jobs")


def test_a_bare_site_name_maps_to_its_origin():
    assert ground_origins("play a song on youtube") == {"youtube.com"}
    assert "linkedin.com" in ground_origins("find jobs on linkedin")


def test_grounding_reads_conversation_and_answers_too():
    grounded = ground_origins(
        "search for it", conversation="I like using youtube", user_answers=["on reddit"]
    )
    assert "youtube.com" in grounded
    assert "reddit.com" in grounded


def test_an_unknown_site_named_only_by_word_is_not_grounded():
    """The known-sites map fails CLOSED: a site neither in the map nor written as
    a domain is simply ungrounded, so the planner must ask or the user must name
    the domain."""
    assert ground_origins("play something on someobscuresite") == set()


def test_a_page_cannot_ground_an_origin():
    """The whole point: page content is never an input to ground_origins. An
    origin that appears ONLY in what a page said is not in the grounded set."""
    grounded = ground_origins("open youtube", conversation="", user_answers=())
    assert not origin_is_grounded("attacker.com", grounded)


# ------------------------------------------------------------- matching
def test_subdomains_match_but_lookalikes_do_not():
    grounded = {"youtube.com"}
    assert origin_is_grounded("www.youtube.com", grounded)
    assert origin_is_grounded("m.youtube.com", grounded)
    assert origin_is_grounded("https://youtube.com/watch?v=x", grounded)
    assert not origin_is_grounded("evil-youtube.com", grounded)
    assert not origin_is_grounded("youtube.com.attacker.net", grounded)
    assert not origin_is_grounded("", grounded)


def test_a_registrable_origin_matches_a_grounded_subdomain():
    """The user wrote 'news.ycombinator.com'; a step asking for 'ycombinator.com'
    is still within it."""
    grounded = {"news.ycombinator.com"}
    assert origin_is_grounded("news.ycombinator.com", grounded)


# ------------------------------------------------------------- step check
def test_ungrounded_origin_checks_allowed_origins_and_start_url():
    grounded = {"youtube.com"}
    assert ungrounded_origin({"allowed_origins": ["youtube.com"],
                              "start_url": "https://www.youtube.com"}, grounded) is None
    assert ungrounded_origin({"allowed_origins": ["attacker.com"],
                              "start_url": "https://youtube.com"}, grounded) == "attacker.com"
    # start_url itself must be grounded, even if allowed_origins is clean.
    assert ungrounded_origin({"allowed_origins": ["youtube.com"],
                              "start_url": "https://attacker.com"}, grounded) == "attacker.com"


# -------------------------------------------------- the planner violation
def test_planner_rejects_a_browse_to_an_ungrounded_site():
    grounded = {"youtube.com"}
    steps = [_browse_step(goal="play music", start_url="https://youtube.com",
                          allowed_origins=["attacker.com"])]
    reject = _browse_origin_violation(steps, grounded)
    assert reject is not None
    assert "attacker.com" in reject
    assert "not one the user named" in reject


def test_planner_allows_a_browse_to_a_grounded_site():
    grounded = {"youtube.com"}
    steps = [_browse_step(goal="play music", start_url="https://www.youtube.com",
                          allowed_origins=["youtube.com"])]
    assert _browse_origin_violation(steps, grounded) is None


def test_non_browse_steps_are_ignored_by_the_guard():
    """The guard is scoped to browse tools; a search_files step with a stray
    'allowed_origins' param (there is none, but be defensive) is never checked."""
    other = PlanStep(
        description="search", tool="search_files",
        parameters={"allowed_origins": ["attacker.com"]},
        permission_level=PermissionLevel.READ, requires_approval=False,
    )
    assert _browse_origin_violation([other], set()) is None


def test_browse_grounding_pulls_from_goal_and_answers():
    """_browse_grounding is what the planner actually calls — grounded from the
    plan's own goal + user answers."""
    from app.agents.planner import _browse_grounding

    plan = AgentPlan(goal="play jane by the long faces on youtube")
    plan.user_answers.append("use spotify")
    grounded = _browse_grounding(plan, conversation="")
    assert "youtube.com" in grounded
    assert "spotify.com" in grounded

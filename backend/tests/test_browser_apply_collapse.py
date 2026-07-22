"""
Plan RULE 22 in code, and the read-only downgrade guard (2026-07-19).

The live failure: "Go to weworkremotely.com, find the three most recent senior
Python backend roles … and apply to each one." The planner drafted a `browse`
(find) step + a `browse_commit` (apply) step — the exact split rule 22 forbids —
so the two ran as DISCONNECTED browser sessions: the apply step launched a fresh
Chrome on the homepage with no idea which jobs to apply to, and Chrome flickered
open/closed per step. The replan then downgraded to `read_webpage`, which 403s on
that bot-protected site and cannot act anyway.

These pin the pure helpers that fix it:
  - `_collapse_browse_apply` folds the split into ONE browse_commit(max_commits=N);
  - `_browse_downgrade_violation` refuses a read-only web tool in a browse task;
  - the count / goal-shape parsers that feed them.

Pure-unit by design (the collapse is deterministic and needs no browser): a
graph-level test would have to launch a real Chromium, which the suite refuses.
"""
from app.agents import planner as P
from app.agents.schemas import PlanStep
from app.core.base_tool import PermissionLevel


def _find(tool: str, **params) -> PlanStep:
    return PlanStep(
        description=f"{tool} step",
        tool=tool,
        parameters=params,
        permission_level=PermissionLevel.READ,
        requires_approval=False,
    )


def _commit(**params) -> PlanStep:
    return PlanStep(
        description="Apply to each of the three jobs",
        tool="browse_commit",
        parameters=params,
        permission_level=PermissionLevel.DESTRUCTIVE,
        requires_approval=True,
    )


# --------------------------------------------------------------- the incident

def test_browse_find_plus_commit_same_site_collapses_to_one():
    """The exact WWR shape: a browse 'find' step feeding a same-site
    browse_commit becomes ONE browse_commit — one live session, no flicker."""
    steps = [
        _find("browse", goal="Find the three roles",
              start_url="https://weworkremotely.com",
              allowed_origins=["weworkremotely.com"]),
        _commit(goal="Apply to each of the three senior Python backend roles",
                start_url="https://weworkremotely.com",
                allowed_origins=["weworkremotely.com"]),
    ]
    out = P._collapse_browse_apply(steps)
    assert len(out) == 1
    assert out[0].tool == "browse_commit"
    # N parsed from the commit goal ("three … roles").
    assert out[0].parameters["max_commits"] == 3
    assert out[0].parameters["start_url"] == "https://weworkremotely.com"
    assert out[0].parameters.get("keep_open") is True


def test_web_search_find_is_folded_in_too():
    """A web_search feeder (no origin of its own) preceding a browse_commit is a
    find-for-the-commit — folded in, not left to trip the downgrade guard."""
    steps = [
        _find("web_search", query="senior python backend jobs weworkremotely"),
        _commit(goal="apply to the 2 roles",
                start_url="https://weworkremotely.com",
                allowed_origins=["weworkremotely.com"]),
    ]
    out = P._collapse_browse_apply(steps)
    assert [s.tool for s in out] == ["browse_commit"]
    assert out[0].parameters["max_commits"] == 2


def test_multiple_same_site_commits_merge_to_one():
    """The other forbidden shape — one browse_commit per item — also collapses,
    max_commits taking the count of merged submits."""
    steps = [
        _commit(goal="apply to job", start_url="https://x.com",
                allowed_origins=["x.com"]),
        _commit(goal="apply to job", start_url="https://x.com",
                allowed_origins=["x.com"]),
        _commit(goal="apply to job", start_url="https://x.com",
                allowed_origins=["x.com"]),
    ]
    out = P._collapse_browse_apply(steps)
    assert len(out) == 1
    assert out[0].parameters["max_commits"] == 3


def test_lone_commit_is_left_alone():
    """A single browse_commit with no feeder is a legitimate one-off submit."""
    steps = [_commit(goal="submit the contact form",
                     start_url="https://x.com", allowed_origins=["x.com"])]
    out = P._collapse_browse_apply(steps)
    assert len(out) == 1
    assert out[0] is steps[0]  # untouched


def test_different_site_find_is_not_collapsed():
    """A browse reading site A + a browse_commit on site B are distinct intents —
    the same-site guard keeps them separate."""
    steps = [
        _find("browse", goal="read A", start_url="https://a.com",
              allowed_origins=["a.com"]),
        _commit(goal="submit on B", start_url="https://b.com",
                allowed_origins=["b.com"]),
    ]
    out = P._collapse_browse_apply(steps)
    assert [s.tool for s in out] == ["browse", "browse_commit"]


def test_collapse_merges_origins_and_caps_max_commits():
    steps = [
        _find("browse", start_url="https://jobs.example.com",
              allowed_origins=["example.com"]),
        _commit(goal="apply to the twelve roles",
                start_url="https://example.com",
                allowed_origins=["example.com"], max_commits=1),
    ]
    out = P._collapse_browse_apply(steps)
    # "twelve" is not in the small-count map, so N falls back to the drafted
    # value — but a hostile large count would still be clamped to the cap.
    assert out[0].parameters["max_commits"] <= P._COLLAPSE_MAX_COMMITS
    assert "example.com" in out[0].parameters["allowed_origins"]


# ------------------------------------------------------------ count parsing

def test_parse_target_count():
    assert P._parse_target_count("three most recent senior Python backend roles") == 3
    assert P._parse_target_count("apply to the 3 jobs") == 3
    assert P._parse_target_count("submit all 5 applications") == 5
    assert P._parse_target_count("apply to the job") is None
    assert P._parse_target_count("") is None


# ------------------------------------------------------------ goal shape seed

def test_looks_like_browse_goal():
    assert P._looks_like_browse_goal(
        "Go to weworkremotely.com and apply to the three python jobs")
    assert P._looks_like_browse_goal("sign in to github.com and open my repo")
    # No submit verb → a read tool is legitimate.
    assert not P._looks_like_browse_goal("watch the trailer on youtube.com")
    # Submit verb but no named site → not grounded as a browse task.
    assert not P._looks_like_browse_goal("apply for the job")


# ------------------------------------------------------ downgrade guard

def test_downgrade_guard_blocks_read_webpage_in_browse_task():
    steps = [_find("read_webpage", url="https://weworkremotely.com/jobs")]
    msg = P._browse_downgrade_violation(steps, is_browse_task=True)
    assert msg and "read_webpage" in msg
    for bad in ("browse_page", "web_search"):
        assert P._browse_downgrade_violation([_find(bad)], is_browse_task=True)


def test_downgrade_guard_silent_when_not_a_browse_task():
    steps = [_find("read_webpage", url="https://x.com")]
    assert P._browse_downgrade_violation(steps, is_browse_task=False) is None


def test_downgrade_guard_allows_the_browser_tools():
    steps = [
        _find("browse", start_url="https://x.com", allowed_origins=["x.com"]),
        _commit(goal="apply", start_url="https://x.com", allowed_origins=["x.com"]),
    ]
    assert P._browse_downgrade_violation(steps, is_browse_task=True) is None


def test_has_browse_action():
    assert P._has_browse_action([_commit(goal="g")])
    assert P._has_browse_action([_find("browse")])
    assert not P._has_browse_action([_find("read_webpage")])


# ------------------------------- journey collapse (2026-07-21, read-only)
# The books.toscrape incident: one continuous navigation drafted as THREE browse
# steps, each launching its own Chrome — step 2 re-did step 1's navigation and
# step 3's `back` had no history. Consecutive same-site browse steps are ONE
# journey in ONE session.

def _browse_step(goal, desc=None, **params):
    params.setdefault("start_url", "https://books.toscrape.com")
    params.setdefault("allowed_origins", ["books.toscrape.com"])
    return PlanStep(
        description=desc or goal,
        tool="browse",
        parameters={"goal": goal, **params},
        permission_level=PermissionLevel.READ,
        requires_approval=False,
    )


def test_the_book_plan_collapses_to_one_journey():
    """The incident shape frozen: 3 same-site browse steps → ONE browse whose
    goal is the joined journey, origins/start_url carried."""
    steps = [
        _browse_step("Navigate to the homepage and click the Travel category"),
        _browse_step("Click the top book listed to view its details and price"),
        _browse_step("Use the back button to return to the category list"),
    ]
    out = P._collapse_browse_journey(steps)
    assert len(out) == 1
    assert out[0].tool == "browse"
    goal = out[0].parameters["goal"]
    assert "Travel category" in goal and "top book" in goal and "back button" in goal
    assert goal.count(", then ") == 2                       # order preserved
    assert out[0].parameters["start_url"] == "https://books.toscrape.com"
    assert out[0].parameters["allowed_origins"] == ["books.toscrape.com"]


def test_different_site_browse_steps_do_not_fold():
    steps = [
        _browse_step("read A", start_url="https://a.com", allowed_origins=["a.com"]),
        _browse_step("read B", start_url="https://b.com", allowed_origins=["b.com"]),
    ]
    out = P._collapse_browse_journey(steps)
    assert len(out) == 2


def test_non_adjacent_browse_steps_do_not_fold():
    """A non-browse step between two browse steps is a real dependency boundary
    — nothing folds across it."""
    steps = [
        _browse_step("read the travel page"),
        _find("read_file", path="C:/notes.txt"),
        _browse_step("read the mystery page"),
    ]
    out = P._collapse_browse_journey(steps)
    assert [s.tool for s in out] == ["browse", "read_file", "browse"]


def test_a_single_browse_step_is_untouched():
    steps = [_browse_step("open the page")]
    out = P._collapse_browse_journey(steps)
    assert out[0] is steps[0]


def test_journey_collapse_ors_keep_open():
    """A journey ending in playback keeps the media hand-off."""
    steps = [
        _browse_step("search for jane", start_url="https://youtube.com",
                     allowed_origins=["youtube.com"]),
        _browse_step("play the top result", start_url="https://youtube.com",
                     allowed_origins=["youtube.com"], keep_open=True),
    ]
    out = P._collapse_browse_journey(steps)
    assert len(out) == 1
    assert out[0].parameters["keep_open"] is True


def test_journey_collapse_unions_subdomain_origins():
    """m.site.com and site.com are the same site (the dot-aware rule) — folded,
    with both origins carried."""
    steps = [
        _browse_step("search", start_url="https://www.linkedin.com",
                     allowed_origins=["linkedin.com"]),
        _browse_step("open the profile", start_url="https://m.linkedin.com",
                     allowed_origins=["m.linkedin.com"]),
    ]
    out = P._collapse_browse_journey(steps)
    assert len(out) == 1
    assert "linkedin.com" in out[0].parameters["allowed_origins"]
    assert "m.linkedin.com" in out[0].parameters["allowed_origins"]


def test_pending_origin_browse_steps_do_not_fold():
    """A step whose sites are still PENDING placeholders is unknowable — left
    exactly as drafted."""
    steps = [
        _browse_step("read the page"),
        _browse_step("open the found site", start_url="PENDING: the url step 1 finds",
                     allowed_origins=[]),
    ]
    out = P._collapse_browse_journey(steps)
    assert len(out) == 2

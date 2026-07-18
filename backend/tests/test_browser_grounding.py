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
    upload_path_is_grounded,
    upload_path_unsafe,
    upload_violation,
)
from app.agents.planner import _browse_origin_violation, _upload_path_violation
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


def _commit_step(**params) -> PlanStep:
    return PlanStep(
        description="upload",
        tool="browse_commit",
        parameters=params,
        permission_level=PermissionLevel.DESTRUCTIVE,
        requires_approval=True,
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


# ==================================================== 14.6 — file upload
# WHICH file leaves the machine is bounded by the user's own words (never a
# page) and by the file-tools' path safety (no system files) — the exfiltration
# bound of _browse_origin_violation, applied to a file path with a write behind it.

# --------------------------------------------------------------- grounding
def test_an_upload_file_the_user_named_is_grounded():
    assert upload_path_is_grounded(
        r"C:\Users\me\Desktop\resume.pdf", "upload my resume.pdf to example.com"
    )
    # A full path the user pasted grounds too.
    assert upload_path_is_grounded(
        "/home/me/report.docx",
        "attach it", conversation="the file is /home/me/report.docx",
    )
    # Named only in an answer.
    assert upload_path_is_grounded(
        r"D:\docs\cv.pdf", "upload the file", user_answers=["it's cv.pdf"]
    )


def test_a_file_the_page_named_is_not_grounded():
    """The whole point: a path that appears NOWHERE in the user's words — only a
    malicious page could have named it — never grounds. Page content is not an
    input here by construction."""
    assert not upload_path_is_grounded(
        r"C:\Users\me\.ssh\id_rsa", "upload my resume to example.com"
    )
    assert not upload_path_is_grounded("resume.pdf", "")           # empty corpus
    assert not upload_path_is_grounded("", "upload resume.pdf")    # empty path


# --------------------------------------------------------------- path safety
def test_upload_path_unsafe_flags_a_missing_file():
    reason = upload_path_unsafe(r"C:\definitely\nope\missing-file.pdf")
    assert reason is not None
    assert "does not exist" in reason


def test_upload_path_unsafe_flags_a_directory(tmp_path):
    reason = upload_path_unsafe(str(tmp_path))
    assert reason is not None
    assert "not a file" in reason


def test_upload_path_unsafe_flags_a_protected_system_path():
    """Reuses the file-tools' OWN _blocked_reason — a protected system dir is
    refused as an upload source exactly as every file tool refuses it."""
    from app.tools.file_tools import _PROTECTED

    if not _PROTECTED:  # non-Windows CI without the configured roots
        return
    protected = _PROTECTED[0] / "some-secret.dat"
    reason = upload_path_unsafe(str(protected))
    assert reason is not None
    assert "protected" in reason


def test_upload_path_unsafe_passes_a_real_file(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    assert upload_path_unsafe(str(f)) is None


# ------------------------------------------------- combined + planner gate
def test_upload_violation_none_when_no_upload_requested():
    assert upload_violation("", "any goal") is None


def test_upload_violation_rejects_an_ungrounded_file(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    # Real, safe file — but the user never named it: rejected on grounding.
    reason = upload_violation(str(f), "upload something to example.com")
    assert reason is not None
    assert "not one the user named" in reason


def test_upload_violation_rejects_a_grounded_but_unsafe_file():
    # Named by the user, but it does not exist — rejected on safety.
    reason = upload_violation(
        r"C:\nope\resume.pdf", "upload resume.pdf to example.com"
    )
    assert reason is not None
    assert "does not exist" in reason


def test_upload_violation_allows_a_grounded_safe_file(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    assert upload_violation(str(f), f"upload {f.name} to example.com") is None


def test_planner_rejects_an_ungrounded_upload(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    steps = [_commit_step(goal="upload a file", start_url="https://example.com/x",
                          upload_path=str(f))]
    # The corpus (goal) never names the file.
    reject = _upload_path_violation(steps, "upload a file to example.com")
    assert reject is not None
    assert str(f) in reject
    assert "not one the user named" in reject


def test_planner_allows_a_grounded_safe_upload(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    steps = [_commit_step(goal="upload it", start_url="https://example.com/x",
                          upload_path=str(f))]
    assert _upload_path_violation(steps, f"please upload {f.name} to example.com") is None


def test_upload_gate_ignores_non_commit_and_upload_less_steps(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_text("cv")
    # A browse_commit with no upload_path is not checked; a browse step with a
    # stray upload_path is not checked (the gate is scoped to browse_commit).
    plain_commit = _commit_step(goal="post a comment", start_url="https://example.com/x")
    browse = _browse_step(goal="x", upload_path=r"C:\Users\me\.ssh\id_rsa")
    assert _upload_path_violation([plain_commit, browse], "anything") is None

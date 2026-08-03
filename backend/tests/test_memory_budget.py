"""The MEMORY CONTEXT budget and decay ranking (2026-08-03, Tier 2 item 7).

⚠️ THIS MUST BE TESTED SYNTHETICALLY, and that is the point of the item. The
real database when this shipped held 19 semantic memories (953 chars in total),
46 contact interactions and ZERO episodes — nothing there can exercise a budget.
The defect was never the current size; it was that `format_context` had exactly
one `[:5]` and one `[:120]` in the whole function while the `PEOPLE YOU KNOW`
section rendered EVERY ContactInteraction for every bundled contact, and the
fact log is append-only by design. So these tests seed the year-three shape.
"""
from datetime import datetime, timedelta

import pytest

from app.memory.budget import (
    MAX_CONTACT_TEXT,
    MAX_FACTS_PER_CONTACT,
    MAX_PROFILE_FIELD,
    MEMORY_CONTEXT_TOTAL_CAP,
    MAX_SECTIONS,
    Section,
    clip_text,
    fit_sections,
)
from app.memory.decay import (
    HALF_LIFE_DAYS,
    RECENCY_WEIGHT,
    USAGE_WEIGHT,
    rank_score,
)
from app.memory.engine import MemoryEngine
from app.db.models import Contact, ContactInteraction, SemanticMemory, UserProfile


# ------------------------------------------------------------------ the budget


def test_a_normal_sized_memory_is_not_clipped_at_all():
    """The common case must cost nothing and explain nothing. Today's real
    volume is ~1k chars against a 6k cap."""
    sections = [
        Section("WHAT I KNOW ABOUT YOU:", [f"- fact {i}" for i in range(20)]),
        Section("YOUR PREFERENCES:", ["- likes tea"]),
    ]
    out = fit_sections(sections)
    assert [s.items for s in out] == [s.items for s in sections]
    assert not any("clipped" in i for s in out for i in s.items)


def test_an_empty_section_is_dropped_not_rendered_as_a_bare_header():
    out = fit_sections([Section("EMPTY:", []), Section("REAL:", ["- x"])])
    assert [s.title for s in out] == ["REAL:"]


def test_the_whole_block_stays_under_the_cap():
    sections = [
        Section(f"SECTION {n}:", [f"- {'x' * 300}" for _ in range(50)])
        for n in range(4)
    ]
    out = fit_sections(sections)
    total = sum(s.size() for s in out)
    assert total <= MEMORY_CONTEXT_TOTAL_CAP + len(sections) * 8  # join slack


def test_position_never_determines_survival():
    """⚠️ THE FAIR-SHARE PROPERTY, and the reason `rendering.fair_shares` is
    reused rather than re-implemented. A first-come-first-served cut spends the
    budget on whatever renders first and starves whatever renders last — and in
    `format_context` the last sections are PREFERENCES and PAST CONTEXT.

    ⚠️ THE SIZES HERE ARE LOAD-BEARING. The first version of this test used a
    16-char second section, and it PASSED against a deliberately-reverted
    first-come-first-served allocator — `_fit` reserves room for its marker, so
    the greedy section stopped short and left enough crumbs for a tiny one. A
    test that cannot fail on the defect proves nothing. The second section is
    now big enough to need a real share, which is the only thing fair-share
    gives it."""
    greedy = Section("GREEDY:", [f"- {'x' * 400}" for _ in range(100)])
    later = Section("LATER:", [f"- later line {i}" for i in range(60)])
    out = fit_sections([greedy, later])

    assert len(out) == 2, "a greedy first section starved the one after it"
    survivor = next(s for s in out if s.title == "LATER:")
    real_lines = [i for i in survivor.items if i.startswith("- ")]
    assert len(real_lines) >= 30, (
        f"the later section kept only {len(real_lines)} of 60 lines — it was "
        "served the first section's leftovers instead of its own share"
    )


def test_a_cut_is_always_marked_and_says_how_many_went():
    section = Section("BIG:", [f"- {'x' * 200}" for _ in range(200)])
    out = fit_sections([section])[0]
    assert "clipped for length" in out.items[-1]
    assert "more" in out.items[-1]
    # And it is cut by ITEM — no half-item is ever rendered.
    assert all(i.startswith("- ") or "clipped" in i for i in out.items)


def test_the_marker_never_pushes_the_section_back_over_budget():
    section = Section("BIG:", [f"- {'x' * 90}" for _ in range(400)])
    out = fit_sections([section], total=1000)[0]
    assert out.size() <= 1000


def test_a_section_is_never_squeezed_to_nothing():
    """A section that survives the allocation must be able to say something,
    otherwise its header is noise. The floor comes from `rendering.fair_shares`
    (`_MIN_STEP_SHARE`), which is why this module has no floor of its own — see
    the ⚠️ note on MAX_SECTIONS."""
    many = [Section(f"S{n}:", [f"- {'x' * 500}" for _ in range(20)]) for n in range(5)]
    out = fit_sections(many)
    for s in out:
        assert any("- " in i for i in s.items), f"{s.title} was cut to nothing"


def test_the_section_count_cannot_overflow_the_floor():
    """⚠️ THE INVARIANT THAT MAKES THE BORROWED FLOOR SAFE, and the test that
    caught a dead constant.

    `fair_shares` floors every share at `_MIN_STEP_SHARE`, so N sections are
    guaranteed at least N x 800 chars REGARDLESS of the total cap. That is fine
    only while N stays small. Adding a sixth section would silently let the
    floor beat the budget, which is exactly the kind of quiet overflow this
    whole item exists to prevent."""
    from app.agents.rendering import _MIN_STEP_SHARE

    assert MAX_SECTIONS * _MIN_STEP_SHARE <= MEMORY_CONTEXT_TOTAL_CAP


def test_format_context_emits_no_more_sections_than_declared():
    """The other half of the invariant above: the constant has to match the
    engine. Counted from the source so adding a section without revisiting the
    budget fails here."""
    import inspect

    from app.memory.engine import MemoryEngine

    source = inspect.getsource(MemoryEngine.format_context)
    assert source.count("sections.append(") <= MAX_SECTIONS


def test_clip_text_marks_the_cut_and_never_says_truncated():
    """"truncated" is a fact a TOOL reports about its own results. Reusing it
    for "our display budget ran out" is what let the record lie on 2026-07-29
    and killed a plan."""
    out = clip_text("y" * 900, 100)
    assert "clipped for length" in out
    assert "truncated" not in out
    assert clip_text("short", 100) == "short"
    assert clip_text("", 100) == ""


# ------------------------------------------------- the block, through the engine


async def _seed_heavy(db, *, contacts: int, facts_each: int, memories: int) -> None:
    for i in range(memories):
        db.add(SemanticMemory(
            content=f"The user fact number {i} " + "detail " * 8,
            subject="user", is_active=True,
        ))
    for c in range(contacts):
        contact = Contact(
            name=f"Person {c}", relationship_type="friend", is_active=True,
            summary="s" * 2000, notes="n" * 2000,
        )
        db.add(contact)
        await db.flush()
        for f in range(facts_each):
            db.add(ContactInteraction(
                contact_id=contact.id,
                description=f"fact {f} about person {c} " + "words " * 6,
                category="general",
                interaction_date=datetime(2026, 1, 1) + timedelta(days=f),
            ))
    await db.commit()


@pytest.mark.asyncio
async def test_the_year_three_shape_stays_under_budget(db_session):
    """⚠️ THE ONE THAT MATTERS. 50 contacts x 200 facts is what an append-only
    fact log looks like after a few years. Before the budget this rendered every
    one of those 10,000 lines into every prompt that mentioned any of them."""
    await _seed_heavy(db_session, contacts=50, facts_each=200, memories=500)
    engine = MemoryEngine(db_session, provider=None, qdrant=None)

    memories = (await db_session.execute(
        __import__("sqlalchemy").select(SemanticMemory).limit(5)
    )).scalars().all()
    contacts = (await db_session.execute(
        __import__("sqlalchemy").select(Contact)
    )).scalars().all()

    from app.memory.engine import RetrievedContext

    bundle = RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=list(memories), preferences=[], episodes=[],
        contacts=list(contacts),
    )
    block = await engine.format_context(bundle)

    assert len(block) <= MEMORY_CONTEXT_TOTAL_CAP + 500, (
        f"the rendered block was {len(block)} chars — the budget did not hold"
    )
    assert "PEOPLE YOU KNOW" in block


@pytest.mark.asyncio
async def test_only_the_most_recent_facts_per_contact_are_shown(db_session):
    """The fact log is oldest-first and the MEMORY RULES block tells the model
    the newest fact in a category is the current truth — so the tail is the half
    that rule assumes."""
    contact = Contact(name="Jamil", relationship_type="friend", is_active=True)
    db_session.add(contact)
    await db_session.flush()
    for f in range(40):
        db_session.add(ContactInteraction(
            contact_id=contact.id, description=f"FACT-{f:02d}", category="general",
            interaction_date=datetime(2026, 1, 1) + timedelta(days=f),
        ))
    await db_session.commit()

    from app.memory.engine import RetrievedContext

    engine = MemoryEngine(db_session, provider=None, qdrant=None)
    block = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=[], preferences=[], episodes=[], contacts=[contact],
    ))

    assert "FACT-39" in block, "the newest fact was dropped"
    assert "FACT-00" not in block, "an ancient fact survived the per-contact cap"
    assert "older fact(s) not shown" in block, "the drop was silent"
    shown = sum(1 for line in block.splitlines() if "FACT-" in line)
    assert shown == MAX_FACTS_PER_CONTACT


@pytest.mark.asyncio
async def test_free_text_contact_fields_are_clipped(db_session):
    contact = Contact(
        name="Ali", relationship_type="friend", is_active=True,
        summary="S" * 5000, notes="N" * 5000,
    )
    db_session.add(contact)
    await db_session.commit()

    from app.memory.engine import RetrievedContext

    engine = MemoryEngine(db_session, provider=None, qdrant=None)
    block = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=[], preferences=[], episodes=[], contacts=[contact],
    ))
    assert "S" * (MAX_CONTACT_TEXT + 50) not in block
    assert "clipped for length" in block


@pytest.mark.asyncio
async def test_unbounded_profile_fields_are_clipped(db_session):
    profile = UserProfile(name="Khawar", background="B" * 5000, work_style="W" * 5000)
    db_session.add(profile)
    await db_session.commit()

    from app.memory.engine import RetrievedContext

    engine = MemoryEngine(db_session, provider=None, qdrant=None)
    block = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=profile,
        semantic_memories=[], preferences=[], episodes=[], contacts=[],
    ))
    assert "B" * (MAX_PROFILE_FIELD + 50) not in block
    assert "Khawar" in block, "the identity itself must never be the thing clipped"


@pytest.mark.asyncio
async def test_an_empty_bundle_still_renders_nothing(db_session):
    from app.memory.engine import RetrievedContext

    engine = MemoryEngine(db_session, provider=None, qdrant=None)
    block = await engine.format_context(RetrievedContext(
        resolved_entities=[], pending_resolution=None, user_profile=None,
        semantic_memories=[], preferences=[], episodes=[], contacts=[],
    ))
    assert block == ""


# ------------------------------------------------------------------- the decay


def test_freshness_breaks_a_tie_between_equally_relevant_facts():
    now = datetime(2026, 8, 3)
    fresh = rank_score(0.80, created_at=now - timedelta(days=1), now=now)
    stale = rank_score(0.80, created_at=now - timedelta(days=900), now=now)
    assert fresh > stale


def test_relevance_still_dominates_freshness():
    """⚠️ THE PROPERTY TO RE-CHECK IF THE WEIGHTS EVER MOVE. Cosine over the
    retrieval threshold runs 0.5-1.0 and the boosts total 0.25, so a brand-new
    but barely-relevant fact must never outrank an old, highly-relevant one.
    A decay that could do that would be a silent deletion."""
    now = datetime(2026, 8, 3)
    old_and_relevant = rank_score(0.95, created_at=now - timedelta(days=2000), now=now)
    new_and_marginal = rank_score(0.55, created_at=now, last_used_at=now, now=now)
    assert old_and_relevant > new_and_marginal
    assert RECENCY_WEIGHT + USAGE_WEIGHT < 0.5


def test_a_fact_the_conversation_keeps_using_stays_up():
    """The difference between "old" and "stale"."""
    now = datetime(2026, 8, 3)
    born = now - timedelta(days=1000)
    used = rank_score(0.70, created_at=born, last_used_at=now, now=now)
    forgotten = rank_score(0.70, created_at=born, last_used_at=None, now=now)
    assert used > forgotten


def test_a_missing_timestamp_is_treated_as_old_never_as_fresh():
    now = datetime(2026, 8, 3)
    assert rank_score(0.7, created_at=None, now=now) == pytest.approx(0.7)


def test_the_half_life_is_what_it_says():
    now = datetime(2026, 8, 3)
    at_half_life = rank_score(
        0.0, created_at=now - timedelta(days=HALF_LIFE_DAYS), now=now
    )
    assert at_half_life == pytest.approx(RECENCY_WEIGHT * 0.5, abs=1e-6)


def test_scores_are_never_reduced_below_the_similarity():
    """Purely ADDITIVE: the worst this can do is reorder. Anything that could
    push a candidate below the retrieval threshold would be a deletion."""
    now = datetime(2026, 8, 3)
    for age in (0, 30, 365, 5000):
        score = rank_score(0.6, created_at=now - timedelta(days=age), now=now)
        assert score >= 0.6

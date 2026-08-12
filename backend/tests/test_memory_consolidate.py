"""Contact history consolidation (2026-08-04, Tier 2 item 7).

The budget round made memory BOUNDED; this makes what falls outside the bound
still USEFUL. `budget.MAX_FACTS_PER_CONTACT = 8` clips a contact's fact log in
every prompt, so past the recent window everything Furi learned about a person
was in the database, on the API, in the UI — and invisible to the model forever.

Most of these tests are about what it must NOT do: it deletes nothing, it
overwrites nothing a human wrote, it spends no LLM call to discover there is
nothing to do, and it throws away a digest that does not rest on the notes it
claims to summarise.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import Contact, ContactInteraction, utc_now
from app.memory.budget import MAX_CONTACT_DIGEST, MAX_FACTS_PER_CONTACT
from app.memory.consolidate import (
    CONSOLIDATE_MIN_NEW,
    DIGEST_MAX_TOKENS,
    MIN_TOKENS_TO_JUDGE,
    UNGROUNDED_MAX,
    compose_digest,
    consolidate_contact,
    consolidate_contact_histories,
    digest_is_grounded,
    find_candidate,
    looks_truncated,
    render_source_block,
)


# --------------------------------------------------------------- fixtures


async def _contact(db, name="Jamil Ali", **kw) -> Contact:
    contact = Contact(name=name, relationship_type=kw.pop("rel", "friend"), **kw)
    db.add(contact)
    await db.commit()
    return contact


async def _facts(db, contact: Contact, n: int, *, prefix="note", oldest_days=400):
    """n facts, oldest first, one day apart."""
    rows = []
    for i in range(n):
        row = ContactInteraction(
            contact_id=contact.id,
            description=f"{prefix} {i} about cricket in Lahore",
            category="other",
        )
        row.interaction_date = utc_now() - timedelta(days=oldest_days - i)
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


class _StubProvider:
    """Counts calls, so "spends no LLM call" is an assertion rather than a hope."""

    def __init__(self, reply: str = ""):
        self.reply = reply
        self.calls = 0
        self.last_prompt = ""

    async def chat(self, messages, **kw):
        self.calls += 1
        self.last_prompt = messages[-1].content

        class R:
            content = self.reply

        return R()


@pytest.fixture
def stub_provider(monkeypatch):
    provider = _StubProvider()
    import app.memory.consolidate as consolidate

    monkeypatch.setattr(consolidate, "create_provider", lambda: provider)
    return provider


# ----------------------------------------------------- what it must NOT do


@pytest.mark.asyncio
async def test_a_short_fact_log_is_never_a_candidate(db_session):
    """Nothing is being clipped, so there is nothing to rescue."""
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT)
    assert await find_candidate(db_session) is None


@pytest.mark.asyncio
async def test_finding_nothing_to_do_costs_zero_llm_calls(db_session, stub_provider):
    """⚠️ THE COST ASSERTION. Housekeeping runs every 15 minutes; if the common
    case cost a call this would be ~96 calls a day against a paid quota to
    discover repeatedly that nothing changed."""
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + 1)

    assert await consolidate_contact_histories(db_session) == 0
    assert stub_provider.calls == 0


@pytest.mark.asyncio
async def test_a_contact_just_over_the_window_waits_for_enough_new_material(db_session):
    """One newly-aged-out fact is not worth a call: the digest exists to rescue
    a long tail, not to be recomposed per fact."""
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW - 1)
    assert await find_candidate(db_session) is None


@pytest.mark.asyncio
async def test_consolidation_deletes_nothing(db_session, stub_provider):
    """⚠️ THE LOAD-BEARING PROPERTY. This is strictly weaker than the archive,
    which at least sets `archived_at`. Consolidation is purely additive: every
    fact it summarises is still a row afterwards."""
    stub_provider.reply = "Jamil is a friend who plays cricket in Lahore."
    contact = await _contact(db_session)
    total = MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 3
    await _facts(db_session, contact, total)

    assert await consolidate_contact_histories(db_session) == 1

    rows = (
        await db_session.execute(
            select(ContactInteraction).where(ContactInteraction.contact_id == contact.id)
        )
    ).scalars().all()
    assert len(rows) == total


@pytest.mark.asyncio
async def test_consolidation_never_touches_the_summary_field(db_session, stub_provider):
    """`Contact.summary` is written by extraction AND by the user. The digest
    owns its own column so consolidating can never destroy something typed."""
    stub_provider.reply = "Jamil is a friend who plays cricket in Lahore."
    contact = await _contact(db_session, summary="Hand-written by the user.")
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 1)

    await consolidate_contact_histories(db_session)
    await db_session.refresh(contact)

    assert contact.summary == "Hand-written by the user."
    assert contact.history_digest


@pytest.mark.asyncio
async def test_the_newest_facts_are_never_summarised_away(db_session, stub_provider):
    """The render shows the newest MAX_FACTS_PER_CONTACT verbatim. Absorbing
    them into the digest would replace exact facts with prose about them."""
    stub_provider.reply = "Jamil plays cricket in Lahore."
    contact = await _contact(db_session)
    rows = await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 2)

    await consolidate_contact(db_session, contact)
    await db_session.refresh(contact)

    newest_kept = rows[-MAX_FACTS_PER_CONTACT:]
    assert all(
        f.interaction_date > contact.history_digest_upto for f in newest_kept
    )


@pytest.mark.asyncio
async def test_a_failed_composition_stores_nothing(db_session, stub_provider):
    """No fallback, deliberately: no digest is exactly today's behaviour, and a
    deterministic stand-in would read like knowledge and carry none."""
    stub_provider.reply = ""  # provider returned nothing
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 1)

    assert await consolidate_contact(db_session, contact) is False
    await db_session.refresh(contact)
    assert contact.history_digest is None
    assert contact.history_digest_upto is None


@pytest.mark.asyncio
async def test_a_provider_that_raises_never_breaks_the_sweep(db_session, monkeypatch):
    class _Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("provider down")

    import app.memory.consolidate as consolidate

    monkeypatch.setattr(consolidate, "create_provider", lambda: _Boom())
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 1)

    assert await consolidate_contact_histories(db_session) == 0


@pytest.mark.asyncio
async def test_an_ungrounded_digest_is_thrown_away(db_session, stub_provider):
    """The comparator, end to end: a digest about a different person entirely
    is discarded and nothing is stored."""
    stub_provider.reply = (
        "Beatrice manages procurement contracts in Reykjavik and enjoys "
        "competitive dressage alongside amateur volcanology."
    )
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 1)

    assert await consolidate_contact(db_session, contact) is False
    await db_session.refresh(contact)
    assert contact.history_digest is None


# ------------------------------------------------------------ what it does


@pytest.mark.asyncio
async def test_a_long_fact_log_is_consolidated_once(db_session, stub_provider):
    stub_provider.reply = "Jamil is a friend who plays cricket in Lahore."
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 5)

    assert await consolidate_contact_histories(db_session) == 1
    assert stub_provider.calls == 1

    # And a second pass finds nothing new to do — no facts have aged out since.
    assert await consolidate_contact_histories(db_session) == 0
    assert stub_provider.calls == 1


@pytest.mark.asyncio
async def test_the_most_clipped_contact_is_served_first(db_session, stub_provider):
    """One contact per pass, so WHICH one matters: the person losing the most
    knowledge should not wait behind someone barely over the line."""
    stub_provider.reply = "They play cricket in Lahore."
    small = await _contact(db_session, name="Small Log")
    big = await _contact(db_session, name="Big Log")
    await _facts(db_session, small, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 1)
    await _facts(db_session, big, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 40)

    candidate = await find_candidate(db_session)
    assert candidate is not None and candidate.name == "Big Log"


@pytest.mark.asyncio
async def test_an_inactive_contact_is_never_consolidated(db_session, stub_provider):
    contact = await _contact(db_session, is_active=False)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 5)
    assert await find_candidate(db_session) is None


@pytest.mark.asyncio
async def test_the_previous_digest_travels_into_the_next_generation(db_session):
    """⚠️ WHAT MAKES THE ROLLING DESIGN LOSSLESS ACROSS GENERATIONS. The input
    is bounded per generation; the only thing carrying the older history
    forward is the previous digest appearing in the composer's own prompt. If
    it stopped travelling, everything before the last window would be dropped
    silently — with a digest still sitting there looking complete."""
    contact = await _contact(db_session, history_digest="Known Jamil since 2019.")
    facts = await _facts(db_session, contact, 3)

    block = render_source_block(contact, facts)

    assert "Known Jamil since 2019." in block
    assert "note 0 about cricket in Lahore" in block
    assert "Jamil Ali" in block


@pytest.mark.asyncio
async def test_recomposition_only_absorbs_facts_past_the_watermark(
    db_session, stub_provider
):
    stub_provider.reply = "Jamil plays cricket in Lahore, seen often."
    contact = await _contact(db_session)
    await _facts(db_session, contact, MAX_FACTS_PER_CONTACT + CONSOLIDATE_MIN_NEW + 2)
    await consolidate_contact(db_session, contact)
    await db_session.refresh(contact)
    first_watermark = contact.history_digest_upto

    # Enough new facts arrive for the tail to grow past the window again.
    await _facts(
        db_session, contact, CONSOLIDATE_MIN_NEW + 2, prefix="later", oldest_days=100
    )
    assert await consolidate_contact_histories(db_session) == 1
    await db_session.refresh(contact)

    assert contact.history_digest_upto > first_watermark
    # A fact the first digest already covers is NOT re-read — it is carried by
    # the previous digest, which is what keeps the input bounded forever.
    assert "note 0 about cricket in Lahore" not in stub_provider.last_prompt
    # A fact that has since aged out of the render window IS absorbed. (The
    # "later" batch is newer still, so it correctly stays verbatim in the
    # render rather than being summarised — that is the newest-window rule.)
    assert "note 7 about cricket in Lahore" in stub_provider.last_prompt


# ------------------------------------------------------------- the guard

# ⚠️ THE THRESHOLD IS MEASURED, NOT CHOSEN. Both texts below are realistic:
# the faithful one is what the composer prompt asks for (prose about the notes,
# with the connective words a paragraph needs), and the fabricated one is the
# failure mode that matters — plausible, confident, and about nothing in the
# record. Pinning the two measurements either side of UNGROUNDED_MAX is what
# stops the constant being "tidied" later, the SIMILARITY_FLOOR precedent.

_NOTES = [
    "Played cricket with Jamil at the Gaddafi Stadium nets",
    "Jamil started a new job at Systems Limited in Lahore",
    "Jamil is allergic to peanuts",
    "Went to Jamil's wedding in Islamabad",
    "Jamil and I watched the Pakistan match together",
]
_FAITHFUL = (
    "Jamil is a long-standing friend in Lahore who plays cricket with the user "
    "at the Gaddafi Stadium nets and started a job at Systems Limited. He is "
    "allergic to peanuts, married in Islamabad, and watches Pakistan matches "
    "with the user."
)
_FABRICATED = (
    "Beatrice is a procurement director in Reykjavik who breeds Icelandic "
    "horses, chairs the volcanology society, and spends winters restoring "
    "harpsichords in Copenhagen."
)


def test_the_threshold_sits_between_a_faithful_digest_and_a_fabricated_one():
    def ungrounded_share(digest: str) -> float:
        from app.memory.consolidate import _significant

        tokens = _significant(digest)
        haystack = " ".join(_NOTES).lower()
        return sum(1 for t in tokens if t not in haystack) / len(tokens)

    faithful = ungrounded_share(_FAITHFUL)
    fabricated = ungrounded_share(_FABRICATED)

    # The measurement, recorded so a future change to the stopword list or the
    # constant has to argue with a number.
    assert faithful <= UNGROUNDED_MAX < fabricated, (
        f"faithful={faithful:.2f} fabricated={fabricated:.2f} "
        f"threshold={UNGROUNDED_MAX}"
    )


def test_a_faithful_digest_is_grounded():
    assert digest_is_grounded(_FAITHFUL, _NOTES) is True


def test_a_fabricated_digest_is_not_grounded():
    assert digest_is_grounded(_FABRICATED, _NOTES) is False


def test_too_little_text_is_not_convicted():
    """Fails toward ACCEPTING here on purpose: convicting on two words would
    reject a perfectly good one-line digest, and the cost of accepting a short
    one is bounded by how little it can say."""
    assert digest_is_grounded("A friend.", _NOTES) is True


def test_a_digest_is_grounded_against_the_previous_digest_too():
    """Generation N summarises generation N-1 plus new facts, so the previous
    digest is part of the record it must rest on."""
    previous = "Jamil has been a friend since university in Lahore."
    assert digest_is_grounded(
        "Jamil is a university friend from Lahore who plays cricket.",
        ["Played cricket with Jamil"] + [previous],
    ) is True


@pytest.mark.asyncio
async def test_a_long_model_reply_is_clipped_before_storage(stub_provider):
    """The digest renders into every prompt that mentions this person, so an
    unbounded model reply must not become an unbounded prompt block."""
    from app.memory.consolidate import DIGEST_MAX_CHARS

    stub_provider.reply = "cricket in Lahore with Jamil. " * 500
    digest = await compose_digest("PERSON: Jamil")

    assert len(digest) <= DIGEST_MAX_CHARS
    # And the render caps it again, independently — belt and braces, because
    # this column is also reachable by a hand-edited row.
    assert MAX_CONTACT_DIGEST <= DIGEST_MAX_CHARS


def test_min_tokens_to_judge_is_small_enough_to_judge_a_real_digest():
    """A guard that never fires is not a guard. The realistic faithful digest
    above must be long enough to be judged at all."""
    from app.memory.consolidate import _significant

    assert len(_significant(_FAITHFUL)) >= MIN_TOKENS_TO_JUDGE


# ------------------------------------------- the cut-off digest (found live)


@pytest.mark.asyncio
async def test_a_digest_cut_off_mid_sentence_is_thrown_away(stub_provider):
    """⚠️ FOUND BY THE RUNTIME CHECK, NOT BY ANY TEST HERE. Against the real
    provider the first digest came back as "…His wife Sana is a" — everything
    after silently absent, and nothing in the stored text saying so. A stub
    returns whatever it is told, so no hermetic test could have seen it."""
    stub_provider.reply = (
        "Jamil is a backend engineer at Systems Limited in Lahore. His wife "
        "Sana is a"
    )
    assert await compose_digest("PERSON: Jamil") == ""


@pytest.mark.asyncio
async def test_a_complete_digest_is_kept(stub_provider):
    stub_provider.reply = "Jamil is a backend engineer at Systems Limited in Lahore."
    assert await compose_digest("PERSON: Jamil") == stub_provider.reply


def test_the_truncation_test_is_certain_not_a_judgement():
    assert looks_truncated("He works at Systems Limited") is True
    assert looks_truncated("He works at Systems Limited.") is False
    assert looks_truncated("Does he? Yes!") is False
    assert looks_truncated('She said "yes."') is False
    assert looks_truncated("") is False  # nothing to judge; the caller handles it


@pytest.mark.asyncio
async def test_a_long_reply_is_clipped_at_a_sentence_boundary(stub_provider):
    """Storage must never CREATE the half-sentence the guard rejects."""
    stub_provider.reply = "Jamil plays cricket in Lahore. " * 200
    digest = await compose_digest("PERSON: Jamil")
    assert digest.endswith(".")
    assert not digest.endswith("Jamil plays cricket in Lahor")


def test_the_token_budget_clears_the_thinking_model_floor():
    """⚠️ THE ACTUAL DEFECT. 320 sat below the floor this codebase already
    learned and wrote down: on a thinking model the REASONING tokens come out
    of the same budget, which is why `task_router` and `reading_enumerator`
    both pin 512. A future tidy-up that lowers this has to argue with the
    number."""
    assert DIGEST_MAX_TOKENS >= 512

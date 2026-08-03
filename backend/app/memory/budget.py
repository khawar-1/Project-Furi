"""
Jarvis OS — MEMORY CONTEXT budget (2026-08-03)

*(Tier 2, item 7 of `suhhestionsfromclaude.txt` — "memory only grows")*

WHAT WAS ACTUALLY UNBOUNDED
---------------------------
`MemoryEngine.format_context` had exactly ONE `[:5]` and ONE `[:120]` in the
whole function. Everything else went into the prompt verbatim, and the growth
was concentrated in one place — the `PEOPLE YOU KNOW` section, which selected
`ContactInteraction` for every bundled contact with **no LIMIT, no date filter
and no clip**, alongside the contact's full `summary` and `notes`. A contact
with 200 logged facts put 200 lines in every prompt that mentioned them, forever;
the fact log is append-only by design, so it only ever gets longer.

And there was no backstop downstream: `chat._build_system_prompt` has no total
budget at all, while the *transcript* is capped at 30 messages / 24k chars
(`_provider_history`) — capped, its own comment says, PRECISELY BECAUSE
"long-range recall is the memory engine's job". The one component the transcript
budget leans on was the one with no budget of its own.

Measured on the real database when this shipped: 19 semantic memories (953 chars
total), 46 contact interactions, 0 episodes. So this is PREVENTIVE — the shape
is wrong, not the current size, and it has to be exercised synthetically
(`test_memory_budget.py` seeds 500 memories / 50 contacts × 200 facts).

⚠️ WHY THE OTHER PROMPT BLOCKS ARE NOT BOUNDED HERE, AND WHY THERE IS NO GLOBAL
PROMPT CAP
------------------------------------------------------------------------------
Checked rather than assumed: `screen_note` is bounded at its source
(`screen_ocr.CONDENSE_MAX_CHARS = 600`), `background_note` at
`task_status._MAX_TASKS = 8`, and the disambiguation blocks by their candidate
counts. Memory was the ONLY unbounded input.

A "total system-prompt budget" was considered and REJECTED: the base prompt is
the IDENTITY / CAPABILITIES / MEMORY-RULES / honesty block, and a cap that could
clip it would delete the rules that stop Jarvis fabricating actions — a safety
regression dressed as tidying. Bound the input that grows; never the rules.

HOW IT CLIPS
------------
Allocation is FAIR-SHARE across sections, reusing `rendering.fair_shares` — the
public seam that exists so a caller with its own budget does not write a second
allocator. Position must never determine survival: sections are rendered in a
fixed order and a first-come-first-served cut would always drop episodes and
never touch the profile.

Within a section the cut is BY ITEM — a fact, a contact, a memory — never
mid-string, and it is always MARKED. Nothing here deletes or hides a row: this
is a rendering budget, and every fact it leaves out is still in the database and
still returned by the API.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.rendering import fair_shares

# The whole rendered block, across every section. The transcript gets 24k
# (`chat._HISTORY_MAX_CHARS`); memory is the durable half and needs far less per
# turn than the live conversation, because what it carries is already the top-N
# of a relevance search rather than a running log.
MEMORY_CONTEXT_TOTAL_CAP = 6000
# ⚠️ THERE IS DELIBERATELY NO per-section floor constant here. The first draft
# had one (400) and it was DEAD CODE: `rendering.fair_shares` already floors
# every share at its own `_MIN_STEP_SHARE = 800`, so a lower local floor could
# never bind. A constant that looks load-bearing and cannot take effect is the
# same class of lie as a second copy of a list — the test that was written to
# prove the floor worked is what caught it.
#
# That borrowed floor is safe here ONLY because the section count is small and
# fixed: `format_context` emits at most MAX_SECTIONS, and 5 x 800 < the cap. If
# a section is ever added, `test_the_section_count_cannot_overflow_the_floor`
# fails — which is the point, because n x 800 > cap would silently let the
# floor beat the budget.
MAX_SECTIONS = 5

# Per-contact fact cap, applied BEFORE the fair-share allocation so one
# well-documented person cannot crowd out everyone else. The most RECENT facts
# are kept: the fact log is ordered oldest-first and "what is new about this
# person" is what a conversation turns on. The engine's own MEMORY RULES tell
# the model the most recent fact in a category is the current truth, so keeping
# the tail is also the reading that rule assumes.
MAX_FACTS_PER_CONTACT = 8
# The consolidated digest of everything the line above clips (2026-08-04, see
# app/memory/consolidate.py). Larger than MAX_CONTACT_TEXT because this one
# field stands in for the whole of a contact's older history — but still capped
# here as the belt: consolidate.py bounds what it writes, and this bounds what
# is rendered whatever ends up in the column.
MAX_CONTACT_DIGEST = 600
# Free-text contact fields. `summary` and `notes` are `Text` columns with no
# length limit anywhere in the write path.
MAX_CONTACT_TEXT = 300
# `background` and `work_style` on the single-row profile, likewise unbounded.
MAX_PROFILE_FIELD = 400


@dataclass
class Section:
    """One rendered MEMORY CONTEXT section, kept as its title plus its ITEMS so
    the budget can drop whole items rather than cut one in half."""

    title: str
    items: list[str] = field(default_factory=list)
    # Items are ordered most-important-first, so a cut drops from the END.
    # (The engine reverses where its own order is oldest-first.)

    def render(self) -> str:
        return self.title + "\n" + "\n".join(self.items)

    def size(self) -> int:
        return len(self.render())


def clip_text(text: str, cap: int) -> str:
    """Clip a free-text field, marking the cut. Uses the same wording as
    `rendering._clip` deliberately: in this codebase "truncated" is a fact a
    TOOL reports about its own results, and reusing it for "our display budget
    ran out" is what let the record lie on 2026-07-29."""
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= cap else text[:cap].rstrip() + "… (clipped for length)"


def _fit(section: Section, budget: int) -> Section:
    """Drop items from the END until the section fits, and say how many went.

    The marker is part of the section, so the model is never silently shown a
    partial list — the same reason `_names` reports "… and N more"."""
    if section.size() <= budget:
        return section
    kept: list[str] = []
    used = len(section.title) + 1
    # Reserve room for the marker so adding it cannot push us back over.
    reserve = 48
    for item in section.items:
        if used + len(item) + 1 + reserve > budget:
            break
        kept.append(item)
        used += len(item) + 1
    dropped = len(section.items) - len(kept)
    if dropped > 0:
        kept.append(f"  … and {dropped} more (clipped for length)")
    return Section(title=section.title, items=kept)


def fit_sections(
    sections: list[Section], *, total: int = MEMORY_CONTEXT_TOTAL_CAP
) -> list[Section]:
    """Fit every section inside `total`, fair-share, dropping whole items.

    Sections that want less than their share release the remainder to the ones
    that want more (`rendering.fair_shares` does that redistribution) — so on a
    normal-sized memory nothing is clipped at all, and on a large one the cut
    lands where the bulk is instead of always on the last section."""
    live = [s for s in sections if s.items]
    if not live:
        return []
    if sum(s.size() for s in live) <= total:
        return live  # the common case: nothing to do, nothing to explain
    shares = fair_shares([s.render() for s in live], total)
    return [_fit(s, share) for s, share in zip(live, shares)]

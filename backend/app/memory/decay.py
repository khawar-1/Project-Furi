"""
Jarvis OS — Memory decay as a RANKING signal (2026-08-03)

*(Tier 2, item 7 — "memory only grows")*

Before this, a semantic-memory search was pure cosine similarity: two facts that
mention the same thing ranked identically whether one was written this morning
or eighteen months ago, and nothing anywhere preferred the fresh one. That is
fine at 19 memories and wrong at 5,000.

WHAT THIS IS NOT
----------------
It is not a filter and it is not a deletion. Everything here is an ADDITIVE
boost applied to candidates a search has ALREADY returned, so the worst it can
do is reorder. A decay that could push a fact below the retrieval threshold
would be a silent deletion wearing a ranking function's clothes, and the one
rule this whole item is built around is that nothing is destroyed
automatically.

⚠️ RELEVANCE STILL DOMINATES, BY CONSTRUCTION. Cosine similarity over the
retrieval threshold runs 0.5–1.0; the boosts here total at most
`RECENCY_WEIGHT + USAGE_WEIGHT` = 0.25. So freshness can break a near-tie —
which is the whole point — but it can never float an irrelevant fact over a
relevant one. If that ratio is ever changed, that is the property to re-check.

⚠️ REFERENCE FRAMES. `SemanticMemory.created_at` / `last_used_at` are NAIVE UTC
(`models.utc_now()`), so `now` must be naive UTC too. This is the mistake
`semantic_file_tools._recency_boost` documents in its own docstring — file
mtimes are LOCAL and message timestamps are naive UTC, and comparing one against
the other silently shifts every score by the machine's offset. Callers pass
`now`; the default is `utc_now()`.

Pure functions with an injectable clock, deliberately — the `detect_cadence`
discipline, so the behaviour is testable without freezing time or touching a DB.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

# How fast freshness fades. At one half-life the recency boost is half its
# maximum. Three months is chosen to match how people actually talk about their
# own lives — "recently" is a season, not a week.
HALF_LIFE_DAYS = 90.0
# Maximum boosts. Kept small against the 0.5-1.0 cosine range on purpose; see
# the ⚠️ note above.
RECENCY_WEIGHT = 0.15
USAGE_WEIGHT = 0.10
# How many candidates to pull from the vector store per requested result, so
# there is something to re-rank. Ranking N items into N slots changes nothing.
OVERFETCH = 3


def _half_life_decay(when: Optional[datetime], now: datetime) -> float:
    """1.0 for something that just happened, → 0.0 as it recedes. An absent
    timestamp scores 0 — unknown is treated as old, never as fresh, so a NULL
    can never win a tie it has no claim to."""
    if when is None:
        return 0.0
    age_days = (now - when).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / HALF_LIFE_DAYS)


def rank_score(
    similarity: float,
    *,
    created_at: Optional[datetime] = None,
    last_used_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> float:
    """Cosine similarity plus a small freshness/usage bonus.

    `last_used_at` is when the fact was last actually rendered into a prompt —
    a fact the conversation keeps coming back to stays near the top even as it
    ages, which is the difference between "old" and "stale"."""
    if now is None:
        from app.db.models import utc_now

        now = utc_now()
    return (
        float(similarity)
        + RECENCY_WEIGHT * _half_life_decay(created_at, now)
        + USAGE_WEIGHT * _half_life_decay(last_used_at, now)
    )

"""
Identity resolution unit tests — deterministic fuzzy matching.
Ported from the ad-hoc debug scripts (test_jamil.py, test_fuzzy_logic.py,
test_identity_resolver.py) into the pytest suite. Pure sync, no DB.
"""
from dataclasses import dataclass

from app.memory.engine import (
    ResolutionStatus,
    identify_contact,
    normalize_name,
    resolve_confirmation,
)


@dataclass
class FakeContact:
    id: str
    name: str


def contacts(*names: str) -> list[FakeContact]:
    return [FakeContact(id=f"id-{i}", name=n) for i, n in enumerate(names)]


# ============================================================
# normalize_name
# ============================================================

def test_normalize_name_strips_possessive_and_punctuation():
    assert normalize_name("Jamil's") == "jamil"
    assert normalize_name("  Jamil   Ali! ") == "jamil ali"
    assert normalize_name("") == ""


# ============================================================
# identify_contact — RESOLVED / AMBIGUOUS / NOT_FOUND
# ============================================================

def test_exact_match_resolves_even_with_similar_candidates():
    # "jamil ali" is an exact (case-insensitive) match — resolves immediately
    # despite "Jamil Ali Khan" also being similar.
    result = identify_contact("jamil ali", contacts("Jamil Ali", "Jamil Ali Khan"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.contact.name == "Jamil Ali"


def test_subset_name_with_multiple_matches_is_ambiguous():
    # "jamil" is a strict subset of both names — must ask, never guess.
    result = identify_contact("jamil", contacts("Jamil Ali", "Jamil Khan"))
    assert result.status == ResolutionStatus.AMBIGUOUS
    names = {c["name"] for c in result.candidates}
    assert names == {"Jamil Ali", "Jamil Khan"}


def test_subset_name_with_single_match_resolves():
    result = identify_contact("jamil", contacts("Jamil Ali", "Sara Khan"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.contact.name == "Jamil Ali"


def test_typo_resolves_to_close_contact():
    result = identify_contact("jamel ali", contacts("Jamil Ali", "Sara Khan"))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.contact.name == "Jamil Ali"


def test_unrelated_name_not_found():
    result = identify_contact("bob", contacts("Jamil Ali", "Sara Khan"))
    assert result.status == ResolutionStatus.NOT_FOUND


def test_empty_contact_list_not_found():
    result = identify_contact("anyone", [])
    assert result.status == ResolutionStatus.NOT_FOUND


# ============================================================
# resolve_confirmation — positional, candidate, global, not-found
# ============================================================

CANDIDATES = [
    {"id": "c1", "name": "Jamil Ali"},
    {"id": "c2", "name": "Jamil Khan"},
]
ALL_CONTACTS = contacts("Jamil Ali", "Jamil Khan", "Sara Malik")


def test_positional_first_and_second():
    assert resolve_confirmation("the first one", CANDIDATES, ALL_CONTACTS) == "c1"
    assert resolve_confirmation("second", CANDIDATES, ALL_CONTACTS) == "c2"
    assert resolve_confirmation("2", CANDIDATES, ALL_CONTACTS) == "c2"


def test_distinguishing_token_matches_candidate():
    assert resolve_confirmation("khan", CANDIDATES, ALL_CONTACTS) == "c2"
    assert resolve_confirmation("Jamil Ali", CANDIDATES, ALL_CONTACTS) == "c1"


def test_completely_different_name_falls_back_to_global_match():
    # User pivots to a name outside the candidates but known globally
    resolved = resolve_confirmation("Sara Malik", CANDIDATES, ALL_CONTACTS)
    assert resolved == "id-2"  # Sara Malik's id in ALL_CONTACTS


def test_unknown_reply_returns_not_found_global():
    assert resolve_confirmation("zzz qqq", CANDIDATES, ALL_CONTACTS) == "NOT_FOUND_GLOBAL"

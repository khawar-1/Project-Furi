"""
Public-suffix handling, and the grounding bug it fixes (2026-07-26).

THE LIVE DEFECT. `origin_is_grounded` computed the registrable name label as
`labels[-2]`, while its docstring described "the part left of the effective
TLD". Those agree for a single-label suffix (`indeed.com` -> `indeed`) and
disagree for a multi-label one: `labels[-2]` of `outfitters.com.pk` is the
string `"com"`. So "go to outfitters" could not ground the Pakistani site the
user actually meant, and the browse dead-ended.

THE SAFETY ARGUMENT these tests exist to pin: a partial suffix table fails
CLOSED. A missing rule shortens the computed suffix, which makes the registrable
name wrong, which makes the match FAIL — capability lost, boundary never
widened. The adversarial cases don't depend on the table at all.
"""
import pytest

from app.browser.grounding import origin_is_grounded
from app.browser.publicsuffix import public_suffix, registrable, registrable_name


# ------------------------------------------------------------- the primitives
@pytest.mark.parametrize(
    "host, expected",
    [
        # single-label suffixes: the case labels[-2] always got right
        ("indeed.com", "indeed"),
        ("jobs.indeed.com", "indeed"),
        ("example.org", "example"),
        ("twitch.tv", "twitch"),
        # multi-label suffixes: the case it got wrong
        ("outfitters.com.pk", "outfitters"),
        ("shop.outfitters.com.pk", "outfitters"),
        ("amazon.co.uk", "amazon"),
        ("smile.amazon.co.uk", "amazon"),
        ("bbc.co.uk", "bbc"),
        ("sony.co.jp", "sony"),
        ("daraz.com.pk", "daraz"),
        ("hangers.com.pk", "hangers"),
        # the lookalike guard, which never needed the table
        ("indeed.attacker.com", "attacker"),
        ("outfitters.evil.com", "evil"),
    ],
)
def test_registrable_name(host, expected):
    assert registrable_name(host) == expected


@pytest.mark.parametrize(
    "host, suffix",
    [
        ("indeed.com", "com"),
        ("outfitters.com.pk", "com.pk"),
        ("amazon.co.uk", "co.uk"),
        # A two-label host that LOOKS like a known multi-label suffix is not one:
        # "com.pk" itself is the suffix, not a registrable domain, so there is no
        # name label to take and the multi-label rule must not fire.
        ("com.pk", "pk"),
    ],
)
def test_public_suffix(host, suffix):
    assert public_suffix(host) == suffix


def test_registrable_domain():
    assert registrable("jobs.indeed.com") == "indeed.com"
    assert registrable("shop.outfitters.com.pk") == "outfitters.com.pk"
    assert registrable("smile.amazon.co.uk") == "amazon.co.uk"


def test_no_wildcard_rules_are_bundled():
    """A wildcard is the one rule shape whose ABSENCE does not fail closed — it
    can LENGTHEN a suffix, moving a boundary in the unsafe direction. Literals
    only, and this pins it so nobody pastes a PSL chunk in later."""
    from app.browser.publicsuffix import _MULTI_LABEL_SUFFIXES

    assert not any("*" in rule or rule.startswith(".") for rule in _MULTI_LABEL_SUFFIXES)


# --------------------------------------------------- the grounding behaviour
def test_a_bare_name_now_grounds_a_multi_label_domain():
    """THE FIX. "go to outfitters" grounds outfitters.com.pk, which is where the
    real store lives."""
    assert origin_is_grounded("outfitters.com.pk", {"outfitters"}) is True
    assert origin_is_grounded("daraz.com.pk", {"daraz"}) is True
    assert origin_is_grounded("amazon.co.uk", {"amazon"}) is True


def test_a_bare_name_still_refuses_a_lookalike():
    """Unchanged, and the reason the fix is safe: the registrable name of
    indeed.attacker.com is 'attacker', so a bare 'indeed' never reaches it."""
    assert origin_is_grounded("indeed.attacker.com", {"indeed"}) is False
    assert origin_is_grounded("outfitters.com.pk.evil.com", {"outfitters"}) is False
    assert origin_is_grounded("evil-outfitters.com.pk", {"outfitters"}) is False


def test_a_full_domain_does_not_stretch_across_public_suffixes():
    """THE OWNER'S DECISION, pinned (2026-07-26): "always pause and ask" for
    redirects. When the user names a FULL domain, only that domain and its
    subdomains ground. outfitters.com does NOT silently authorise
    outfitters.com.pk — that redirect becomes a question instead, which is the
    conservative posture chosen deliberately over auto-following same-brand
    geo-redirects."""
    assert origin_is_grounded("outfitters.com.pk", {"outfitters.com"}) is False
    assert origin_is_grounded("amazon.co.uk", {"amazon.com"}) is False


def test_subdomain_matching_is_unchanged():
    assert origin_is_grounded("m.youtube.com", {"youtube.com"}) is True
    assert origin_is_grounded("evil-youtube.com", {"youtube.com"}) is False
    assert origin_is_grounded("", {"youtube.com"}) is False


def test_an_unknown_suffix_fails_closed_not_open():
    """A ccTLD hierarchy the table does not know shortens the suffix, so the
    name label is wrong and the match fails. Capability lost, never a widened
    boundary — the property that makes a hand-maintained list acceptable."""
    # 'zzz' is not a real multi-label suffix in the table.
    assert origin_is_grounded("outfitters.com.zzz", {"outfitters"}) is False
    assert registrable_name("outfitters.com.zzz") == "com"

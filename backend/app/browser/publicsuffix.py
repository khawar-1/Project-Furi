"""
Furi OS — public suffixes, bundled and deliberately partial.

WHY THIS EXISTS
---------------
`grounding.origin_is_grounded` needs the REGISTRABLE NAME of a host — the label
immediately left of the effective public suffix — to decide whether a bare site
name the user typed ("go to indeed") grounds a domain the planner proposed.

It approximated that as `labels[-2]`, and its own docstring described something
different ("the part left of the effective TLD"). For a single-label suffix the
two agree: `indeed.com` → `indeed`. For a multi-label one they do not, and the
gap is not academic — live 2026-07-26, "go to outfitters" could not ground
`outfitters.com.pk`, because `labels[-2]` of that host is the string `"com"`.
The Pakistani site the user meant was unreachable through a guard that was
computing the wrong thing.

WHY BUNDLED, AND WHY NOT `tldextract`
-------------------------------------
`tldextract` fetches the live Public Suffix List over the network on first use
and caches it to disk. A remote fetch inside the decision path of the guard that
holds the exfiltration bound is the wrong shape at any size: the guard's answer
would depend on a file retrieved at runtime, and an unexpected NEW rule arriving
from the network could LENGTHEN a suffix and move a boundary nobody reviewed.

WHY A PARTIAL LIST IS SAFE — the property that makes this defensible
--------------------------------------------------------------------
Incompleteness fails CLOSED. A missing rule makes the computed suffix SHORTER,
which makes the registrable name WRONGER, which makes the match FAIL:

    registrable_name("outfitters.com.pk")  without a com.pk rule  -> "com"
      => a bare "outfitters" does NOT ground it. Capability lost, nothing else.

And the adversarial cases never depended on the table at all:

    registrable_name("indeed.attacker.com") -> "attacker"
      => a bare "indeed" does NOT ground it, table or no table.

So the cost of a gap here is a dead end the user can route around by naming the
full domain; it is never a widened boundary. That asymmetry is the whole reason
a hand-maintained list is acceptable where a fetched one is not.

NO WILDCARD RULES ARE BUNDLED. The real PSL has entries like `*.ck` and
`*.compute.amazonaws.com`, and a wildcard is the one rule shape that can make a
suffix longer than the literal table implies — i.e. the one shape whose ABSENCE
does not fail closed in the direction argued above. Literals only.
"""
from __future__ import annotations

# Multi-label public suffixes, flat literals. Covers the country second-level
# hierarchies that real commercial sites actually sit under; the single-label
# case (.com, .org, .io, …) needs no table at all and is handled by the default.
#
# Adding to this list only ever ENABLES a match that a bare user-typed name
# would otherwise miss. It cannot enable one for a different registrable name.
_MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset({
    # generic second levels, used under many ccTLDs
    "com.af", "com.ag", "com.ai", "com.ar", "com.au", "com.bd", "com.bh",
    "com.bn", "com.bo", "com.br", "com.bz", "com.cn", "com.co", "com.cu",
    "com.cy", "com.do", "com.ec", "com.eg", "com.et", "com.fj", "com.gh",
    "com.gt", "com.hk", "com.jm", "com.jo", "com.kh", "com.kw", "com.lb",
    "com.lc", "com.lk", "com.ly", "com.mm", "com.mt", "com.mx", "com.my",
    "com.na", "com.ng", "com.ni", "com.np", "com.om", "com.pa", "com.pe",
    "com.ph", "com.pk", "com.pl", "com.pr", "com.py", "com.qa", "com.sa",
    "com.sb", "com.sg", "com.sv", "com.tr", "com.tt", "com.tw", "com.ua",
    "com.uy", "com.vc", "com.ve", "com.vn",
    "co.ao", "co.at", "co.bw", "co.ck", "co.cr", "co.id", "co.il", "co.in",
    "co.jp", "co.ke", "co.kr", "co.ls", "co.ma", "co.mz", "co.nz", "co.th",
    "co.tz", "co.ug", "co.uk", "co.uz", "co.ve", "co.vi", "co.za", "co.zm",
    "co.zw",
    "net.au", "net.br", "net.cn", "net.il", "net.in", "net.nz", "net.pk",
    "net.tr", "net.tw", "net.uk", "net.za",
    "org.au", "org.br", "org.cn", "org.il", "org.in", "org.nz", "org.pk",
    "org.tr", "org.tw", "org.uk", "org.za",
    "edu.au", "edu.br", "edu.cn", "edu.hk", "edu.in", "edu.mx", "edu.pk",
    "edu.sg", "edu.tr", "edu.tw", "edu.za",
    "gov.au", "gov.br", "gov.cn", "gov.hk", "gov.in", "gov.pk", "gov.sg",
    "gov.tr", "gov.uk", "gov.za",
    "ac.at", "ac.cn", "ac.id", "ac.il", "ac.in", "ac.jp", "ac.kr", "ac.nz",
    "ac.th", "ac.uk", "ac.za",
    "gob.ar", "gob.cl", "gob.es", "gob.mx", "gob.pe", "gob.ve",
    "gouv.fr",
    "or.jp", "or.kr", "ne.jp", "go.jp", "go.kr", "in.ua", "mil.uk",
    "sch.uk", "ltd.uk", "plc.uk", "me.uk", "org.es", "com.es", "nom.es",
})


# ⚠️ "Contains a dot" is NOT enough to call a string a site: `report.txt` has the
# same shape as a domain, and only the suffix tells them apart — the table above
# bundles MULTI-label suffixes only, so `public_suffix("report.txt")` is "txt"
# and says nothing about whether "txt" is a real TLD.
#
# So a caller that must decide "is this a site or a filename?" needs a set of
# final labels it recognises. The list is partial ON PURPOSE and fails CLOSED the
# same way the suffix table does: a TLD missing here means the caller declines to
# treat the string as a site, which is always the pre-existing behaviour. It
# grants no capability and relaxes no guard.
#
# ONE HOME, TWO CALLERS (2026-08-02). This was born in task_router as
# `_NAV_TLDS`, deciding whether a message is a bare navigation instruction; the
# site-question gate needs the identical fact to decide whether a clarifying
# question's option is an address worth resolving. Two private copies of "what is
# a TLD" would drift, and drift here is a boundary nobody reviewed — the same
# reasoning that moved `registry.mutates` out of placeholder_resolver.
KNOWN_TLDS: frozenset[str] = frozenset({
    "com", "org", "net", "edu", "gov", "mil", "int", "info", "biz", "name",
    "io", "ai", "app", "dev", "co", "me", "tv", "cc", "xyz", "online", "site",
    "shop", "store", "blog", "cloud", "tech", "news", "live", "media", "page",
    # ccTLDs, common ones and the user's own.
    "pk", "uk", "us", "ca", "au", "nz", "in", "de", "fr", "es", "it", "nl",
    "se", "no", "fi", "dk", "pl", "ru", "jp", "cn", "kr", "br", "mx", "ar",
    "za", "ae", "sa", "tr", "ch", "at", "be", "ie", "pt", "gr", "cz", "sg",
    "hk", "my", "id", "ph", "th", "vn", "bd", "lk", "np", "ir", "eu",
})


def _labels(host: str) -> list[str]:
    return [p for p in (host or "").strip().lower().rstrip(".").split(".") if p]


def has_known_tld(host: str) -> bool:
    """True when `host` is dotted and its final label is a TLD we recognise —
    i.e. it reads as an address rather than a filename. See KNOWN_TLDS."""
    labels = _labels(host)
    return len(labels) >= 2 and labels[-1] in KNOWN_TLDS


def public_suffix(host: str) -> str:
    """The effective public suffix of `host` — the multi-label one when the
    bundled table knows it, otherwise the final label. Empty for an empty or
    single-label host (a bare "localhost" has no registrable structure)."""
    labels = _labels(host)
    if len(labels) < 2:
        return ""
    two = ".".join(labels[-2:])
    if two in _MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return two
    return labels[-1]


def registrable(host: str) -> str:
    """The registrable domain: the name label plus its public suffix.
    'jobs.indeed.com' -> 'indeed.com'; 'shop.outfitters.com.pk' ->
    'outfitters.com.pk'. Empty when the host has no registrable structure."""
    labels = _labels(host)
    suffix = public_suffix(host)
    if not suffix:
        return ""
    depth = len(suffix.split("."))
    if len(labels) <= depth:
        return ""
    return ".".join(labels[-(depth + 1):])


def registrable_name(host: str) -> str:
    """The registrable NAME label alone — 'indeed' for both 'indeed.com' and
    'jobs.indeed.com', 'outfitters' for 'outfitters.com.pk'.

    This is what a bare site name from the user is compared against, so it is
    the single value that decides whether "go to outfitters" reaches
    outfitters.com.pk. It must never return a component deeper than the
    registrable one: 'indeed.attacker.com' has to yield 'attacker', which is
    what refuses the lookalike."""
    reg = registrable(host)
    if not reg:
        labels = _labels(host)
        return labels[0] if labels else ""
    return reg.split(".")[0]

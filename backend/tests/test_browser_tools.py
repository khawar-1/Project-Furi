"""
Phase 6 Part 1 — BrowserTool suite.

The suite never touches the network: HTTP_FETCH_FACTORY / SEARCH_PROVIDER_FACTORY
are pointed at fakes, so tests assert the exact parsing / extraction / SSRF
behaviour without a real request.

Covered:
  - web_search: provider results pass through, capped, empty-safe.
  - read_webpage: fetch → readable-text extraction, title, truncation,
    non-text refusal, HTTP-error refusal, redirect-to-private re-check.
  - SSRF guard (_validate_url / _host_is_blocked): scheme, localhost, private /
    loopback / link-local / metadata IPs blocked; public literal IP allowed.
  - DuckDuckGo HTML parsing (_parse_ddg_results / _decode_ddg_href).
  - Both tools are READ → run through execute_tool WITHOUT approval.
  - Untrusted-data framing lives in the tool descriptions.
  - Result formatters render web output.
"""
import pytest

import app.tools  # noqa: F401 — registers every tool
from app.agents.rendering import _RESULT_FORMATTERS
from app.core.base_tool import PermissionLevel
from app.tools import browser_tools
from app.tools.browser_tools import (
    FetchedPage,
    SEARCH_MAX_RESULTS,
    _decode_ddg_href,
    _host_is_blocked,
    _parse_ddg_results,
    _validate_url,
    extract_readable,
)
from app.tools.registry import execute_tool, registry


# ------------------------------------------------------------- fake seams

@pytest.fixture(autouse=True)
def _reset_factories():
    """Every test starts with the real seams disabled; restore after."""
    browser_tools.HTTP_FETCH_FACTORY = None
    browser_tools.SEARCH_PROVIDER_FACTORY = None
    yield
    browser_tools.HTTP_FETCH_FACTORY = None
    browser_tools.SEARCH_PROVIDER_FACTORY = None


def _set_fetch(page_or_fn):
    if callable(page_or_fn):
        browser_tools.HTTP_FETCH_FACTORY = page_or_fn
    else:
        browser_tools.HTTP_FETCH_FACTORY = lambda url: page_or_fn


def _set_search(results):
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: list(results)[:n]


def _web_search():
    return registry.get("web_search")


def _read_webpage():
    return registry.get("read_webpage")


# =============================================================== web_search

async def test_web_search_returns_provider_rows():
    _set_search([
        {"title": "A", "url": "https://a.com", "snippet": "sa"},
        {"title": "B", "url": "https://b.com", "snippet": "sb"},
    ])
    result = await _web_search().execute(query="langgraph")
    assert result.success
    assert result.output["count"] == 2
    assert result.output["query"] == "langgraph"
    assert result.output["results"][0]["url"] == "https://a.com"


async def test_web_search_caps_max_results():
    _set_search([{"title": str(i), "url": f"https://x/{i}", "snippet": ""} for i in range(50)])
    result = await _web_search().execute(query="q", max_results=999)
    assert result.output["count"] <= SEARCH_MAX_RESULTS


async def test_web_search_requires_query():
    result = await _web_search().execute(query="   ")
    assert not result.success
    assert "query" in result.error.lower()


async def test_web_search_empty_results_is_failure():
    # Zero results is a FAILED result (not a success with an empty list): the
    # search came up empty, which does not mean the fact doesn't exist. The
    # failure steers the planner to reformulate instead of the summary reporting
    # an authoritative "no results were found" (live bug: the FIFA question).
    _set_search([])
    result = await _web_search().execute(query="nothing")
    assert not result.success
    assert "no results" in result.error.lower()


async def test_web_search_provider_error_is_clean_failure():
    def boom(q, n):
        raise RuntimeError("provider down")

    browser_tools.SEARCH_PROVIDER_FACTORY = boom
    result = await _web_search().execute(query="q")
    assert not result.success
    assert "failed" in result.error.lower()


# ==================================================== layered provider chain
# These exercise the REAL default path (no SEARCH_PROVIDER_FACTORY) by patching
# the provider list — no network is touched.

async def test_real_search_returns_first_nonempty_provider(monkeypatch):
    calls = []

    async def empty(q, n):
        calls.append("tavily")
        return []

    async def rows(q, n):
        calls.append("ddg-html")
        return [{"title": "T", "url": "https://x.com", "snippet": "s"}]

    async def never(q, n):
        calls.append("ddg-lite")
        return [{"title": "N", "url": "https://n.com", "snippet": ""}]

    monkeypatch.setattr(browser_tools, "_SEARCH_PROVIDERS", [
        ("tavily", empty), ("duckduckgo-html", rows), ("duckduckgo-lite", never),
    ])
    out = await browser_tools._real_search("q", 5)
    assert [r["url"] for r in out] == ["https://x.com"]
    assert calls == ["tavily", "ddg-html"]  # stopped at the first non-empty


async def test_real_search_skips_a_failing_provider(monkeypatch):
    async def boom(q, n):
        raise RuntimeError("down")

    async def rows(q, n):
        return [{"title": "T", "url": "https://x.com", "snippet": "s"}]

    monkeypatch.setattr(browser_tools, "_SEARCH_PROVIDERS", [
        ("tavily", boom), ("duckduckgo-html", rows),
    ])
    out = await browser_tools._real_search("q", 5)
    assert out[0]["url"] == "https://x.com"


async def test_real_search_all_empty_returns_empty(monkeypatch):
    async def empty(q, n):
        return []

    monkeypatch.setattr(browser_tools, "_SEARCH_PROVIDERS", [("a", empty), ("b", empty)])
    assert await browser_tools._real_search("q", 5) == []


async def test_real_search_all_error_reraises_last(monkeypatch):
    async def boom(q, n):
        raise RuntimeError("provider down")

    monkeypatch.setattr(browser_tools, "_SEARCH_PROVIDERS", [("a", boom), ("b", boom)])
    with pytest.raises(RuntimeError):
        await browser_tools._real_search("q", 5)


async def test_web_search_empty_chain_is_failure(monkeypatch):
    # End to end through execute() with the real chain: every provider empty →
    # a failed ToolResult that steers a retry.
    async def empty(q, n):
        return []

    monkeypatch.setattr(browser_tools, "_SEARCH_PROVIDERS", [("a", empty)])
    result = await _web_search().execute(query="obscure nonsense query")
    assert not result.success
    assert "no results" in result.error.lower()


async def test_tavily_skipped_without_key(monkeypatch):
    # No key → Tavily is a no-op returning [], so the chain falls to DuckDuckGo
    # and the default install never calls the API.
    monkeypatch.setattr(browser_tools.settings, "TAVILY_API_KEY", "")
    assert await browser_tools._tavily_search("q", 5) == []


async def test_google_cse_skipped_without_both_credentials(monkeypatch):
    # Key-gated like Tavily: missing EITHER the key or the engine id → no-op [],
    # so the default install never calls Google and the chain falls through.
    monkeypatch.setattr(browser_tools.settings, "GOOGLE_SEARCH_API_KEY", "k")
    monkeypatch.setattr(browser_tools.settings, "GOOGLE_SEARCH_CX", "")
    assert await browser_tools._google_cse_search("q", 5) == []
    monkeypatch.setattr(browser_tools.settings, "GOOGLE_SEARCH_API_KEY", "")
    monkeypatch.setattr(browser_tools.settings, "GOOGLE_SEARCH_CX", "cx")
    assert await browser_tools._google_cse_search("q", 5) == []


def test_google_cse_rows_maps_items_and_is_preferred_in_the_chain():
    rows = browser_tools._google_cse_rows(
        {"items": [
            {"title": "Black Clover", "link": "https://x.test/bc",
             "snippet": "the latest episode is 170"},
            {"link": "ftp://skip.me", "snippet": "bad scheme is dropped"},
        ]},
        5,
    )
    assert len(rows) == 1
    assert rows[0]["url"] == "https://x.test/bc"
    assert "170" in rows[0]["content"] and "170" in rows[0]["snippet"]
    assert rows[0]["truncated"] is False
    # Google is first — preferred when configured (the fresher-index fix).
    assert browser_tools._SEARCH_PROVIDERS[0][0] == "google-cse"


# ============================================================= read_webpage

_HTML = """
<html><head><title>My  Page</title><style>.x{color:red}</style></head>
<body>
  <script>evil()</script>
  <h1>Heading</h1>
  <p>First paragraph with <b>bold</b> text.</p>
  <p>Second paragraph &amp; an entity.</p>
</body></html>
"""


async def test_read_webpage_extracts_readable_text():
    _set_fetch(FetchedPage(
        url="https://example.com/page", status_code=200,
        content_type="text/html; charset=utf-8", text=_HTML,
    ))
    result = await _read_webpage().execute(url="https://example.com/page")
    assert result.success
    assert result.output["title"] == "My Page"
    content = result.output["content"]
    assert "Heading" in content
    assert "First paragraph with bold text." in content
    assert "Second paragraph & an entity." in content
    assert "evil()" not in content          # script dropped
    assert "color:red" not in content       # style dropped


async def test_read_webpage_bare_host_gets_https():
    seen = {}

    def fake(url):
        seen["url"] = url
        return FetchedPage(url=url, status_code=200, content_type="text/html", text=_HTML)

    _set_fetch(fake)
    result = await _read_webpage().execute(url="example.com/doc")
    assert result.success
    assert seen["url"].startswith("https://")


async def test_read_webpage_rejects_non_text():
    _set_fetch(FetchedPage(
        url="https://x.com/f.pdf", status_code=200,
        content_type="application/pdf", text="%PDF-1.4...",
    ))
    result = await _read_webpage().execute(url="https://x.com/f.pdf")
    assert not result.success
    assert "readable web page" in result.error


async def test_read_webpage_rejects_http_error():
    _set_fetch(FetchedPage(
        url="https://x.com/missing", status_code=404,
        content_type="text/html", text="<html>nope</html>",
    ))
    result = await _read_webpage().execute(url="https://x.com/missing")
    assert not result.success
    assert "404" in result.error


async def test_read_webpage_rechecks_redirect_target():
    # Input URL is public, but the response's FINAL url is loopback.
    _set_fetch(FetchedPage(
        url="http://127.0.0.1/secret", status_code=200,
        content_type="text/html", text=_HTML,
    ))
    result = await _read_webpage().execute(url="https://public.example/redir")
    assert not result.success
    assert "blocked" in result.error.lower()


async def test_read_webpage_empty_content_fails():
    _set_fetch(FetchedPage(
        url="https://x.com", status_code=200,
        content_type="text/html", text="<html><body></body></html>",
    ))
    result = await _read_webpage().execute(url="https://x.com")
    assert not result.success


# =============================================================== SSRF guard

@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "http://localhost/x",
    "http://127.0.0.1/x",
    "http://10.0.0.5/x",
    "http://192.168.1.10/x",
    "http://169.254.169.254/latest/meta-data",  # cloud metadata
    "http://[::1]/x",                            # ipv6 loopback
])
def test_validate_url_blocks_unsafe(url):
    normalized, error = _validate_url(url)
    assert normalized is None
    assert error


def test_validate_url_allows_public_literal_ip():
    normalized, error = _validate_url("http://8.8.8.8/x")
    assert error is None
    assert normalized == "http://8.8.8.8/x"


def test_host_is_blocked_literals():
    assert _host_is_blocked("localhost")
    assert _host_is_blocked("127.0.0.1")
    assert _host_is_blocked("10.1.2.3")
    assert not _host_is_blocked("8.8.8.8")


# ========================================================= DuckDuckGo parse

_DDG_HTML = """
<div class="result results_links web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example <b>Title</b></a>
  </h2>
  <a class="result__snippet" href="x">This is the <b>snippet</b> text.</a>
</div>
<div class="result">
  <h2 class="result__title">
    <a class="result__a" href="https://direct.example/two">Second</a>
  </h2>
  <a class="result__snippet">Second snippet.</a>
</div>
"""


def test_parse_ddg_results():
    rows = _parse_ddg_results(_DDG_HTML, 10)
    assert len(rows) == 2
    assert rows[0]["url"] == "https://example.com/page"
    assert rows[0]["title"] == "Example Title"
    assert rows[0]["snippet"] == "This is the snippet text."
    assert rows[1]["url"] == "https://direct.example/two"


def test_parse_ddg_results_respects_limit():
    assert len(_parse_ddg_results(_DDG_HTML, 1)) == 1


_DDG_LITE_HTML = """
<table>
  <tr><td>
    <a rel="nofollow" class="result-link"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fp&amp;rut=x">Lite <b>Title</b></a>
  </td></tr>
  <tr><td class="result-snippet">Lite snippet text.</td></tr>
  <tr><td>
    <a class="result-link" href="https://direct.example/two">Second</a>
  </td></tr>
  <tr><td class="result-snippet">Second lite snippet.</td></tr>
</table>
"""


def test_parse_ddg_lite_results():
    rows = browser_tools._parse_ddg_lite_results(_DDG_LITE_HTML, 10)
    assert len(rows) == 2
    assert rows[0]["url"] == "https://example.com/p"
    assert rows[0]["title"] == "Lite Title"
    assert rows[0]["snippet"] == "Lite snippet text."
    assert rows[1]["url"] == "https://direct.example/two"


def test_parse_ddg_lite_results_respects_limit():
    assert len(browser_tools._parse_ddg_lite_results(_DDG_LITE_HTML, 1)) == 1


def test_decode_ddg_href_variants():
    assert _decode_ddg_href(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx&rut=z"
    ) == "https://a.com/x"
    assert _decode_ddg_href("https://direct.com/y") == "https://direct.com/y"
    assert _decode_ddg_href("//cdn.example/z").startswith("https://")


def test_extract_readable_plain():
    title, text = extract_readable(_HTML)
    assert title == "My Page"
    assert "Heading" in text


# ================================================ registry / permission

def test_both_tools_are_read_level():
    assert _web_search().permission_level == PermissionLevel.READ
    assert _read_webpage().permission_level == PermissionLevel.READ


async def test_read_tools_run_without_approval(db_session):
    _set_search([{"title": "T", "url": "https://t.com", "snippet": "s"}])
    result = await execute_tool("web_search", {"query": "q"}, db_session, approved=False)
    assert result.success  # READ tools never hit the approval gate


def test_descriptions_carry_untrusted_framing():
    for tool in (_web_search(), _read_webpage()):
        desc = tool.definition().description.lower()
        assert "data" in desc
        assert "never" in desc


# =================================================================== render

def test_formatters_registered():
    assert "web_search" in _RESULT_FORMATTERS
    assert "read_webpage" in _RESULT_FORMATTERS


def test_fmt_web_search():
    out = _RESULT_FORMATTERS["web_search"]({
        "results": [{"title": "T", "url": "https://u.com", "snippet": "sn"}],
        "count": 1,
    })
    assert "T" in out and "https://u.com" in out and "sn" in out


def test_fmt_read_webpage_fences_content():
    out = _RESULT_FORMATTERS["read_webpage"]({
        "title": "Page", "url": "https://u.com", "content": "hello world",
    })
    assert "Page" in out and "hello world" in out and "```" in out


# ================================================= content budget (2026-07-16)
# Regression suite for the FIFA fabrication: Tavily returns clean extracted page
# CONTENT (the whole reason it is first in the chain) and the mapping reused
# SNIPPET_MAX_CHARS=300 — a cap sized for DuckDuckGo's short scraped teasers —
# throwing the rest away. These run against the PURE _tavily_rows mapper, the
# seam whose absence let `content[:300]` ship uncaught.

def _tavily_body(content: str, n: int = 1) -> dict:
    return {"results": [
        {"title": f"T{i}", "url": f"https://ex.com/{i}", "content": content}
        for i in range(n)
    ]}


def test_tavily_rows_keeps_full_content():
    """The direct regression for content[:300]: substantive content survives up
    to its OWN budget, and the short preview stays short."""
    rows = browser_tools._tavily_rows(_tavily_body("y" * 5000), 5)
    assert len(rows[0]["content"]) == browser_tools.CONTENT_MAX_CHARS
    assert rows[0]["truncated"] is True
    assert len(rows[0]["snippet"]) <= browser_tools.SNIPPET_MAX_CHARS
    # The whole point: far more than the old 300 chars now reaches the record.
    assert len(rows[0]["content"]) > browser_tools.SNIPPET_MAX_CHARS * 3


def test_tavily_rows_short_content_not_truncated():
    rows = browser_tools._tavily_rows(_tavily_body("short body"), 5)
    assert rows[0]["content"] == "short body"
    assert rows[0]["truncated"] is False


def test_tavily_rows_marks_the_fifa_shape():
    """The incident, frozen. This is the VERBATIM 300-char fragment the old code
    stored for FIFA's qualified-teams page — cut mid-word exactly where the team
    list began. Zero team names survived and the summary invented 112 countries.
    The page itself is far longer, so the row must now carry real content AND
    admit that it was cut."""
    real_page = (
        "# Qualified teams for the FIFA World Cup 2026. Look ahead to the global "
        "showpiece in Canada, Mexico and the United States with details on the "
        "teams who have booked their ticket to the tournament. Image 3: FIFA "
        "World Cup 26 qualified teams wallchart graphic 16x9. ## **FIFA World "
        "Cup 2026™ qualified teams** " + ", ".join(
            ["Argentina", "Brazil", "Japan", "Morocco", "England"] * 40
        )
    )
    rows = browser_tools._tavily_rows(_tavily_body(real_page), 5)
    assert rows[0]["truncated"] is True
    # The evidence the old cap destroyed is now in the record.
    assert "Argentina" in rows[0]["content"]
    assert len(rows[0]["content"]) > 300


def test_tavily_rows_skips_non_http_urls():
    body = {"results": [
        {"title": "bad", "url": "javascript:alert(1)", "content": "x"},
        {"title": "ok", "url": "https://ok.com", "content": "y"},
    ]}
    rows = browser_tools._tavily_rows(body, 5)
    assert [r["url"] for r in rows] == ["https://ok.com"]


def test_tavily_rows_respects_max_results():
    assert len(browser_tools._tavily_rows(_tavily_body("c", n=9), 3)) == 3


def test_ddg_rows_unchanged_by_the_content_budget():
    """Keyless-install regression: DuckDuckGo's scraped snippet is genuinely all
    it has, so content="" is the HONEST row — not something we cut. That
    distinction drives the escalation downstream."""
    rows = _parse_ddg_results(_DDG_HTML, 10)
    assert rows[0]["content"] == ""
    assert rows[0]["truncated"] is False
    assert len(rows[0]["snippet"]) <= browser_tools.SNIPPET_MAX_CHARS
    assert rows[0]["snippet"]  # the teaser still renders


async def test_tavily_request_uses_advanced_depth_and_no_raw_content(monkeypatch):
    """search_depth must be 'advanced' (better extraction — the reason Tavily is
    first), and include_raw_content must NOT be set: it has no per-result flag,
    so it would return full page markdown for EVERY result and blow every
    downstream budget."""
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return _tavily_body("body")

    class _Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **kw):
            sent.update(json or {})
            return _Resp()

    import httpx
    monkeypatch.setattr(browser_tools.settings, "TAVILY_API_KEY", "k")
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await browser_tools._tavily_search("q", 5)
    assert sent["search_depth"] == "advanced"
    assert "include_raw_content" not in sent


# ==================================== truth in rendering (live bug 2026-07-16)

def test_fmt_web_search_marks_truncation_without_naming_tools():
    """An unmarked cut is worse than a thin record: SUMMARY_PROMPT tells the
    model 'only call a list truncated if the results say so — otherwise it is
    complete', so silence CERTIFIED the FIFA fragment as the whole list. The
    marker states the fact and ONLY the fact — the remedy is automatic
    (evidence_resolver), and naming it here leaks tool names into the reply."""
    out = _RESULT_FORMATTERS["web_search"]({
        "results": [{
            "title": "Qualified teams", "url": "https://fifa.com/x",
            "snippet": "s", "content": "A" * 900, "truncated": True,
        }],
        "count": 1,
    })
    assert "partial extract" in out.lower()
    assert "not the whole page" in out.lower()
    # The marker must NOT name a tool: the summary LLM copies the record's own
    # words into the user's answer, and that prose must never mention tools
    # (live verification 2026-07-16: it leaked "read_webpage on the URL returns
    # the rest" a dozen times into the reply).
    assert "read_webpage" not in out
    assert "https://fifa.com/x" in out


def test_fmt_web_search_does_not_mark_untruncated_content():
    out = _RESULT_FORMATTERS["web_search"]({
        "results": [{
            "title": "T", "url": "https://u.com",
            "snippet": "s", "content": "B" * 900, "truncated": False,
        }],
        "count": 1,
    })
    assert "partial extract" not in out.lower()
    assert "B" * 50 in out


def test_fmt_web_search_fences_long_untrusted_content():
    """Web content is untrusted prose that carries its own markdown — the real
    FIFA snippet literally contained '##' headers. Inlining it would inject the
    page author's heading structure into the record and the user's chat."""
    out = _RESULT_FORMATTERS["web_search"]({
        "results": [{
            "title": "T", "url": "https://u.com", "snippet": "s",
            "content": "## Their Heading\n" + "c" * 900, "truncated": False,
        }],
        "count": 1,
    })
    assert "```" in out


def test_fmt_web_search_keeps_short_results_inline():
    """Back-compat: a short DDG-shaped result renders exactly as before —
    teaser inline on the bullet, no fence."""
    out = _RESULT_FORMATTERS["web_search"]({
        "results": [{"title": "T", "url": "https://u.com", "snippet": "sn",
                     "content": "", "truncated": False}],
        "count": 1,
    })
    assert "- **T** — https://u.com: sn" in out
    assert "```" not in out


# ====================================================== query fan-out (L1)
#
# THE INCIDENT (live, 2026-07-17). "which teams have qualified for fifa
# worldcup final 2026" answered with the 48 teams that qualified for the
# TOURNAMENT — two days before a two-team final. Reworded to "which teams are
# playing fifa final 2026" it answered correctly. Google answers both, and not
# by understanding better: it fans the question out into several readings and
# lets the evidence settle it.
#
# The sentence "'who is in the final' is not 'who qualified'" was ALREADY in
# planner rule 16, verbatim, when this happened — which is why the fix is
# structural rather than a better rule. The model is no longer asked to PICK
# the right reading (a precision problem it loses ~half the time) but to
# ENUMERATE readings: the right one need only appear, never be chosen.

# The user's exact words, frozen.
INCIDENT_PROMPT = "which teams have qualified for fifa worldcup final 2026"
INCIDENT_PROMPT_REWORDED = "which teams teams are playing fifa final 2026"


def _fanout_provider(pages: dict):
    """A provider whose answer depends on the query — the whole point being that
    different readings reach different pages. Returns the recorded calls."""
    calls: list[str] = []

    def fake(q, n):
        calls.append(q)
        for needle, rows in pages.items():
            if needle in q.lower():
                return list(rows)[:n]
        return []

    browser_tools.SEARCH_PROVIDER_FACTORY = fake
    return calls


def _wrow(url, title="T", content="", snippet="s", truncated=False):
    return {"title": title, "url": url, "snippet": snippet,
            "content": content, "truncated": truncated}


async def test_fanout_covers_both_readings_of_the_incident_question():
    """The incident, frozen: the ambiguous wording now reaches BOTH pages.

    This is the thesis — not that Jarvis picks right, but that the right page is
    IN the evidence for the summary to pick from."""
    calls = _fanout_provider({
        "playing": [_wrow("https://w.org/final", "Final", "Spain v Argentina " * 60)],
        "qualified": [_wrow("https://w.org/qualification", "Qualification", "48 teams " * 60)],
    })
    result = await _web_search().execute(queries=[
        "which teams are playing the 2026 FIFA World Cup final",
        "which teams qualified for the 2026 FIFA World Cup",
    ])
    assert result.success
    assert len(calls) == 2, "each reading runs its own search"
    urls = {r["url"] for r in result.output["results"]}
    assert urls == {"https://w.org/final", "https://w.org/qualification"}


async def test_fanout_ranks_a_page_several_readings_agree_on_first():
    """RRF's defining property, and what makes the FANOUT_MERGED_MAX cut safe:
    corroboration across readings outranks being #1 for exactly one reading."""
    shared = _wrow("https://fifa.com/wc", "Shared")
    _fanout_provider({
        "playing": [_wrow("https://a.com/only"), shared],
        "qualified": [_wrow("https://b.com/only"), shared],
    })
    result = await _web_search().execute(queries=["playing x", "qualified y"])
    assert result.output["results"][0]["url"] == "https://fifa.com/wc"
    assert len(result.output["results"][0]["found_by"]) == 2


async def test_fanout_merges_the_same_page_found_by_two_readings():
    """Dedupe is on the NORMALIZED url: providers routinely return one article
    with a trailing slash or tracking params. Keeping both would spend a row of
    the render budget twice on the same page."""
    _fanout_provider({
        "playing": [_wrow("https://fifa.com/wc/")],
        "qualified": [_wrow("https://fifa.com/wc?utm_source=x")],
    })
    result = await _web_search().execute(queries=["playing x", "qualified y"])
    assert result.output["count"] == 1


async def test_fanout_keeps_the_richer_copy_of_a_shared_page():
    """Each reading runs the provider chain independently, so one may be
    answered by Tavily (real content) and another by a DDG scraper (content="").
    The merge keeps the evidence, not whichever finished first."""
    _fanout_provider({
        "thin": [_wrow("https://p.com", content="", snippet="teaser")],
        "rich": [_wrow("https://p.com", content="R" * 900, snippet="a much longer preview")],
    })
    result = await _web_search().execute(queries=["thin q", "rich q"])
    row = result.output["results"][0]
    assert len(row["content"]) == 900
    assert row["snippet"] == "a much longer preview"


async def test_fanout_truncated_travels_with_the_content_it_describes():
    """`truncated` is a fact recorded at the cut site. Taking richer content
    without its marker would misreport whether the record is whole — the exact
    silence that let the summary certify a fragment as complete (2026-07-16)."""
    _fanout_provider({
        "short": [_wrow("https://p.com", content="s" * 10, truncated=False)],
        "cut": [_wrow("https://p.com", content="c" * 900, truncated=True)],
    })
    result = await _web_search().execute(queries=["short q", "cut q"])
    row = result.output["results"][0]
    assert row["content"] == "c" * 900 and row["truncated"] is True


async def test_fanout_cap_is_enforced_in_code_not_trusted_from_the_model():
    """The planner proposes readings; it does not decide how many searches run."""
    calls = _fanout_provider({"q": [_wrow("https://x.com")]})
    await _web_search().execute(queries=[f"q{i}" for i in range(20)])
    assert len(calls) == browser_tools.FANOUT_MAX_QUERIES


async def test_fanout_merged_set_is_capped_to_protect_the_render_budget():
    """THE TRAP. The original FIFA fabrication was content STARVATION. Five
    queries x five results at CONTENT_MAX_CHARS each overflows what the renderer
    gives web_search, and the fair-share allocator would clip — re-creating that
    bug with MORE sources feeding it. The merge caps first."""
    pages = {f"q{i}": [_wrow(f"https://s{i}.com/{j}", content="c" * 900)
                       for j in range(5)] for i in range(5)}
    _fanout_provider(pages)
    result = await _web_search().execute(queries=[f"q{i}" for i in range(5)])
    assert result.output["count"] <= browser_tools.FANOUT_MERGED_MAX


def test_fanout_render_budget_invariant_holds():
    """The invariant the cap rests on, asserted rather than trusted to a
    comment: if either constant moves, this fails loudly instead of silently
    starving the record."""
    from app.agents.rendering import _STEP_RESULT_CAPS
    assert (_STEP_RESULT_CAPS["web_search"]
            >= browser_tools.FANOUT_MERGED_MAX * browser_tools.CONTENT_MAX_CHARS)


async def test_fanout_survives_one_dead_reading():
    """One bad query or one flaky provider must not lose the readings that
    worked — the answer may well be in them."""
    def fake(q, n):
        if "bad" in q:
            raise RuntimeError("provider down")
        return [_wrow("https://good.com")]

    browser_tools.SEARCH_PROVIDER_FACTORY = fake
    result = await _web_search().execute(queries=["bad q", "good q"])
    assert result.success
    assert result.output["results"][0]["url"] == "https://good.com"


async def test_fanout_all_readings_failing_is_an_infrastructure_failure():
    """Distinct from "found nothing": every reading erroring means the search is
    broken, not that the fact does not exist."""
    def boom(q, n):
        raise RuntimeError("provider down")

    browser_tools.SEARCH_PROVIDER_FACTORY = boom
    result = await _web_search().execute(queries=["a", "b"])
    assert not result.success
    assert "failed" in result.error.lower()


async def test_fanout_all_readings_empty_still_steers_a_retry():
    """Every reading cleanly finding nothing keeps the "reword and retry"
    failure — never an authoritative "this does not exist"."""
    browser_tools.SEARCH_PROVIDER_FACTORY = lambda q, n: []
    result = await _web_search().execute(queries=["a", "b"])
    assert not result.success
    assert "no results" in result.error.lower()


async def test_single_query_behaviour_is_unchanged():
    """Back-compat: one reading keeps the numbers it always had — the merge is
    rank-preserving for a single input, and max_results still rules (the
    FANOUT_MERGED_MAX cut applies only when fanning out)."""
    _set_search([_wrow(f"https://x/{i}") for i in range(10)])
    result = await _web_search().execute(query="unambiguous", max_results=10)
    assert result.output["count"] == 10
    assert result.output["query"] == "unambiguous"
    assert result.output["results"][0]["url"] == "https://x/0"


async def test_blank_and_duplicate_readings_collapse():
    calls = _fanout_provider({"real": [_wrow("https://x.com")]})
    result = await _web_search().execute(queries=["real q", "  ", "REAL Q", ""])
    assert len(calls) == 1
    assert result.success


async def test_queries_wins_over_query_when_both_are_sent():
    calls = _fanout_provider({"q": [_wrow("https://x.com")]})
    await _web_search().execute(query="ignored", queries=["q one", "q two"])
    assert calls == ["q one", "q two"]


async def test_empty_queries_falls_back_to_query():
    calls = _fanout_provider({"only": [_wrow("https://x.com")]})
    await _web_search().execute(query="only this", queries=[])
    assert calls == ["only this"]


async def test_fanout_definition_advertises_queries():
    params = _web_search().definition().parameters["properties"]
    assert "queries" in params and params["queries"]["type"] == "array"


def test_normalize_url_is_the_one_notion_of_same_page():
    """Shared with evidence_resolver deliberately — two copies of this rule
    would be free to drift, and then the merge and the fetcher would disagree
    about what "already read" means."""
    n = browser_tools.normalize_url
    assert n("https://A.com/x/") == n("https://a.com/x") == n("https://a.com/x?utm=1")
    assert n("https://a.com/x") != n("https://a.com/y")


def test_fmt_web_search_names_the_readings_when_a_question_was_ambiguous():
    """The merged rows alone would hide that the wording admitted two answers —
    the summary would see one pile of pages and could not offer the reading it
    did not lead with."""
    out = _RESULT_FORMATTERS["web_search"]({
        "queries": ["who is playing the final", "who qualified for the cup"],
        "results": [{"title": "T", "url": "https://u.com", "snippet": "sn",
                     "content": "", "truncated": False}],
        "count": 1,
    })
    assert "read more than one way" in out
    assert "who is playing the final" in out and "who qualified for the cup" in out


def test_fmt_web_search_stays_quiet_for_a_single_reading():
    """An unambiguous question must not be told it was ambiguous."""
    out = _RESULT_FORMATTERS["web_search"]({
        "queries": ["one clear question"],
        "results": [{"title": "T", "url": "https://u.com", "snippet": "sn",
                     "content": "", "truncated": False}],
        "count": 1,
    })
    assert "read more than one way" not in out

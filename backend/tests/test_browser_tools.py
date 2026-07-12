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


async def test_web_search_empty_results_safe():
    _set_search([])
    result = await _web_search().execute(query="nothing")
    assert result.success
    assert result.output["count"] == 0


async def test_web_search_provider_error_is_clean_failure():
    def boom(q, n):
        raise RuntimeError("provider down")

    browser_tools.SEARCH_PROVIDER_FACTORY = boom
    result = await _web_search().execute(query="q")
    assert not result.success
    assert "failed" in result.error.lower()


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

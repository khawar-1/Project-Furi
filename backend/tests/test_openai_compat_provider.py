"""
Tests for the OpenAI-compatible provider (DeepSeek et al.).

Focus: a 4xx from the API must surface the provider's OWN error body — not the
opaque httpx "Client error '400 Bad Request'" that hid the real reason live
(2026-07-24) — plus the two defensive request guards (max_tokens ceiling,
empty-content filtering). All hermetic via httpx.MockTransport; no network.
"""
import httpx
import pytest

from app.providers.base import LLMMessage
from app.providers.openai_compat import _MAX_OUTPUT_TOKENS, OpenAICompatProvider


def _provider(handler) -> OpenAICompatProvider:
    """A provider whose internal client is swapped for a mock transport."""
    p = OpenAICompatProvider(
        api_key="test-key",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        provider_name="deepseek",
    )
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


async def test_chat_400_surfaces_the_deepseek_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Content Exists Risk",
                            "type": "invalid_request_error"}},
        )

    provider = _provider(handler)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await provider.chat([LLMMessage(role="user", content="hi")])
    text = str(exc.value)
    assert "Content Exists Risk" in text          # the API's own message
    assert "400" in text and "deepseek" in text    # legible, attributed


async def test_stream_400_surfaces_the_body_too():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "max_tokens is too large"}}
        )

    provider = _provider(handler)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        async for _ in provider.stream_chat([LLMMessage(role="user", content="hi")]):
            pass
    assert "max_tokens is too large" in str(exc.value)


async def test_400_with_a_non_json_body_still_raises_legibly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="upstream proxy error")

    provider = _provider(handler)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await provider.chat([LLMMessage(role="user", content="hi")])
    assert "upstream proxy error" in str(exc.value)


async def test_successful_chat_returns_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "hello sir"}}],
                "usage": {"total_tokens": 12},
            },
        )

    provider = _provider(handler)
    resp = await provider.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == "hello sir"
    assert resp.tokens_used == 12


async def test_max_tokens_is_clamped_to_the_ceiling():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "deepseek-chat",
                  "choices": [{"message": {"content": "ok"}}]},
        )

    provider = _provider(handler)
    # An over-ceiling request must be clamped, not sent as-is (a 400 trigger).
    await provider.chat([LLMMessage(role="user", content="hi")], max_tokens=100_000)
    assert seen["payload"]["max_tokens"] == _MAX_OUTPUT_TOKENS
    # None → a valid default, not null.
    await provider.chat([LLMMessage(role="user", content="hi")])
    assert seen["payload"]["max_tokens"] == _MAX_OUTPUT_TOKENS


def test_to_messages_drops_empty_content_but_never_empties_the_list():
    provider = OpenAICompatProvider(
        api_key="k", model="deepseek-chat",
        base_url="https://api.deepseek.com", provider_name="deepseek",
    )
    out = provider._to_messages([
        LLMMessage(role="system", content="you are jarvis"),
        LLMMessage(role="assistant", content="   "),   # blank — dropped
        LLMMessage(role="user", content="play one piece"),
    ])
    assert [m["content"] for m in out] == ["you are jarvis", "play one piece"]

    # All-blank falls back to the raw mapping so the API reports the real issue.
    only_blank = provider._to_messages([LLMMessage(role="user", content="")])
    assert only_blank == [{"role": "user", "content": ""}]


# ============================ transport failures (2026-07-26 DNS-outage incident)
# A machine-wide DNS outage made the planner log
#     `Planner LLM call failed (attempt 1): `
# — nothing after the colon, because that httpx exception's str() was EMPTY. Half
# the failure was undiagnosable. Same class as the 400 legibility round above,
# which fixed HTTP STATUS errors and left TRANSPORT errors to whatever str() gave.


def _dns_failure() -> httpx.ConnectError:
    """The incident's shape: an httpx transport error with an empty message,
    wrapping the OSError that actually carries WSAHOST_NOT_FOUND."""
    failure = httpx.ConnectError("")
    failure.__cause__ = OSError(11001, "getaddrinfo failed")
    return failure


async def test_chat_transport_failure_is_never_an_empty_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise _dns_failure()

    provider = _provider(handler)
    with pytest.raises(RuntimeError) as exc:
        await provider.chat([LLMMessage(role="user", content="hi")])

    text = str(exc.value)
    assert text.strip()                       # the whole point — never empty
    assert "could not be reached" in text
    assert "deepseek" in text                 # attributed
    assert "ConnectError" in text             # the class survives an empty str()
    # the original is chained, so callers classifying by walking __cause__ still see it
    assert isinstance(exc.value.__cause__, httpx.ConnectError)


async def test_stream_transport_failure_is_normalized_too():
    def handler(request: httpx.Request) -> httpx.Response:
        raise _dns_failure()

    provider = _provider(handler)
    with pytest.raises(RuntimeError) as exc:
        async for _ in provider.stream_chat([LLMMessage(role="user", content="hi")]):
            pass
    assert "could not be reached" in str(exc.value)


async def test_a_normalized_transport_error_is_still_classified_as_transport():
    """The provider's RuntimeError must not hide the network from the planner's
    retry/backoff classifier — that would put a DNS outage back on the 'the model
    failed' path and drop the backoff."""
    from app.agents.planner import _is_transport_error

    def handler(request: httpx.Request) -> httpx.Response:
        raise _dns_failure()

    provider = _provider(handler)
    try:
        await provider.chat([LLMMessage(role="user", content="hi")])
    except RuntimeError as exc:
        assert _is_transport_error(exc)
    else:
        pytest.fail("expected the normalized transport error")


async def test_http_status_errors_are_untouched_by_the_transport_normalization():
    """A 400 is the MODEL/request failing, not the network: it must keep the
    legible body the 2026-07-24 round gave it and stay an HTTPStatusError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Content Exists Risk"}})

    provider = _provider(handler)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await provider.chat([LLMMessage(role="user", content="hi")])
    assert "Content Exists Risk" in str(exc.value)

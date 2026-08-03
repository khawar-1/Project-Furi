"""
Jarvis OS — The remote listener (2026-08-03)

*(Tier 2, item 5 — "one machine, one room")*

A SECOND FastAPI app, on a second port, carrying ONLY the routes named in
`remote_manifest.REMOTE_ROUTES`. The main app is untouched and stays on
loopback.

⚠️ WHY A SECOND APP RATHER THAN A SMARTER MIDDLEWARE. The alternative was one
app whose auth middleware returns a scoped principal and 403s anything off the
allowlist. That works right up until it doesn't: a middleware ordering change, a
route registered before the gate, an exception path that returns early — and the
whole API is on the LAN. Here the guarantee is not "a check refuses it", it is
**the route is not there**. `/chat/stream` 404s on the remote port for the same
reason it 404s on a webserver that never heard of Jarvis.

That is the same move as `registry.execute_tool` refusing structurally instead
of by prompt, and as READ-mode browsing: prefer the guarantee you cannot code
your way around.

WHAT IS SHARED, DELIBERATELY
----------------------------
The route OBJECTS are the same ones the main app serves — same handlers, same
dependencies, same approval gate underneath. Copying them rather than
re-declaring them means the remote surface cannot drift into a second
implementation of approve, which would be the worst of all worlds.

The LIFESPAN is deliberately NOT shared: this app is mounted into an already-
running process, so it must not run startup again (scheduler, migrations, model
warm-up). It borrows the process it is started from.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from loguru import logger

from app.core.remote_manifest import is_remote_route


class RemoteAuthMiddleware:
    """Pure-ASGI token gate for the remote listener.

    A separate class from `auth.AuthMiddleware` on purpose: that one answers
    "is this THE machine token?" and grants everything, which is exactly what a
    phone must not have. This one accepts only a paired DEVICE token, verified
    against its stored hash.

    ⚠️ It is a second lock on a door that is already narrow — the routes behind
    it are the manifest's, whatever this middleware does. Fails closed on
    anything it cannot resolve, and `/health` is exempt so a phone can find the
    machine before it has a token.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # `/` is the page itself, and it must load WITHOUT a token — it is where
        # the token gets installed, from the QR link's fragment. The page is
        # inert: every piece of data on it comes from an authed call below.
        if path in ("/health", "/") or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        device = await self._device_for(scope)
        if device is not None:
            await self.app(scope, receive, send)
            return

        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"detail": "This device is not paired, or its pairing has expired."},
            status_code=401,
        )
        await response(scope, receive, send)

    async def _device_for(self, scope) -> Optional[object]:
        """Resolve the presented token to a live device. Any failure — a bad
        header, an unreadable database — denies rather than raising."""
        from app.core.auth import _header_token, _query_token
        from app.core.remote_tokens import touch_device, verify_token
        from app.db.database import AsyncSessionLocal

        token = _header_token(scope) or _query_token(scope)
        if not token:
            return None
        try:
            async with AsyncSessionLocal() as db:
                device = await verify_token(db, token)
                if device is not None:
                    await touch_device(db, device.id)
                return device
        except Exception as e:  # noqa: BLE001 — deny, never crash the request
            logger.error(f"Remote token verification failed (denying): {e}")
            return None


def create_remote_app(source: FastAPI) -> FastAPI:
    """Build the remote listener by copying the manifest's routes out of the
    real app. Nothing is re-declared — the handlers are the same objects."""
    remote = FastAPI(
        title="Jarvis OS — Remote",
        description="Read, approve, answer. Never a shell.",
        # No API explorer on a LAN port: it is a map of the surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    remote.add_middleware(RemoteAuthMiddleware)
    remote.add_middleware(
        CORSMiddleware,
        # The remote page is served from this same app, so a wildcard here is
        # not a hole — every request still needs a paired device token.
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # The page. Declared HERE rather than copied, because it exists only on
    # this listener — the desktop has its own UI and must never serve this one.
    @remote.get("/", include_in_schema=False)
    async def _page():  # pragma: no cover — exercised by the live check
        from starlette.responses import HTMLResponse

        from app.core.remote_page import REMOTE_PAGE

        return HTMLResponse(REMOTE_PAGE)

    kept: list[str] = []
    for route in source.routes:
        if not isinstance(route, APIRoute):
            continue  # WebSockets and mounts are never copied
        if not is_remote_route(route.methods, route.path):
            continue
        remote.router.routes.append(route)
        kept.append(f"{sorted(route.methods - {'HEAD', 'OPTIONS'})[0]} {route.path}")

    logger.info(f"🔒 Remote surface: {len(kept)} route(s) mounted, everything else absent")
    return remote


def remote_routes(app: FastAPI) -> set[tuple[str, str]]:
    """(method, path) actually mounted on a remote app — for tests that need to
    assert absence rather than trust the manifest."""
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                out.add((method, route.path))
    return out

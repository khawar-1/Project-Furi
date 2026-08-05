"""Runtime verification for Home & IoT (Feature 1).

A hermetic test cannot tell you the wiring BOOTS. This drives the REAL
main.py lifespan on an isolated port with a scratch DB, against a FAKE Home
Assistant hub served on localhost — so the whole chain is exercised end to
end: migrations, router mount, config persistence, the HTTP client, the tools,
and the structural approval gate.

⚠️ Everything runs in ONE process/invocation. The 2026-08-03 lesson: a probe
that stashed its token in a temp file between shell invocations sent every
request with an EMPTY token, got 401 on everything, and "all denied" read
exactly like success. A negative result is only evidence if the positive
control passes in the same breath.

Run:  venv\\Scripts\\python scripts\\_verify_home_runtime.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
HUB_PORT = 18123
API_PORT = 18001

# --------------------------------------------------------------- fake hub

_STATES = [
    {"entity_id": "light.kitchen_main", "state": "off",
     "attributes": {"friendly_name": "Kitchen Lights", "area": "Kitchen"}},
    {"entity_id": "lock.front_door", "state": "locked",
     "attributes": {"friendly_name": "Front Door", "area": "Hallway"}},
    {"entity_id": "climate.living_room", "state": "heat",
     "attributes": {"friendly_name": "Living Room Thermostat", "area": "Living Room",
                    "current_temperature": 19.5}},
]

SERVICE_CALLS: list[tuple[str, dict]] = []
EXPECTED_TOKEN = "fake-long-lived-token"


class _Hub(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _authed(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {EXPECTED_TOKEN}"

    def _send(self, code: int, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"message": "Unauthorized"})
        if self.path == "/api/config":
            return self._send(200, {"version": "2026.8.1"})
        if self.path == "/api/states":
            return self._send(200, _STATES)
        if self.path.startswith("/api/states/"):
            eid = self.path.rsplit("/", 1)[-1]
            row = next((s for s in _STATES if s["entity_id"] == eid), None)
            return self._send(200, row) if row else self._send(404, {})
        self._send(404, {})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"message": "Unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/api/services/"):
            SERVICE_CALLS.append((self.path.replace("/api/services/", ""), body))
            return self._send(200, [])
        self._send(404, {})


def start_hub() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", HUB_PORT), _Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------- the check

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))


async def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-home-verify-"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch / 'scratch.db'}"
    os.environ["QDRANT_HOST"] = ""            # no vector DB needed
    os.environ["HOME_ASSISTANT_TOKEN_PATH"] = str(scratch / "home_token.json")
    os.environ["BACKEND_PORT"] = str(API_PORT)
    os.environ["REMOTE_ENABLED"] = "false"
    os.environ["DEBUG"] = "false"  # SQL echo drowns the checks
    sys.path.insert(0, str(ROOT))

    start_hub()
    print(f"fake Home Assistant hub on :{HUB_PORT}")

    import uvicorn
    from main import app
    from app.core.auth import get_or_create_token

    config = uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.1)
    if not server.started:
        print("backend never started")
        return 1
    print(f"real backend lifespan booted on :{API_PORT}\n")

    import httpx

    token = get_or_create_token()
    headers = {"X-Jarvis-Token": token}
    base = f"http://127.0.0.1:{API_PORT}"

    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            # -- the router is mounted at all ------------------------------
            r = await http.get(f"{base}/api/home/settings")
            check(r.status_code == 200, "GET /api/home/settings is mounted",
                  f"HTTP {r.status_code}")
            check(r.json().get("enabled") is False,
                  "home control defaults OFF (opt-in)", str(r.json()))

            # -- validation is a 400, not a silent drop --------------------
            r = await http.put(f"{base}/api/home/settings", json={
                "enabled": True, "base_url": "ftp://nope"})
            check(r.status_code == 400, "a bad hub address is a 400", r.text[:80])

            r = await http.put(f"{base}/api/home/settings", json={
                "enabled": True, "base_url": ""})
            check(r.status_code == 400, "enabling with no address is a 400",
                  r.text[:80])

            # -- a real save, then a real probe ----------------------------
            r = await http.put(f"{base}/api/home/settings", json={
                "enabled": True,
                "base_url": f"http://127.0.0.1:{HUB_PORT}",

                "token": EXPECTED_TOKEN,
            })
            check(r.status_code == 200 and r.json()["configured"] is True,
                  "settings persist and report configured", r.text[:100])
            # ⚠️ The claim is that the token VALUE never comes back — not that
            # the word "token" is absent, which `has_token` legitimately
            # contains. The first version of this check asserted the latter and
            # manufactured a defect out of correct code (the ghost-file lesson:
            # a probe that asserts the wrong thing is worse than no probe).
            check(EXPECTED_TOKEN not in r.text,
                  "the access token VALUE is never returned on read",
                  "has_token flag only")

            token_file = Path(os.environ["HOME_ASSISTANT_TOKEN_PATH"])
            check(token_file.exists(), "the token is stored outside the database",
                  str(token_file))

            r = await http.post(f"{base}/api/home/test-connection")
            body = r.json()
            check(body.get("connected") is True,
                  "test-connection reaches the real hub", body.get("detail", ""))

            # -- the device list is real -----------------------------------
            r = await http.get(f"{base}/api/home/devices")
            body = r.json()
            ids = [d["entity_id"] for d in body.get("devices", [])]
            check("light.kitchen_main" in ids and len(ids) == 3,
                  "devices are read from the hub over HTTP", str(ids))
            check(any(d["name"] == "Kitchen Lights" for d in body["devices"]),
                  "friendly names survive the round trip")

            # -- the tools, through the REAL registry and gate --------------
            from app.db.database import AsyncSessionLocal
            from app.tools.registry import execute_tool

            async with AsyncSessionLocal() as db:
                res = await execute_tool("list_devices", {"area": "kitchen"}, db)
                check(res.success and res.output["count"] == 1,
                      "list_devices runs against the live hub",
                      str(res.output if res.success else res.error))

                before = len(SERVICE_CALLS)
                res = await execute_tool(
                    "set_device_state",
                    {"entity_id": "light.kitchen_main", "state": "on"},
                    db, approved=False,
                )
                check(
                    (not res.success) and res.requires_approval
                    and len(SERVICE_CALLS) == before,
                    "AN UNAPPROVED WRITE NEVER REACHES THE HOUSE",
                    f"calls before={before} after={len(SERVICE_CALLS)}",
                )

                res = await execute_tool(
                    "set_device_state",
                    {"entity_id": "light.kitchen_main", "state": "on",
                     "attributes": {"brightness_pct": 80, "explode": True}},
                    db, approved=True,
                )
                sent = SERVICE_CALLS[-1] if SERVICE_CALLS else ("", {})
                check(res.success and sent[0] == "light/turn_on",
                      "an APPROVED write reaches the hub as the mapped service",
                      str(sent))
                check("explode" not in sent[1] and sent[1].get("brightness_pct") == 80,
                      "unknown attributes are dropped in flight", str(sent[1]))

                res = await execute_tool(
                    "set_climate",
                    {"entity_id": "climate.living_room", "temperature": 21.5},
                    db, approved=True,
                )
                check(res.success and SERVICE_CALLS[-1][0] == "climate/set_temperature",
                      "set_climate reaches the thermostat", str(SERVICE_CALLS[-1]))

                res = await execute_tool(
                    "set_climate",
                    {"entity_id": "climate.living_room", "temperature": 220},
                    db, approved=True,
                )
                check(not res.success, "an absurd temperature fails rather than clamps",
                      (res.error or "")[:70])

            # -- disconnect clears the credential ---------------------------
            r = await http.put(f"{base}/api/home/settings", json={
                "enabled": False, "base_url": f"http://127.0.0.1:{HUB_PORT}",
                "token": ""})
            check(r.status_code == 200 and r.json()["has_token"] is False,
                  "clearing the token removes it", r.text[:80])
            check(not token_file.exists(),
                  "no stale credential is left on disk")
    finally:
        server.should_exit = True
        await task
        shutil.rmtree(scratch, ignore_errors=True)

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

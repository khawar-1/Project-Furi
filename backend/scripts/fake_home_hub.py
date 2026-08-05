"""A stand-in Home Assistant hub, for testing Feature 1 by hand.

Speaks the three REST endpoints `app/integrations/home_assistant.py` uses
(`/api/config`, `/api/states`, `/api/services/{domain}/{service}`) with a small
house full of devices, and PRINTS every service call it receives — so you can
watch exactly what left Jarvis and reached "the house", and confirm that
nothing reached it before you approved.

State is real: turning the kitchen light on here actually flips the state this
hub reports afterwards, so a follow-up "is the kitchen light on?" reads back
what you just did rather than a canned answer.

Run it in its own terminal, leave it running, then point Jarvis at it in
Settings -> Home & devices:

    cd backend
    venv\\Scripts\\python scripts\\fake_home_hub.py

    address:  http://127.0.0.1:18123
    token:    test-token          (any non-empty value this hub is told to expect)

⚠️ This is a TEST DOUBLE, not a hub. It binds 127.0.0.1 only and accepts one
hard-coded token. Do not use it for anything but driving the UI and chat by
hand.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "127.0.0.1"
PORT = 18123
TOKEN = "test-token"

# A house with the shapes that matter: two lights so "turn off the light" is
# genuinely ambiguous and must ask; a lock and a garage door so you can see a
# real approval card for something consequential; a thermostat for set_climate;
# scenes for run_scene; and a read-only sensor.
STATES: dict[str, dict] = {
    "light.kitchen_main": {
        "state": "off",
        "attributes": {"friendly_name": "Kitchen Lights", "area": "Kitchen",
                       "brightness": 0},
    },
    "light.living_room": {
        "state": "on",
        "attributes": {"friendly_name": "Living Room Lamp", "area": "Living Room",
                       "brightness": 180},
    },
    "light.bedroom": {
        "state": "off",
        "attributes": {"friendly_name": "Bedroom Light", "area": "Bedroom"},
    },
    "switch.coffee_machine": {
        "state": "off",
        "attributes": {"friendly_name": "Coffee Machine", "area": "Kitchen"},
    },
    "fan.office": {
        "state": "off",
        "attributes": {"friendly_name": "Office Fan", "area": "Office",
                       "percentage": 0},
    },
    "lock.front_door": {
        "state": "locked",
        "attributes": {"friendly_name": "Front Door", "area": "Hallway"},
    },
    "cover.garage_door": {
        "state": "closed",
        "attributes": {"friendly_name": "Garage Door", "area": "Garage"},
    },
    "climate.living_room": {
        "state": "heat",
        "attributes": {"friendly_name": "Living Room Thermostat",
                       "area": "Living Room", "current_temperature": 19.5,
                       "temperature": 21.0, "hvac_modes": ["off", "heat", "cool"]},
    },
    "media_player.living_room_tv": {
        "state": "off",
        "attributes": {"friendly_name": "Living Room TV", "area": "Living Room"},
    },
    "scene.goodnight": {
        "state": "scening",
        "attributes": {"friendly_name": "Goodnight", "area": ""},
    },
    "scene.movie_night": {
        "state": "scening",
        "attributes": {"friendly_name": "Movie Night", "area": "Living Room"},
    },
    "sensor.outdoor_temperature": {
        "state": "14.2",
        "attributes": {"friendly_name": "Outdoor Temperature", "area": "",
                       "unit_of_measurement": "°C"},
    },
    "binary_sensor.back_door": {
        "state": "off",
        "attributes": {"friendly_name": "Back Door", "area": "Kitchen",
                       "device_class": "door"},
    },
}

# service -> the state it leaves behind. Mirrors what a real hub does, so a
# read-back after a write tells the truth.
_RESULTING_STATE = {
    "turn_on": "on", "turn_off": "off", "start": "on", "return_to_base": "off",
    "lock": "locked", "unlock": "unlocked",
    "open_cover": "open", "close_cover": "closed",
    "media_play": "playing", "media_pause": "paused",
}

CALL_COUNT = 0


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class _Hub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # our own logging below is the useful one
        pass

    def _authed(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _send(self, code: int, body) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _row(self, entity_id: str) -> dict:
        row = STATES[entity_id]
        return {"entity_id": entity_id, "state": row["state"],
                "attributes": row["attributes"]}

    def do_GET(self):  # noqa: N802
        if not self._authed():
            print(f"[{_stamp()}] 401  {self.path}  (bad or missing token)")
            return self._send(401, {"message": "Unauthorized"})

        if self.path == "/api/config":
            print(f"[{_stamp()}] ping")
            return self._send(200, {"version": "2026.8.1 (fake hub)",
                                    "location_name": "Test House"})

        if self.path == "/api/states":
            print(f"[{_stamp()}] read  all states ({len(STATES)} devices)")
            return self._send(200, [self._row(e) for e in STATES])

        if self.path.startswith("/api/states/"):
            entity_id = self.path.rsplit("/", 1)[-1]
            if entity_id not in STATES:
                print(f"[{_stamp()}] read  {entity_id}  -> 404")
                return self._send(404, {"message": "Entity not found."})
            print(f"[{_stamp()}] read  {entity_id}  -> {STATES[entity_id]['state']}")
            return self._send(200, self._row(entity_id))

        self._send(404, {"message": "Not found"})

    def do_POST(self):  # noqa: N802
        global CALL_COUNT
        if not self._authed():
            print(f"[{_stamp()}] 401  POST {self.path}  (bad or missing token)")
            return self._send(401, {"message": "Unauthorized"})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"message": "Bad JSON"})

        if not self.path.startswith("/api/services/"):
            return self._send(404, {"message": "Not found"})

        service_path = self.path.replace("/api/services/", "")
        CALL_COUNT += 1
        print(f"\n[{_stamp()}] *** SERVICE CALL #{CALL_COUNT} ***")
        print(f"    {service_path}")
        print(f"    {json.dumps(data)}\n")

        # Apply it, so a read-back reflects what actually happened.
        changed = []
        entity_id = data.get("entity_id")
        entity_ids = [entity_id] if isinstance(entity_id, str) else (entity_id or [])
        domain, _, service = service_path.partition("/")
        for eid in entity_ids:
            if eid not in STATES:
                continue
            row = STATES[eid]
            if service == "toggle":
                row["state"] = "off" if row["state"] == "on" else "on"
            elif service in _RESULTING_STATE:
                row["state"] = _RESULTING_STATE[service]
            elif service == "set_temperature" and "temperature" in data:
                row["attributes"]["temperature"] = data["temperature"]
            elif service == "set_hvac_mode" and "hvac_mode" in data:
                row["state"] = data["hvac_mode"]
            for key in ("brightness_pct", "percentage", "position", "volume_level"):
                if key in data:
                    row["attributes"][key] = data[key]
            changed.append(self._row(eid))

        self._send(200, changed)


def main() -> int:
    server = HTTPServer((HOST, PORT), _Hub)
    print("=" * 66)
    print(f"  Fake Home Assistant hub  ->  http://{HOST}:{PORT}")
    print(f"  token: {TOKEN}")
    print(f"  {len(STATES)} devices across "
          f"{len({r['attributes'].get('area') for r in STATES.values() if r['attributes'].get('area')})} rooms")
    print()
    print("  Point Jarvis at it in Settings -> Home & devices, then watch this")
    print("  window: every SERVICE CALL below is something that reached the")
    print("  house. Nothing should appear here until you approve a card.")
    print("=" * 66)
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nstopped. {CALL_COUNT} service call(s) this session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

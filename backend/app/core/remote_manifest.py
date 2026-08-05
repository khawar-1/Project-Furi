"""
Jarvis OS — The remote surface's route manifest (2026-08-03)

*(Tier 2, item 5 — "one machine, one room")*

WHAT THIS IS FOR
----------------
`BACKEND_HOST` stays loopback and should: the main API deletes files and runs
shell commands. But that means no phone, and an approval card nobody is sitting
in front of is an agent stuck until someone walks back to the desk.

So: a SECOND listener, serving a narrow allowlist — read what is happening,
approve or decline what is waiting, answer a question. Never a shell.

⚠️ THE SAFETY IS THAT THE ROUTES DO NOT EXIST THERE, not that a check refuses
them. `create_remote_app` builds the full app and copies across ONLY the routes
named here; everything else is simply not mounted, and returns 404 because there
is nothing to call. That is a different KIND of guarantee from "a middleware
rejects it" — no bug in a middleware, no ordering mistake, and no future
`Depends` refactor can expose `/chat/stream` on that port, because it was never
put there.

That is the same reasoning the approval gate itself rests on
(`registry.execute_tool` refuses structurally rather than by prompt), and the
same reasoning that made READ-mode browsing safe.

⚠️ DEFAULT DENY, AND EVERY DENIAL IS WRITTEN DOWN. `test_every_route_is_allowed_or_denied`
walks the REAL app's routes: each must be in `REMOTE_ROUTES` or in `DENIED` with
a reason. A router added later fails that test until someone decides — which is
the only thing that stops this list rotting into a hole. The coverage-test
discipline that found `read_file.path` on its first run.

WHY "READ + APPROVE + ASK" AND NOT "READ + CHAT"
------------------------------------------------
`POST /chat/stream` is the widest-blast-radius endpoint in the product: it fans
into the reminder, interrupt, routine, continuation and task routers, and can
start an approval-gated background Task that runs shell commands, deletes files,
sends mail and drives a browser. Putting it on a LAN port would mean the remote
surface can start anything the desktop can — which is precisely "a shell",
however the words are arranged. It is denied.

The four ACTIONS that are allowed are all answers to something Jarvis already
asked, about work the user already started at the desk:

    POST /api/agent/approve        approve or cancel a plan already parked
    POST /api/agent/choose         answer a clarifying question already asked
    POST /api/tasks/{id}/pause     stop a run that is going wrong
    POST /api/tasks/{id}/cancel    ditto, terminally

None of them can start work. Approve still runs the plan through
`registry.execute_tool`, so the approval gate, the path guards and every
grounding lock apply exactly as they do on the desktop — and an off-card
approval still has to echo the contract hash (Tier 2 item 8).
"""
from __future__ import annotations

from typing import Iterable

# (method, path) pairs the remote listener serves. Paths are the app's own
# route paths, parameters included — matched literally against `APIRoute.path`.
REMOTE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    # --- liveness -----------------------------------------------------------
    ("GET", "/health"),

    # --- what is happening --------------------------------------------------
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/activity"),
    ("GET", "/api/activity/{session_id}"),
    ("GET", "/api/reminders"),
    ("GET", "/api/routines"),
    ("GET", "/api/threads"),
    ("GET", "/api/initiative/suggestions"),
    ("GET", "/chat/sessions/{session_id}/messages"),

    # --- the four answers ---------------------------------------------------
    ("POST", "/api/agent/approve"),
    ("POST", "/api/agent/choose"),
    ("POST", "/api/tasks/{task_id}/pause"),
    ("POST", "/api/tasks/{task_id}/cancel"),
})

# Everything else, with the reason. A route in neither map fails the coverage
# test — silence is not a decision.
#
# ⚠️ KEYED BY PATH, WHILE `REMOTE_ROUTES` IS KEYED BY (METHOD, PATH) — because
# a path can be allowed for one method and must be denied for another, and the
# coverage check found exactly that on its first run: `GET /api/threads` reads
# the user's open concerns and belongs on a phone, while `POST /api/threads`
# creates one and does not. Such a path appears in BOTH maps; the reason here
# describes the methods that are NOT allowed.
#
# Grouped by WHY, because the reasons are different and the differences matter.
DENIED: dict[str, str] = {
    # ---- can start work (this is the "never a shell" line) -----------------
    "/chat/stream": (
        "fans into five routers and can start an approval-gated background Task "
        "that runs shell commands, deletes files, sends mail and drives a "
        "browser — this IS the shell, whatever it is called"
    ),
    "/chat": "persists messages and runs the chat LLM; the remote surface reads, it does not converse",
    "/api/agent/execute": "plans and runs a new goal — starting work is exactly what remote must not do",
    "/api/routines/{routine_id}/run": "starts a background Task from a stored goal",
    "/api/initiative/run-now": "runs a full initiative pass, which may auto-start a task at the 'act' tier",
    "/api/initiative/suggestions/{suggestion_id}/accept": "starts an approval-gated Task",
    "/api/settings/briefing/run-now": "composes and delivers a briefing (LLM + Google reads)",
    "/api/index/rebuild": "starts a filesystem indexing pass",
    "/api/browser/login": "launches a real Chromium window at a caller-supplied URL in the signed-in profile",
    "/api/browser/close-login": "drives the browser stack",
    "/api/browser/stop-media": "drives the browser stack",
    "/api/browser/close-window": "drives the browser stack",
    "/api/integrations/google/connect": "opens an OAuth consent flow in the system browser",
    "/api/integrations/google/disconnect": "revokes the user's Google account",
    "/ws/test": "dev utility that broadcasts an arbitrary payload to every client",

    # ---- changes settings ---------------------------------------------------
    "/api/settings/briefing": "changes when Jarvis speaks first",
    "/api/settings/voice": "changes voice settings, INCLUDING who may approve by voice",
    "/api/context/settings": "the sensing kill switch — changing it remotely is the wrong direction",
    "/api/index/config": "changes which folders are indexed",
    "/api/initiative/settings": "changes the autonomy ceiling",
    "/api/browser/vision": "changes the browser vision posture",
    "/api/routines/{routine_id}/schedule": "arms a scheduled autonomous run",

    # ---- writes the user's own data ----------------------------------------
    "/memory": "creates memories",
    "/memory/{memory_id}": "deletes a memory (hard delete)",
    "/memory/{memory_id}/restore": "restores an archived memory",
    "/memory/conflicts/{conflict_id}/resolve": "hard-deletes a fact the user judged superseded",
    "/memory/conflicts/{conflict_id}/dismiss": "settles a conflict review",
    "/api/contacts": "creates contacts",
    "/api/contacts/{contact_id}": "edits or deletes a contact",
    "/api/contacts/{contact_id}/interactions": "writes a contact fact",
    "/api/contacts/{contact_id}/interactions/{interaction_id}": "deletes a contact fact",
    "/api/reminders": "creates a reminder (and arms a scheduler job)",
    "/api/reminders/{reminder_id}": "cancels a reminder",
    "/api/routines": "creates or re-teaches a routine",
    "/api/routines/{routine_id}": "deletes a routine",
    # GET is ALLOWED above; this reason covers POST (creates a thread).
    "/api/threads": "POST creates a goal thread — the phone reads threads, it does not write them",
    "/api/threads/{thread_id}/resolve": "changes thread state",
    "/api/threads/{thread_id}/dismiss": "changes thread state",
    "/api/preferences/{pref_id}": "deletes a preference",
    "/api/schedule/{job_id}": "cancels a scheduled job",
    "/api/schedule/test": "schedules an arbitrary push",
    "/api/initiative/suggestions/{suggestion_id}/dismiss": "writes suggestion state and tunes an affinity",
    "/api/autofill": "reads or writes the autofill profile — form-fill data, some of it secret",
    "/api/autofill/{key}": "deletes an autofill field",
    "/api/context/device": "feeds the sensing pipeline",
    "/api/context/state": "feeds the sensing pipeline",
    "/api/context/screen": "uploads a screen frame for OCR",

    # ---- home & IoT: configured at the machine, like pairing ---------------
    #
    # ⚠️ /settings takes the hub ADDRESS and the ACCESS TOKEN. A paired phone
    # that could rewrite the address could point Jarvis's home tools at a hub
    # someone else controls — the same class of hazard as a paired device that
    # can pair another, which is why /api/remote is absent from this manifest
    # too. Connecting a home is a decision made at the desk.
    #
    # The device LIST and the connection probe are reads, and they are denied on
    # the "discloses more than a phone needs" ground below: a list of every room
    # and every lock in the house, plus whether each is currently open, is the
    # single most sensitive read on this surface.
    #
    # None of this stops the phone being USEFUL here: a home plan started at the
    # desk pauses for approval, and approving it is /api/agent/approve, which IS
    # on the manifest. The phone answers the question; it does not rewire the
    # house.
    "/api/home/settings": "sets the hub address and stores its access token",
    "/api/home/test-connection": "probes the hub",
    "/api/home/devices": "every room, lock and door in the house, and whether each is open",

    # Desktop control (Feature 2), denied for the same two reasons. /settings
    # can widen what Jarvis may do to the machine — a phone that could flip
    # allow_clipboard on is a phone that can read whatever was last copied,
    # which on a work machine is routinely a password. /apps enumerates every
    # program the user has installed, which is a fingerprint of them and of
    # no use away from the desk. A desktop plan STARTED at the desk still
    # pauses for approval, and approving it is /api/agent/approve, which IS
    # on the manifest.
    "/api/desktop/settings": "widens what Jarvis may do to the machine, including clipboard access",
    "/api/desktop/apps": "every application installed on the user's machine",

    # ---- discloses more than a phone needs ---------------------------------
    #
    # These are READS, and denying them is a judgement rather than a rule: the
    # remote surface is small and public-ish (a LAN port, a token on a phone),
    # so it carries what is needed to answer a pending question and no more.
    "/api/context/world": "the world model includes OCR'd text from the user's screen",
    "/api/context/status": "sensing state; the phone has no use for it",
    "/api/browser/media": "the titles and URLs of every open agent tab",
    "/api/browser/account": "browser sign-in state",
    "/api/agent/tools": "the full tool inventory — a map of everything Jarvis can do",
    "/api/activity/routing": "per-turn routing forensics, including a copy of what was typed",
    "/api/activity/plans": "per-plan failure forensics",
    "/memory/search": "semantic search across everything Jarvis knows about the user",
    "/memory/stats": "memory counts",
    "/memory/archived": "archived memories",
    "/memory/conflicts": "pairs of the user's own facts that may disagree",
    "/api/episodes": "episodes",
    "/api/episodes/search": "episode search",
    "/api/preferences": "inferred behavioural preferences",
    "/api/contacts/resolve/{name}": "contact identity resolution",
    "/api/schedule": "the scheduler's job table",
    "/api/index": "file-index configuration",
    "/api/index/status": "file-index progress",
    "/api/index/frequent-folders": "learned folder habits",
    "/api/integrations/google/status": "Google account state, including the address",
    "/api/settings/voice/clone": "voice cloning",
    "/api/voice/transcribe": "spends GPU on audio from an unattended surface",
    "/api/voice/speak": "makes the DESKTOP machine talk out loud, remotely",
    "/api/voice/speak/stream": "same",
    "/api/voice/status": "voice model state",

    # ---- pairing itself ----------------------------------------------------
    #
    # ⚠️ THE COVERAGE TEST CAUGHT THESE THE MOMENT THEY WERE REGISTERED, which
    # is the whole reason it exists: the pairing API is the most tempting thing
    # to leave allowed ("it would be handy to add a device from my phone") and
    # the most dangerous — a paired device that can pair another, or revoke its
    # own revocation, is a credential that cannot be taken away. Pairing happens
    # at the machine.
    "/api/remote": "lists paired devices; pairing is done at the machine",
    "/api/remote/pair": "a paired device must never be able to pair another",
    "/api/remote/{device_id}": "a paired device must never be able to undo its own revocation",

    # ---- infrastructure ----------------------------------------------------
    "/ws": (
        "the push channel is server→client and needs its own session/auth story "
        "before a remote client subscribes to every event on the machine"
    ),
    "/docs": "API explorer",
    "/docs/oauth2-redirect": "part of the API explorer (FastAPI adds it automatically)",
    "/redoc": "API explorer",
    "/openapi.json": "the full API schema — a map of the surface",
}


def is_remote_route(methods: Iterable[str], path: str) -> bool:
    """Is this route served by the remote listener? Default DENY."""
    return any((m.upper(), path) in REMOTE_ROUTES for m in methods)

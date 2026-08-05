# Jarvis OS — Proposed Feature Roadmap

> **Status:** written 2026-08-04 as a proposal. **Features 1 and 2 are now
> BUILT** (see their ✅ headers); 3–8 and the cross-cutting prerequisites are
> still proposals.
> Companion to `CLAUDE.md` (which records what *is* built and why) and
> `suhhestionsfromclaude.txt` (the prior roadmap, whose Tier 1 items 3–4 and all
> of Tier 2 shipped in commits `436a550`, `4290850`, `5c1c9e8`).
>
> Every claim of absence below was verified against the tree, not assumed —
> **at the time of writing.** The "Context" paragraph's tool and router counts
> are that snapshot (33 tools); Features 1 and 2 have since taken it to 47.
> A spec is a record of what was decided, so the body text below is left as
> written rather than back-edited; where a shipped feature departed from it,
> the ✅ header says so and `CLAUDE.md` carries the reasoning.

## Context

Jarvis OS's **cognitive** layer is complete to an unusually high standard:
memory with identity resolution, decay, consolidation and a conflict queue; a
LangGraph planner behind a structural approval gate with grounding locks;
background domain agents with pause/steer/cancel; an initiative engine; a
context layer; GPU voice with a wake word; and a real multi-tab browser that
fills and submits forms behind per-gesture approval. 33 tools, 23 routers,
121 test files, three scored benches.

What is missing is **embodiment** — reach into the physical world, control of
the machine Jarvis already watches, and input channels beyond keyboard and
microphone. Every feature below closes part of that gap.

## Conventions every feature here follows

These are the house rules the codebase already enforces. Each feature spec
below assumes them rather than restating them.

| Rule | Where it lives |
|---|---|
| A tool declares `PermissionLevel` READ / WRITE / DESTRUCTIVE; anything non-READ is refused by `registry.execute_tool` without `approved=True`, and every attempt (executed *and* blocked) writes an `ActivityLog` row | `app/core/base_tool.py`, `app/tools/registry.py` |
| A new tool domain is one module in `app/tools/` plus one import line in `app/tools/__init__.py` — no other code change | `app/tools/__init__.py` |
| External services go behind an **injectable factory** so tests never touch the network | `app/integrations/google_services.py` (`GMAIL_SERVICE_FACTORY`) |
| Runtime settings are a typed dataclass + coercer in `app_settings`, defaulting **OFF**; `.env` is only for secrets and process-level config | `app/core/app_settings.py` |
| Recurring work is a scheduler job kind that self-registers at import, with an `ensure_*_job()` reconcile in the lifespan | `app/core/birthdays.py`, `reindex.py` |
| Deterministic chat intents get their own router in the `chat.py` chain — no LLM call | `reminder_router.py`, `routine_router.py`, `interrupt_router.py` |
| Tool output is rendered by a code-authored formatter, never raw JSON | `app/agents/rendering.py::_RESULT_FORMATTERS` |
| Anything that can act on the user's behalf is bounded by **grounding**: it must trace to the user's own words, never to page/email/model content | `app/browser/grounding.py` |
| A guard is a comparator computed independently of the model's output. A prompt rule with nothing checking it has been measured at ZERO three times in this project | CLAUDE.md, passim |

---

# 1. Home & IoT control  ✅ BUILT (2026-08-04)

> **Shipped.** 5 tools, the entity-id lock, `/api/home`, a Settings card, 90
> tests, 12/12 falsifications proven, 18/18 runtime checks against a real
> backend lifespan. See the "Home & IoT control" round note in `CLAUDE.md` for
> what was actually built and the three places the design departed from this
> spec (no SSRF exemption — the tools expose no URL at all; the remote surface
> denies all three routes; `spoken.py` needed forms the spec did not mention).

**The single most iconic Jarvis capability, and currently absent.** Verified:
`grep -ril "homeassistant|smart.home|hue|mqtt|tuya"` across `backend/app` and
`frontend/src` returns only two unrelated `.wasm` binaries.
`app/integrations/` contains Google and nothing else.

### Why Home Assistant rather than per-vendor SDKs

One self-hosted HTTP API covers ~2000 device brands (Hue, LIFX, Tuya, Z-Wave,
Zigbee, Nest, Sonos, TP-Link…). A long-lived access token, a REST call, done.
Integrating vendors individually would mean N OAuth flows, N token stores and N
failure modes for the same capability. HA also exposes *scenes* and
*automations*, which map cleanly onto the routines you already have.

If HA is not installed, the integration reports not-configured and every tool
degrades cleanly — the `GoogleNotConnectedError` contract.

### Architecture

```
app/integrations/home_assistant.py   # the ONLY module that holds the HA token
app/tools/home_tools.py              # 5 tools, self-registering
```

`home_assistant.py` mirrors `google_services.py`:
- `HOME_SERVICE_FACTORY` module-level injectable callable — tests swap a fake,
  the suite never touches a real hub.
- `HomeNotConnectedError` with a stable user-facing message, raised from one
  choke point, caught by every tool → clean failed `ToolResult`.
- Token in `~/.jarvis/home_token.json` (atomic write, 0600) *or*
  `HOME_ASSISTANT_TOKEN` in `.env` — never logged. Base URL is a LAN address,
  so it must be exempted from the SSRF guard **explicitly and only for this
  client**, never by relaxing `browser_tools._host_is_blocked`.
- An entity-registry cache (60s TTL) so `list_devices` is cheap.

### Tools

| Tool | Level | Notes |
|---|---|---|
| `list_devices` | READ | Optional `area` / `domain` filter. Returns `{entity_id, name, area, domain, state}`. |
| `get_device_state` | READ | One entity, with attributes (brightness, temperature, …). |
| `set_device_state` | **WRITE** | `entity_id` + `state` + optional attributes. Pauses for approval. |
| `run_scene` | **WRITE** | Activate a named scene ("movie night"). |
| `set_climate` | **WRITE** | Thermostat target; separated from `set_device_state` so the approval card can render °C and mode explicitly. |

**Deliberately NOT built:** a `run_automation` / `call_service` passthrough.
An arbitrary-service escape hatch would let a plan reach anything on the hub,
including HA's own shell-command and notify integrations — the same reasoning
that keeps `run_command` DESTRUCTIVE and blocklisted.

### The entity-id lock (mirrors the calendar event-id lock)

A concrete `entity_id` on a WRITE step must trace to a `list_devices` /
`get_device_state` result **in this plan** — enforced by
`_entity_id_violation` in the `_generate_steps` reject chain, exactly like
`_event_id_violation`. At draft time nothing is completed, so any concrete id
is rejected with retry feedback pushing the model to read first and use a
`PENDING:` placeholder. `placeholder_resolver` then fills it in code when the
completed reads pin exactly one device; several candidates → code never picks,
the plan asks.

Without this, "turn off the light" resolves to a hallucinated `light.bedroom`
that might be the garage door.

### Approval card

`_step_action_detail` renders the full contract in human terms:
`Kitchen Lights (light.kitchen_main) → on, brightness 80%`, never the raw
entity id alone. The user approves a *room and a device*, not a slug.

### Config, API, UI

- `home.config` in `app_settings`: `{enabled: False, base_url, area_aliases}`.
  Token lives outside the DB.
- `GET/PUT /api/home/settings`, `GET /api/home/devices`,
  `POST /api/home/test-connection`.
- Settings panel `HomeCard`: connection status, device count by area, a
  read-only device list. Immediate-PUT toggles (the `FileIndexCard` lesson).

### What it unlocks for free

Because these are ordinary registered tools, they compose immediately with
everything already built: **routines** ("run my goodnight routine"),
**scheduled routines** ("every day at 11pm"), **reminders**, the
**initiative engine** ("you're leaving in 10 minutes and the heating is on —
turn it down?"), and **voice**. This is why it is the highest-value feature
per line of code on this list.

### Tests

`test_home_tools.py` — a chained-call `FakeHomeAssistant` recording every
request (the `FakeGmail` pattern): entity-id lock at all three layers,
not-connected degradation, the unapproved-write structural block, the
approval-card contract, `placeholder_resolver` fill with one vs several
candidates, formatter output.

### Risks

- **LAN SSRF exemption** is the one genuinely delicate part. Scope it to the
  configured HA base URL only, resolved once at config-set time, and never a
  general "private ranges are fine" relaxation.
- HA's `state` vocabulary differs per domain; normalize in code, not prompts.

---

# 2. Desktop control — close the sense/act asymmetry  ✅ BUILT (2026-08-04)

> **Shipped.** 9 tools, the window-handle lock, `/api/desktop`, a Settings card,
> 82 tests, 16/16 falsifications proven, 21/21 runtime checks against a real
> backend lifespan. See the "Desktop control" round note in `CLAUDE.md` for what
> was actually built and the three places the design departed from this spec:
> **ctypes instead of a PowerShell helper** (the host is Python, so there is no
> subprocess and no shell to inject into); **the sub-toggles split five ways**
> rather than four (grouping "turn the volume down" with "read what I just
> copied" forces an unwanted grant); and the routing tier ordered ahead of
> `browse_intent` on measurement. Also recorded there: two Win32 bugs the live
> probe found that no amount of reading would have caught, and a resolver defect
> that made this feature's own plan rule unresolvable.

Phase 8 senses the active application and window title and feeds it into the
World Model. **There is no tool to act on any of it.** Verified: no
`launch_app`, `focus_window`, `set_volume`, `screenshot`, or clipboard access
anywhere in `app/tools/`.

`run_command` exists, but it is DESTRUCTIVE-level, so "open Spotify" pauses for
approval every single time — the right default for a shell, the wrong ergonomics
for the most common thing a person asks an assistant to do.

### Why this is the highest-frequency feature on the list

"Pull that up", "put it on the main screen", "mute that" is film-Jarvis's
most-used register. You have already built the hard half. This is the easy half.

### Architecture

```
app/core/desktop.py         # OS abstraction; per-platform impl behind one seam
app/tools/desktop_tools.py  # the tools
```

`desktop.py` exposes a `DesktopController` protocol with a
`DESKTOP_CONTROLLER_FACTORY` injectable seam (`STT_MODEL_FACTORY` pattern;
conftest autouse fixture installs a fake so the suite never touches the real
OS — non-negotiable, or a test run starts closing the developer's windows).

Windows implementation reuses the **`electron/sensing.ts` precedent**: a
long-lived PowerShell helper using `Add-Type` P/Invoke against Win32
(`ShellExecute`, `SetForegroundWindow`, `EnumWindows`, `keybd_event` for media
keys). **Zero npm native dependency, no electron-builder rebuild** — the exact
reasoning that chose PowerShell over a native module for foreground-window
sensing. macOS/Linux stubs raise a clean "not supported on this platform".

### Tools

| Tool | Level | Rationale for the level |
|---|---|---|
| `list_windows` | READ | Titles + process names of open windows. |
| `focus_window` | **WRITE** | Changes what the user sees. Reversible, so WRITE not DESTRUCTIVE. |
| `launch_app` | **WRITE** | Constrained to an allowlist (below) — this is what keeps it out of DESTRUCTIVE. |
| `close_window` | **WRITE** | Can lose unsaved work → the approval card must name the exact window title. |
| `set_volume` | **WRITE** | Includes mute/unmute. |
| `media_key` | **WRITE** | play-pause / next / previous. |
| `take_screenshot` | READ | Returns a path under `~/.jarvis/screenshots`, never the bytes into a prompt. |
| `read_clipboard` | READ | |
| `write_clipboard` | **WRITE** | |

### The app allowlist is what makes `launch_app` safe

`launch_app` must **not** be a thin `ShellExecute` over an LLM-supplied string —
that is `run_command` with the approval gate weakened, which is strictly worse
than `run_command`. Instead:

- `desktop.discover_apps()` enumerates installed applications once (Start Menu
  shortcuts on Windows, `/Applications` on macOS) into a cached registry.
- `launch_app` takes a **name that must resolve against that registry**. An
  unresolved name is a clean failure naming the closest matches — never a
  path, never a command line, never arguments.
- Fuzzy resolution reuses `rapidfuzz` and the memory engine's
  `MIN_SCORE = 81` / `MIN_GAP = 8` convention: an ambiguous match asks (plan
  rule 11) rather than guessing between "Code" and "Code - Insiders".

This means the tool's entire reachable surface is *applications the user has
installed*, which is a bounded, inspectable set — the property `run_command`
cannot have.

### Screenshots must not become a data-exfiltration channel

`take_screenshot` returns a **path**, and the image is written under
`~/.jarvis/screenshots` with a retention sweep in `housekeeping.py`. It never
returns bytes into a tool result, because a tool result flows into planner and
summary prompts. Reading a screenshot is Feature 6's job, through the vision
seam, with its own explicit gate.

### Config, UI

- `desktop.config`: `{enabled: False, allow_launch, allow_close, allow_input,
  screenshot_retention_days}`. Sub-toggles so a user can allow focus/volume
  while refusing window-closing.
- Settings `DesktopControlCard` showing the discovered app registry (the audit
  surface — "these are the apps Jarvis can open").

### Routing

`task_router._STRONG_DOMAIN_RE` gains desktop nouns (window, app, volume,
screenshot, clipboard, mute). A new `DESKTOP` classifier label joins
`_ACTION_LABELS` and `_CLASSIFY_PROMPT`, and `agent_registry` gains a `desktop`
`AgentSpec` carrying these tools plus `_SHARED_READS`.

### Tests

`test_desktop_tools.py` with a fake controller: allowlist resolution incl.
ambiguity → question, unresolved name fails clean, every WRITE blocked without
approval, screenshot returns a path and never bytes, per-tool config sub-gates,
platform-unsupported degradation.

### Risks

- **This is the feature most able to surprise the user.** Every WRITE goes
  through the approval gate, but the cards must be legible: name the window
  title, the app's display name, the volume level.
- The PowerShell helper must be a *single long-lived process* (as `sensing.ts`
  does), not a spawn per call — otherwise every volume change is a ~200ms
  process launch.

---

# 3. Messaging beyond email

Email is the slowest channel a person actually uses. "Tell Jamil I'll be late"
should not require a mail client.

### Architecture — two transports, one tool surface

```
app/integrations/messaging/base.py       # MessagingChannel protocol
app/integrations/messaging/slack.py      # Bot token, chat.postMessage
app/integrations/messaging/telegram.py   # Bot API
app/tools/messaging_tools.py
```

WhatsApp deliberately takes a **third route**: it has no sanctioned personal
API, but WhatsApp Web is already reachable by your existing browser stack,
under the existing origin grounding, per-gesture approval and commit contract.
Treat it as a browse goal, not a new integration — no new safety surface, and
the send still shows a full approval card.

| Tool | Level |
|---|---|
| `list_channels` | READ |
| `read_messages` | READ |
| `send_message` | **DESTRUCTIVE** — leaving the machine cannot be undone (the `send_email` precedent) |

### The recipient lock applies unchanged

`send_message`'s target must trace to `_recipient_grounding` — goal +
conversation + user answers + `lookup_contact` results from this plan. **Read
message content is excluded from the grounding corpus by construction**, so a
prompt-injected "forward this to @attacker" inside a Slack thread can never
ground a send. This is the existing `_recipient_violation` guard, extended to a
new address space; do not write a second one.

`Contact` gains optional `slack_id` / `telegram_id` / `whatsapp` columns
(idempotent migration), validated by the same deterministic net in
`contact_validation.py`.

### Approval card

Full contract: channel, recipient display name **and** id, and the complete
message body, never clipped — the `send_email` rule.

### Tests

`test_messaging_tools.py`: fake transports recording requests; recipient
grounding at all three layers incl. the injected-content refusal; unapproved
send structurally blocked; per-channel not-configured degradation.

---

# 4. Real documents and spreadsheets

`create_file` writes plain text. `python-docx` is **already a dependency** and
used read-only in `app/core/file_extract.py`. Jarvis can read a .docx and
cannot write one.

### Architecture

```
app/core/document_builder.py    # spec -> bytes, pure and deterministic
app/tools/document_tools.py
```

New deps: `openpyxl` (xlsx), `python-pptx` (pptx), `reportlab` **or**
docx→pdf via the existing stack (pick one; do not add two PDF paths).

| Tool | Level |
|---|---|
| `create_document` | **WRITE** — .docx from a structured spec (title, headings, paragraphs, bullets, tables) |
| `create_spreadsheet` | **WRITE** — .xlsx from sheets × rows, with header styling and formulas as literal strings |
| `create_presentation` | **WRITE** — .pptx from a slide spec |
| `append_to_document` | **WRITE** — the missing verb; `create_file` never overwrites, so there is currently no way to add to an existing file at all |

### The content is authored at planning time, not by the tool

The planner writes the document spec as literal step parameters (the
`send_email` subject/body rule), so:
- **no LLM call ever happens inside a tool** — that invariant stays auditable
  from imports;
- the approval card shows the actual content that will be written.

`_step_action_detail` renders the target path plus a structural summary
(*"3 sections, 1 table, 12 rows"*) and the first ~500 characters of body text —
full contract for the path, a legible précis for the content.

Path safety reuses `file_tools._resolve_path` / `_blocked_reason` unchanged,
and `file_intelligence` learns the destination folder exactly as it does for
`create_file`.

### Tests

`test_document_tools.py`: round-trip each format through `file_extract` (write
then read back — the strongest available assertion), path guards, the
unapproved-write block, `append_to_document` never truncating.

---

# 5. Media & music control

Today, playing music means a full browse run: launch Chromium, observe, decide,
act — tens of seconds for something that should be instant.

### Architecture

```
app/integrations/spotify.py     # OAuth (the google_auth loopback pattern)
app/tools/media_tools.py
```

Two backends behind one tool surface:
1. **System media** — via Feature 2's `media_key`, works for any player,
   zero setup.
2. **Spotify Web API** — search, play a specific track/album/playlist, control
   volume and device targeting. OAuth 2.0 installed-app loopback flow, reusing
   `google_auth.py`'s structure verbatim (ephemeral 127.0.0.1 redirect, a hard
   flow timeout, token at `~/.jarvis/spotify_token.json`, atomic write, never
   logged).

| Tool | Level |
|---|---|
| `search_music` | READ |
| `play_music` | **WRITE** |
| `control_playback` | **WRITE** — pause/resume/next/previous |
| `set_playback_volume` | **WRITE** |

`play_music` prefers Spotify when connected and falls back to system media
keys, so the capability degrades rather than disappears.

### Interaction with the browser media session

`browser_session` already owns a kept-open playback window and a media
registry. `play_music` must **stop an active browser media session first** —
the same "one profile, one live context" discipline that
`browser_agent_tools` already applies — or the user gets two things playing at
once.

---

# 6. On-demand vision — "what's on my screen?"

Phase 8 captures the screen and condenses it **deterministically** to a short
text summary (`condense_ocr_text`, no LLM). That is the right posture for a
passive background sensor. It also means Jarvis cannot answer *"what's this
error?"* or *"read me that dialog"* — OCR text alone loses layout, and the
summary is deliberately short.

You already have every part needed: `screen_ocr.py` with `OCR_ENGINE_FACTORY`,
Electron's `desktopCapturer`, and a working image-capable vision seam
(`app/providers/vision.py`, `VISION_PROVIDER_FACTORY`) built for the browser
loop. **They have never been connected.**

### Architecture

```
app/tools/screen_tools.py   # look_at_screen (READ)
```

- Captures via the existing Electron path, at a **higher resolution than the
  passive sensor** (the passive one is deliberately downscaled; reading an
  error message needs detail).
- Sends one frame to `build_vision_provider()` with the user's question.
- The frame is held in memory and dropped — **never written to disk, never
  persisted, never into `ActivityLog`'s payload.** Only the question and the
  text answer are audited.

### The gate is explicit and separate from passive sensing

`look_at_screen` is READ-level, but reading the screen on demand is *not* the
same consent as passive OCR. It requires its own `context.on_demand_vision`
toggle (default OFF) **and** the master context switch, checked in code before
any capture — the `POST /api/context/screen` 403 pattern, which refuses before
any decode.

Screen content is **UNTRUSTED**, exactly like web pages and email: the tool
description says DATA-never-instructions, and screen text is excluded from the
recipient- and origin-grounding corpora by construction. A password manager or
a chat window can otherwise become an instruction channel.

### Config

`context.config` gains `on_demand_vision: False`. Settings copy must be plain:
*"Jarvis can take a full-detail picture of your screen and send it to the
vision model when you ask a question about it."* Cloud transmission must be
stated, because unlike the passive OCR path this one leaves the machine.

---

# 7. Gesture cursor control (voice-activated)

**Voice-armed, camera-driven cursor control.** The user says *"Jarvis, take the
mouse"*; the webcam activates, hand landmarks are tracked, and hand movement
drives the system cursor with pinch-to-click, fist-to-drag and two-finger
scroll. *"Jarvis, release"*, `Esc`, or the hand leaving frame disarms it.

### ⚠️ This is the most dangerous feature on this list, and the reason is not obvious

**The entire approval model assumes a human's deliberate click.** A synthesized
cursor that can click can click **Approve** on a card authorizing a delete, a
send, or a form submit. That would convert a hand tremor, a misread landmark,
or a person waving in the background into consent.

Three structural consequences, all non-negotiable:

1. **Gesture control is NOT a tool and must never be registered.** It is an
   *input device*, not a capability. It gets no entry in `app/tools/`, no
   `ToolDefinition`, and no reachability from the planner, a routine, a
   scheduled routine, or the initiative engine. Nothing Jarvis can decide to do
   may turn on the user's camera or move their cursor. It is armed by a human,
   for a human, only.

2. **A synthesized click can never land on Jarvis's own approval UI.** Gesture
   mode **auto-suspends** whenever any plan is `awaiting_approval`,
   `awaiting_choice`, or `paused` — the same `_OPEN_STATUSES` predicate
   `plan_store` already owns, so there is no second hand-kept copy of the list.
   The card shows *"Gesture control paused — approve with keyboard, mouse or
   voice."* Approval by voice already exists (`app/agents/spoken.py`) and is
   the right hands-free path, because it is bound to a re-derived contract
   hash. A gesture is bound to nothing.

3. **Frames never leave the machine.** No HTTP POST, no backend, no vision
   provider. Inference runs in the renderer. This is a stronger posture than
   Phase 8's screen OCR, which does POST to localhost.

### Where each half runs

**Perception → Electron renderer, in a Web Worker.** The precedent is exact:
`wakeWord.ts` + `wakeWorker.ts` already run ONNX inference in a renderer Worker
for the wake word, with a documented pin (`onnxruntime-web` 1.17.3,
`numThreads=1`, `proxy=false`, explicit `wasmPaths`) that exists because
ORT ≥1.19's shared-memory-only WASM crashed the renderer with `0xC0000005`.
**Reuse that pinned setup exactly; do not introduce a second ORT version.**

Camera access needs **no new permission surface**: `electron/main.ts` already
grants `media` — which covers camera as well as microphone — to our own origin
only, via `setPermissionRequestHandler` plus its synchronous `CheckHandler`
twin.

Model: MediaPipe Hands (or an ONNX hand-landmark model) — 21 landmarks per
hand, ~30fps on CPU, no GPU requirement.

**Actuation → Electron main process.** The renderer has no OS input access.
Reuse Feature 2's `DesktopController`: the same long-lived PowerShell/Win32
helper, extended with `SetCursorPos` and `mouse_event`. One helper process, one
Win32 surface, one thing to test.

> **⚠️ Open engineering question, to be settled by measurement, not argument.**
> A PowerShell IPC round-trip per cursor update at 30–60 Hz may not hold. If
> measured latency exceeds ~20ms, switch actuation to a native module
> (`@nut-tree/nut-js`) and accept the electron-builder rebuild cost. **Measure
> before choosing** — this codebase's own record shows reasoned-not-measured
> performance decisions being wrong in both directions (the 2026-08-01
> `browse_speed` round; the 2026-07-27 CDP cache round, where the obvious fix
> succeeded and changed nothing).

### Gesture vocabulary

| Gesture | Action |
|---|---|
| Open palm, moving | Move cursor |
| Pinch (thumb + index) | Left click; hold = press-and-hold |
| Double pinch | Double click |
| Pinch with middle finger | Right click |
| Closed fist, moving | Drag |
| Two fingers, vertical | Scroll |
| Palm held still ≥1.5s | **Disarm** (the gesture-native exit) |

### Making it usable rather than a demo

- **Smoothing:** a One Euro filter on the landmark stream. Raw landmarks jitter
  by several pixels at rest; naive averaging trades that for visible lag. This
  is the single biggest determinant of whether the feature feels good.
- **Clutching:** map a *control rectangle* in camera space to the screen, with
  a trackpad-style re-centre when the hand leaves and re-enters — otherwise the
  user runs out of physical space before reaching the screen edge.
- **Dwell-click as an accessibility alternative:** hold still over a target for
  N ms. Configurable, off by default.
- **Multi-monitor:** map to the display containing the focused window; expose
  the choice in settings.

### Safety rails beyond the three structural rules

- **Deadman timer** — no hand detected for `DISARM_AFTER_SECONDS` (default 5) →
  disarm, camera released.
- **Hard session cap** — auto-disarm after N minutes regardless of activity.
- **Visible indicator, always** — StatusBar shows *"Gesture control: ON"* in
  amber beside the existing sensing indicator, plus a camera-active dot. An
  input channel the user cannot see is not acceptable.
- **`Esc` disarms unconditionally**, ahead of every other key handler.
- **Never auto-arms.** Not at startup, not on wake word, not on a push event.

### Arming: a deterministic router, so typed and spoken are equivalent

Voice reaches the backend as ordinary text
(mic → `/api/voice/transcribe` → `sendMessage` → `chat_stream`), so the trigger
belongs in a new `app/api/gesture_router.py`, inserted in the `chat.py` router
chain beside `reminder_router` / `routine_router` / `interrupt_router`.

- Deterministic phrase match, **no LLM call**, short-circuits the turn.
- Defers to any open memory question or plan question — the fail-open rule every
  sibling router follows.
- Responds by pushing a `gesture_control` event (`push.py`); the frontend arms
  or disarms. It is in `notifications.ts` `SILENT_TYPES` — the StatusBar
  indicator is the signal, a toast would be noise.
- Refuses to arm, with a spoken reason, when the config is disabled, no camera
  is present, or a plan is currently awaiting approval.

Trigger phrases are literal and few (*"take the mouse"*, *"gesture control on"*,
*"control the cursor"*, *"release the mouse"*, *"stop gesture control"*). This is
a **fixed command vocabulary**, not intent classification — the distinction that
matters, since keyword lists standing in for judgement have measured at zero
three times in this project. Here there is no judgement to make.

### Config

`gesture.config` in `app_settings` (default **OFF**, opt-in like every sensing
feature):

```
enabled: False
camera_device_id: ""          # "" = system default
sensitivity: 1.0              # control-rectangle scale
smoothing: 0.5                # One Euro cutoff
dwell_click_ms: 0             # 0 = off
disarm_after_seconds: 5
max_session_minutes: 15
target_display: "focused"     # focused | primary | <id>
```

### Files

```
frontend/src/lib/gestureInput.ts       # camera + worker lifecycle + smoothing
frontend/src/lib/gestureWorker.ts      # ONNX/MediaPipe inference (ORT 1.17.3, pinned)
frontend/src/stores/gestureStore.ts    # armed | tracking | suspended state machine
electron/gestureCursor.ts              # main-process actuation via DesktopController
backend/app/api/gesture_router.py      # deterministic arm/disarm trigger
backend/app/core/app_settings.py       # GestureConfig + coercer  (⚠ see below)
```

> ⚠️ When adding `GestureConfig`, note that `set_briefing_config`,
> `set_file_index_config`, `set_context_config`, `set_initiative_config` and
> `set_browser_vision_config` all still **hand-list their fields**, and that is
> exactly the drift that silently dropped `spoken_approval` on 2026-08-03. Use
> `asdict()`, as `set_voice_config` now does, and add a round-trip test
> asserting the *whole* config survives a write.

### Tests

- `test_gesture_router.py` — the trigger matrix; deference to open questions;
  **refusal to arm while a plan awaits approval**; zero LLM calls.
- `test_gesture_registry.py` — asserts no gesture capability is in the tool
  registry and no `AgentSpec` references one. This is the structural guarantee
  that the planner can never turn on the camera, and it should fail loudly the
  day someone "helpfully" adds a tool.
- Frontend: gesture classification from recorded landmark fixtures; the
  smoothing filter; deadman and session-cap timers; **auto-suspend when
  `plan_store` reports an open status**.

### Honest limits, to state in the module docstring

- Lighting- and background-dependent; a second person in frame degrades tracking.
- Precision is worse than a mouse. Small targets will be frustrating; this is
  a *supplementary* input for presentations, accessibility and across-the-room
  use, not a mouse replacement.
- Continuous webcam processing costs meaningful CPU — which is why the session
  cap and deadman exist.

---

# 8. Multi-agent parallelism

Today one task is one agent running one plan. `agent_registry` already
classifies a task's domain and dispatches to a specialist, but *"summarize my
inbox while you find those files"* runs sequentially.

### Architecture

A `coordinator` layer above `task_runner`: decompose a goal into **independent**
sub-goals, start each as its own `Task` under its own domain agent, and join.

- `Task` gains `parent_task_id` (idempotent migration) so the Agents panel can
  render a tree.
- Decomposition is one temp-0 call returning sub-goals **with a declared
  dependency edge set**; code enforces that only edge-free sub-goals run
  concurrently. The LLM proposes the split; **code decides what may run in
  parallel** — the `classify_autonomy` division of labour.
- A hard fan-out cap (3–4). Beyond that, quota and DB contention cost more than
  the latency saved.
- Approval pauses are per sub-task and unchanged; each pushes its own card.

### The thing to be careful about

Two agents writing the same file, or two browse runs contending for the single
Chromium profile. `browser/window.py` already serializes browse runs behind
`driving_run`, so the browser is safe by construction — but **file-domain
sub-tasks must not be allowed to run concurrently against overlapping paths**.
Simplest correct rule: only one WRITE-capable sub-task runs at a time; READ
sub-tasks fan out freely. That captures most of the available latency win with
none of the interference risk.

### Honest assessment

This is the **lowest-value item on this list** and the highest-risk. It buys
latency on multi-domain requests, which are rare, and introduces concurrency
into the one part of the system where every existing guarantee assumes a single
sequential plan. Build features 1, 2 and 7 first.

---

# Cross-cutting prerequisites

Not features, but each one caps everything above.

### A. Provider fallback ladder

`app/providers/factory.py` is an `if/elif` returning exactly one provider.
Every feature funnels through `create_provider()` / `get_llm_provider()` —
routing, planning, summaries, extraction, browse decisions, initiative,
briefings, memory digests. One outage and Jarvis is a text box.
`app/providers/ollama.py` is 145 implemented lines, unreachable without editing
`.env` and restarting.

**The pattern already exists in this repo, wired to the wrong channel.**
`app/providers/vision.py` contains `RotatingVisionProvider`: an ordered
credential pool with a process-global per-key cooldown registry that rotates
*within the same call* and degrades cleanly when everything is cooling. That is
on the **optional** channel, where degrading to DOM-only is fine. The critical
channel has nothing.

A `FallbackProvider` implementing the existing `LLMProvider` ABC is a drop-in
at all ~7 call sites via `get_llm_provider()`. It also unlocks **per-role model
routing** (deferred since Phase 3.5): a cheap fast model for the temperature-0
classifier — `route_bench` measured its p50 at 1600ms — and a strong model for
the planner. `route_bench` and `plan_bench` already exist to prove no
regression.

### B. Packaging

`package.json:33-36` ships only `dist-electron` and `frontend/dist`;
`electron/main.ts:75-77` spawns bare `python -m uvicorn` from a path the
installer never writes; `main.ts:137` opens the window anyway on timeout; and
`preload.ts` exposes `onBackendReady` / `onBackendError` that `main.ts` **never
sends**, so the UI cannot report a failed backend even if it wanted to. 37 env
vars, no signing, no auto-update. Every feature above runs on exactly one
machine until this is fixed.

### C. Guided first run

Every capability is correctly opt-in and therefore off. A new user gets a chat
box and a 2330-line `SettingsPanel.tsx` of ten cards. Each feature here adds
another card. Without onboarding, the roadmap makes the product *harder* to
adopt, not easier.

---

# Suggested order

| # | Work | Why here |
|---|---|---|
| 1 | **Home & IoT** | Most capability per line of code; composes free with routines, scheduler, initiative and voice. |
| 2 | **Desktop control** | Highest daily frequency; closes the sense/act asymmetry; **builds the `DesktopController` that Feature 7 needs**. |
| 3 | **Provider ladder** | Do it before the surface grows further — every feature added multiplies the blast radius of one outage. |
| 4 | **Gesture control** | Depends on #2's actuation layer. Highest wow, highest care. |
| 5 | **On-demand vision** | Small; connects two components that already exist. |
| 6 | **Messaging** | Real utility; reuses the recipient lock unchanged. |
| 7 | **Documents / media** | Breadth. Independent of everything else. |
| 8 | **Packaging + onboarding** | Once the feature set is stable enough to be worth installing. |
| 9 | **Multi-agent parallelism** | Last. Lowest value, highest risk. |

# Verification

Each feature ships with the gates this project already uses:

- Full backend suite green, with the new module's tests **falsified by
  reverting each behavioural change in place** — never `git show :file`, and
  the harness must re-read the patched file to confirm the revert landed before
  trusting a green result.
- `npm run typecheck` and `npx vite build` clean for anything touching the
  frontend.
- `route_bench.py` and `plan_bench.py` re-run for anything touching routing or
  planning — reporting decomposed numbers, never a single score.
- A **runtime check on the real `main.py` lifespan** with an isolated backend
  and scratch DB, because a hermetic test cannot tell you the wiring boots.
- For Features 1, 2 and 7: a user-driven live acceptance run, since none of
  them can be fully proven by a fake.

"""
Jarvis OS — DOM Observation (Phase 14, Part 1)

Turns a live page into the text a text-only LLM can act on: a numbered list of
the interactive elements, then the page's prose.

Why DOM and not vision
----------------------
There is no vision to use: providers/base.py declares `LLMMessage.content: str`
(a bare string — a multimodal content list fails validation before it reaches a
provider), and the live provider, deepseek-chat, has no image input at all.
Gemini is the only configured multimodal model and would need the ABC widened.

But this is the better call regardless, and would be even with vision on tap:
the DOM yields real element identity, where a screenshot yields coordinates that
a scroll invalidates. Note also that screen_ocr.run_ocr is NOT an alternative
here — it returns text, not element identity. You can read a canvas with it; you
can never click what it found.

THE INDEX CONTRACT — why a stale index cannot click the wrong thing
-------------------------------------------------------------------
The LLM says "click 3". If 3 were resolved by re-querying the DOM, a page that
navigated or re-rendered between the observation and the click would hand back a
DIFFERENT element 3 — the loop would click something the user never saw named,
and nothing would notice. That is folder_resolver's "code never picks" hazard
with teeth.

So each observation stamps every element it lists with BOTH a fresh observation
id and its index (data-jarvis-obs / data-jarvis-idx), and resolution requires
both to match. A navigation destroys the document and every attribute with it,
so a stale index resolves to NOTHING and raises a clean StaleObservation. The
invalidation is structural — a property of how documents work, not a check
someone has to remember to write.

THE BUDGET — the 5-wide trap, restated
--------------------------------------
CLAUDE.md records it plainly: widening retrieval without widening the render cap
starves the record (measured — 6 of 8 sources lost), and the fix was that
actionable data must never compete with prose for room (_fmt_search_files puts
its aggregate line FIRST so the per-step clip can never eat it).

So the split here is HARD, not a shared pool: elements get their own budget and
are rendered first; page text gets its own and is rendered second. A chatty
article can never push the search box out of the observation. Every cut is
marked, stating the fact and only the fact — a cut marker that also named a
remedy got copied verbatim into user-facing prose a dozen times (the
_missing_target leak), so these say what was cut and stop talking.

_STEP_RESULT_CAPS["browse_page"] in rendering.py must move with these numbers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

# --------------------------------------------------------------------- budget
_ELEMENT_BUDGET = 6000     # chars, ~80 elements — rendered FIRST
_PAGE_TEXT_BUDGET = 4000   # chars of prose
_NAME_MAX = 120            # one element's accessible name
_HREF_MAX = 100
_VALUE_MAX = 80

_OBS_ATTR = "data-jarvis-obs"
_IDX_ATTR = "data-jarvis-idx"


class StaleObservation(RuntimeError):
    """An index from an observation the page has since replaced."""


@dataclass
class Element:
    index: int
    role: str
    name: str
    value: str = ""
    href: str = ""
    # The element's on-screen box in CSS pixels (x, y, width, height), viewport
    # coords — the same space getBoundingClientRect() returns. Used ONLY by the
    # 15.3 vision fallback to map a vision-reported point back to a real element
    # (vision LOCATES, DOM ACTS). NOT rendered into the prompt — it is code data,
    # so the element/text budgets are unchanged. Default zero so a fake element
    # in a test (or an old observation shape) is valid.
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # FORM MEMBERSHIP (action-level safety, 2026-07-21): what the gesture gate
    # in browser_loop._act reads to refuse an unapproved submit gesture in
    # code. Defaults keep fake elements and old observation shapes valid (no
    # form info → nothing is ever refused on its account).
    form_member: bool = False
    form_submit: bool = False       # this element IS the form's submit control
    form_method: str = ""           # the form's method, uppercased ("" unknown)
    form_search: bool = False       # search-shaped form: submitting is reading

    def render(self) -> str:
        line = f'[{self.index}] {self.role} "{self.name}"' if self.name else f"[{self.index}] {self.role}"
        if self.value:
            line += f" = {self.value!r}"
        if self.href:
            line += f" → {self.href}"
        if self.form_submit and not self.form_search and (self.form_method or "GET") != "GET":
            line += " (submits a form)"
        return line


@dataclass
class Observation:
    observation_id: str
    url: str
    title: str
    elements: list[Element]
    element_total: int
    page_text: str
    text_truncated: bool
    # The page's viewport in CSS pixels (width, height). Lets the 15.3 vision
    # fallback convert a FRACTIONAL point (0..1, independent of screenshot
    # downscale) into the CSS-pixel space the element rects live in. (0, 0) when
    # unknown (a fake page in a test) — resolve_point_to_index then no-ops safely.
    viewport: tuple[float, float] = (0.0, 0.0)
    # The 15.4 CAPTCHA/verification probe result — None (the common case) or
    # {kind, mode, blocking, solved, zones} as _EXTRACT_JS documents. A challenge
    # widget lives in a cross-origin iframe or closed shadow root the element
    # list never captures, so it is detected structurally in-page, never from
    # prose. browser_loop.detect_challenge reads this; Jarvis never solves one.
    # mode 'interstitial' = the page IS the challenge; 'embedded' = a widget on
    # an ordinary page. `zones` are viewport rects of the widget(s) — every
    # overlapping element was already skipped during stamping, and the helpers
    # below veto the vision path and act-time clicks as defense in depth.
    # `solved` = a response field carries a token (the human completed it).
    challenge: Optional[dict] = None

    def index_map(self) -> dict[int, Element]:
        return {e.index: e for e in self.elements}

    def challenge_mode(self) -> str:
        """'interstitial' | 'embedded' | '' (no challenge). A challenge dict
        WITHOUT a mode (an old-shaped fake in a test) reads as 'interstitial' —
        the conservative direction: an interstitial misread stops honestly,
        an embedded misread would keep acting."""
        if not isinstance(self.challenge, dict):
            return ""
        return str(self.challenge.get("mode") or "interstitial")

    def challenge_solved(self) -> bool:
        return bool(isinstance(self.challenge, dict) and self.challenge.get("solved"))

    def challenge_zone_rects(self) -> list[tuple[float, float, float, float]]:
        """The detected widget boxes (viewport CSS px), parsed defensively."""
        if not isinstance(self.challenge, dict):
            return []
        rects: list[tuple[float, float, float, float]] = []
        for zone in self.challenge.get("zones") or []:
            if not isinstance(zone, dict):
                continue
            try:
                rects.append((
                    float(zone.get("x") or 0.0), float(zone.get("y") or 0.0),
                    float(zone.get("w") or 0.0), float(zone.get("h") or 0.0),
                ))
            except (TypeError, ValueError):
                continue
        return [r for r in rects if r[2] > 0 and r[3] > 0]


# ---------------------------------------------------------------- challenge probe
# The CAPTCHA/verification probe, as a standalone JS expression. Kept separate
# from the element-extraction JS (they are different jobs — 15.4 safety vs
# perception) and concatenated into _EXTRACT_JS below so both still run in ONE
# page.evaluate. The full detection rationale lives in the JS comments.
_CHALLENGE_PROBE_JS = """
  // CAPTCHA / verification CHALLENGE probe (15.4, widened 2026-07-19 after a
  // live auto-click: the old probe matched only the api2 path on google.com,
  // and only in the top document — so recaptcha.net, reCAPTCHA Enterprise,
  // same-origin sub-frames, shadow-rooted Turnstile and custom checkboxes were
  // all invisible to it, and one of them got clicked). A challenge widget is
  // detected structurally, never by page prose, via THREE independent signals:
  //   1. known vendor iframes, by src — any Google/recaptcha.net /recaptcha/
  //      path (api2 AND enterprise), hCaptcha, Cloudflare;
  //   2. known container classes (.g-recaptcha / .cf-turnstile / .h-captcha) —
  //      Turnstile's iframe hides in a CLOSED shadow root querySelectorAll can
  //      never pierce, but its host container is ordinary light DOM;
  //   3. the hidden RESPONSE FIELD every major vendor injects into the host
  //      page's light DOM (g-recaptcha-response / cf-turnstile-response /
  //      h-captcha-response) — the one tell a shadow root, custom wrapper, or
  //      renamed container cannot hide.
  // Exclusions that must not regress: the invisible reCAPTCHA v3 badge
  // (.grecaptcha-badge / size=invisible — it rides along on countless ordinary
  // forms and blocks nothing; its injected response textarea lives INSIDE the
  // badge, so the closest() check below skips it), and any widget not RENDERED
  // on-screen at a real size (the 2026-07-18 hidden-modal false positive).
  //
  // Returns null or {kind, mode, blocking, solved, zones}:
  //   mode  — 'interstitial' (the PAGE is the challenge — Cloudflare full-page
  //           IDs) or 'embedded' (a widget sitting on an ordinary page).
  //   zones — viewport rects (top-page CSS px) of every detected widget. The
  //           element walk below SKIPS anything intersecting a zone, so a
  //           challenge control can never be stamped, listed, or clicked — the
  //           structural half of "Jarvis never touches a CAPTCHA".
  //   solved — a response field carries a non-empty token (how a resumed commit
  //           verifies the HUMAN's solve actually happened).
  //   blocking — true only for an interstitial; an embedded widget no longer
  //           halts observation (its policy lives in browser_loop).
  (() => {
    try {
      const zones = [];
      let kind = null;
      let solved = false;
      const addZone = (r) => { if (r) zones.push({ x: r.left, y: r.top, w: r.width, h: r.height }); };
      // Rendered on-screen at a real size — a 0×0 widget in a display:none
      // modal and the off-screen v3 badge both fail this.
      const renderedRect = (el) => {
        const r = el.getBoundingClientRect();
        const ok = r.width > 60 && r.height > 40
          && r.bottom > 0 && r.right > 0
          && r.top < (window.innerHeight || 0) && r.left < (window.innerWidth || 0);
        return ok ? r : null;
      };
      // Widget-sized only. A response-field climb that reaches something
      // form-sized must NOT become a zone — it would swallow the form's own
      // fields and blind the loop to legitimate inputs.
      const widgetRect = (r) => (r && r.width <= 600 && r.height <= 800) ? r : null;

      const scanDoc = (doc, offX, offY, depth) => {
        const off = (r) => r && ({ left: r.left + offX, top: r.top + offY, width: r.width, height: r.height });
        for (const f of Array.from(doc.querySelectorAll('iframe'))) {
          const src = (f.getAttribute('src') || '').toLowerCase();
          let k = null;
          if ((src.includes('google.com/recaptcha/') || src.includes('recaptcha.net/recaptcha/'))
              && !src.includes('size=invisible')) k = 'reCAPTCHA';
          else if (src.includes('hcaptcha.com')) k = 'hCaptcha';
          else if (src.includes('challenges.cloudflare.com')) k = 'Cloudflare';
          if (!k) continue;
          const r = renderedRect(f);
          if (!r) continue;
          kind = kind || k;
          addZone(off(r));
        }
        const containers = [
          ['.g-recaptcha', 'reCAPTCHA'], ['.cf-turnstile', 'Cloudflare'], ['.h-captcha', 'hCaptcha'],
        ];
        for (const pair of containers) {
          for (const el of Array.from(doc.querySelectorAll(pair[0]))) {
            const r = widgetRect(renderedRect(el));
            if (!r) continue;
            kind = kind || pair[1];
            addZone(off(r));
          }
        }
        const fields = doc.querySelectorAll(
          '[name^=g-recaptcha-response], [name^=cf-turnstile-response], [name^=h-captcha-response]'
        );
        for (const field of Array.from(fields)) {
          if (field.closest && field.closest('.grecaptcha-badge')) continue;  // v3 badge
          const name = (field.getAttribute('name') || '');
          const k = name.indexOf('cf-') === 0 ? 'Cloudflare'
            : name.indexOf('h-') === 0 ? 'hCaptcha' : 'reCAPTCHA';
          if ((field.value || '').trim()) solved = true;
          let el = field.parentElement;
          for (let hops = 0; el && hops < 4; hops++, el = el.parentElement) {
            const r = widgetRect(renderedRect(el));
            if (r) { kind = kind || k; addZone(off(r)); break; }
          }
        }
        // Same-origin sub-frames (an embedded form iframe carrying its own
        // widget). A cross-origin contentDocument throws — best-effort; the
        // cross-origin widget iframes themselves are caught by src above.
        if (depth < 2) {
          for (const f of Array.from(doc.querySelectorAll('iframe'))) {
            try {
              const child = f.contentDocument;
              if (!child) continue;
              const fr = f.getBoundingClientRect();
              scanDoc(child, offX + fr.left, offY + fr.top, depth + 1);
            } catch (e) {}
          }
        }
      };
      scanDoc(document, 0, 0, 0);

      // Cloudflare full-page interstitial: the PAGE is the challenge. These IDs
      // only exist on the real challenge page, so no visibility guard needed.
      if (document.querySelector('#challenge-running, #cf-challenge-running, #challenge-form, #cf-please-wait')) {
        return { kind: kind || 'Cloudflare', mode: 'interstitial', blocking: true, solved: solved, zones: zones };
      }
      if (kind) {
        return { kind: kind, mode: 'embedded', blocking: false, solved: solved, zones: zones };
      }
    } catch (e) {}
    return null;
  })()"""


# ---------------------------------------------------------------- extraction
# Runs IN the page. Stamps the elements it lists (see the index contract above)
# and returns plain data. Deliberately verbose about what it skips: an invisible
# or disabled control is not actionable, and listing it spends budget to hand the
# model a move it cannot make.
_EXTRACT_JS = """
(obsId) => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=textbox]', '[role=searchbox]',
    '[role=combobox]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[role=option]', '[contenteditable=""]',
    '[contenteditable=true]'
  ].join(',');

  const clip = (s, n) => {
    s = (s == null ? '' : String(s)).replace(/\\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) + '…' : s;
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    return true;
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'password') return 'password';
      if (t === 'file') return 'file';
      if (t === 'checkbox' || t === 'radio') return t;
      if (t === 'search') return 'searchbox';
      return 'input';
    }
    if (tag === 'textarea') return 'textbox';
    return tag;
  };

  const nameOf = (el) => (
    el.getAttribute('aria-label') ||
    el.getAttribute('placeholder') ||
    clip(el.innerText || '', 200) ||
    el.getAttribute('title') ||
    el.getAttribute('alt') ||
    el.getAttribute('name') ||
    (el.tagName.toLowerCase() === 'input' ? (el.getAttribute('value') || '') : '')
  );

  const challengeInfo = """ + _CHALLENGE_PROBE_JS + """;

  // The no-touch exclusion: anything overlapping a challenge widget's box is
  // never stamped or listed — the LLM cannot click what it is never shown, and
  // a custom "I'm not a robot" checkbox in the light DOM dies here too.
  const challengeZones = (challengeInfo && challengeInfo.zones) || [];
  const inChallengeZone = (r) => challengeZones.some((z) =>
    r.left < z.x + z.w && r.left + r.width > z.x &&
    r.top < z.y + z.h && r.top + r.height > z.y
  );

  document.querySelectorAll('[' + 'data-jarvis-obs' + ']').forEach((el) => {
    el.removeAttribute('data-jarvis-obs');
    el.removeAttribute('data-jarvis-idx');
  });

  const out = [];
  let idx = 0;
  let total = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') continue;
    if (el.disabled) continue;
    if (el.getAttribute('aria-hidden') === 'true') continue;
    if (!visible(el)) continue;
    if (inChallengeZone(el.getBoundingClientRect())) continue;
    total++;
    idx++;
    el.setAttribute('data-jarvis-obs', obsId);
    el.setAttribute('data-jarvis-idx', String(idx));
    const role = roleOf(el);
    // A password field's VALUE is never read — not clipped, not redacted-with-
    // a-hint, simply never taken. Jarvis does not handle credentials (14.4).
    const value = (role === 'password') ? '' : clip(el.value || '', 80);
    let href = '';
    if (tag === 'a') {
      href = el.getAttribute('href') || '';
      if (href.startsWith('javascript:')) href = '';
    }
    // The element's on-screen box (CSS px, viewport coords) — for the 15.3
    // vision fallback to map a vision-reported point back to this element.
    const r = el.getBoundingClientRect();
    // FORM MEMBERSHIP (action-level safety, 2026-07-21): with page traffic
    // flowing, the SUBMIT GESTURE is what the loop must refuse in code — so
    // each element carries whether it belongs to a form, whether it IS the
    // form's submit control, the form's method, and whether the form is
    // search-shaped (submitting a search is reading; role=search, or a form
    // with at most one visible text control and no password/email/file).
    let form = null;
    try {
      const f = el.closest ? el.closest('form') : null;
      if (f) {
        const t = (el.getAttribute('type') || '').toLowerCase();
        const isSearch = !!(el.closest('[role=search]')) ||
          f.getAttribute('role') === 'search' ||
          (!f.querySelector('input[type=password],input[type=email],input[type=file],textarea,select') &&
           f.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image])').length <= 1);
        form = {
          method: (f.getAttribute('method') || 'GET').toUpperCase(),
          submit: (tag === 'button' && (t === 'submit' || t === '')) ||
                  (tag === 'input' && (t === 'submit' || t === 'image')),
          search: !!isSearch
        };
      }
    } catch (e) {}
    out.push({
      index: idx,
      role: role,
      name: clip(nameOf(el), 120),
      value: value,
      href: clip(href, 100),
      rect: { x: r.left, y: r.top, w: r.width, h: r.height },
      form: form
    });
  }

  return {
    url: location.href,
    title: document.title || '',
    elements: out,
    total: total,
    text: (document.body ? document.body.innerText : '') || '',
    viewport: { width: window.innerWidth || 0, height: window.innerHeight || 0 },
    challenge: challengeInfo
  };
}
"""


async def observe(page: Any) -> Observation:
    """Snapshot one page. Best-effort about the prose (a page with no body text
    is normal), strict about the elements (they are what the loop acts on)."""
    observation_id = uuid.uuid4().hex[:12]
    raw = await page.evaluate(_EXTRACT_JS, observation_id)
    if not isinstance(raw, dict):
        raise RuntimeError(f"page observation returned {type(raw).__name__}, expected an object")

    elements = [
        Element(
            index=int(item.get("index", 0)),
            role=str(item.get("role") or "element"),
            name=str(item.get("name") or "")[:_NAME_MAX],
            value=str(item.get("value") or "")[:_VALUE_MAX],
            href=str(item.get("href") or "")[:_HREF_MAX],
            rect=_rect_of(item.get("rect")),
            **_form_of(item.get("form")),
        )
        for item in (raw.get("elements") or [])
        if isinstance(item, dict)
    ]

    text = str(raw.get("text") or "").strip()
    truncated = len(text) > _PAGE_TEXT_BUDGET
    if truncated:
        text = text[:_PAGE_TEXT_BUDGET]

    return Observation(
        observation_id=observation_id,
        url=str(raw.get("url") or page.url or ""),
        title=str(raw.get("title") or ""),
        elements=elements,
        element_total=int(raw.get("total") or len(elements)),
        page_text=text,
        text_truncated=truncated,
        viewport=_viewport_of(raw.get("viewport")),
        challenge=(raw.get("challenge") if isinstance(raw.get("challenge"), dict) else None),
    )


def _rect_of(raw: Any) -> tuple[float, float, float, float]:
    """A JS rect {x, y, w, h} → a tuple, best-effort (zero on any bad shape —
    an element with no box simply never matches a vision point)."""
    if not isinstance(raw, dict):
        return (0.0, 0.0, 0.0, 0.0)
    try:
        return (
            float(raw.get("x") or 0.0),
            float(raw.get("y") or 0.0),
            float(raw.get("w") or 0.0),
            float(raw.get("h") or 0.0),
        )
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


def _form_of(raw: Any) -> dict[str, Any]:
    """The JS form-membership object → Element kwargs, best-effort (a missing
    or malformed form object simply means 'not in a form' — nothing is ever
    refused on its account)."""
    if not isinstance(raw, dict):
        return {}
    return {
        "form_member": True,
        "form_submit": bool(raw.get("submit")),
        "form_method": str(raw.get("method") or "").upper(),
        "form_search": bool(raw.get("search")),
    }


def _viewport_of(raw: Any) -> tuple[float, float]:
    if not isinstance(raw, dict):
        return (0.0, 0.0)
    try:
        return (float(raw.get("width") or 0.0), float(raw.get("height") or 0.0))
    except (TypeError, ValueError):
        return (0.0, 0.0)


# ------------------------------------------------------------------ resolve
async def resolve(page: Any, observation: Observation, index: int) -> Any:
    """An index → the element handle it named, or StaleObservation. Both the
    observation id AND the index must match: after a navigation the attributes
    are gone with the old document, so this raises rather than clicking whatever
    happens to be third on the new page. See the index contract."""
    selector = f'[{_OBS_ATTR}="{observation.observation_id}"][{_IDX_ATTR}="{int(index)}"]'
    handle = await page.query_selector(selector)
    if handle is None:
        raise StaleObservation(
            f"Element [{index}] is no longer on the page — it changed since it "
            f"was read. Look at the page again before acting on it."
        )
    return handle


# ------------------------------------------------------------------- render
def visible_span(observation: Observation, skip_elements: int = 0) -> tuple[int, int]:
    """The half-open [start, end) slice of `observation.elements` that
    render(skip_elements=...) will fit inside the element budget. This is how the
    browse loop's "more" paging knows where the NEXT window starts — the window
    is a CHAR budget, not a fixed count, so only this loop can answer it (the
    WWR incident: 240 elements, ~47 fit, and the Back-End Programming section
    sat at index 173 — unreachable without paging)."""
    start = max(0, min(int(skip_elements or 0), len(observation.elements)))
    used = 0
    end = start
    for element in observation.elements[start:]:
        line = element.render()
        if used + len(line) + 1 > _ELEMENT_BUDGET:
            break
        used += len(line) + 1
        end += 1
    return start, end


def render(observation: Observation, *, skip_elements: int = 0) -> str:
    """The observation as the LLM sees it. Elements first and within their own
    budget — prose can never crowd out the actionable half (see THE BUDGET).

    `skip_elements` slides the element WINDOW (the browse loop's "more" paging):
    the first N elements are skipped so the next budget-worth renders. Indexes
    are unchanged — every element keeps its stamped index, so an element shown
    in an earlier window is still clickable by that index."""
    head = [f"URL: {observation.url}"]
    if observation.title:
        head.append(f"TITLE: {observation.title}")

    start, end = visible_span(observation, skip_elements)
    lines = [e.render() for e in observation.elements[start:end]]
    shown = end - start

    if start > 0 or shown < observation.element_total:
        span = f"{start + 1}–{end} of {observation.element_total}" if start > 0 else f"{shown} of {observation.element_total}"
        header = f"ELEMENTS ({span} shown):"
        remaining = observation.element_total - end
        footer = (
            f"… {remaining} more elements not shown." if remaining > 0 else None
        )
    else:
        header = f"ELEMENTS ({shown}):"
        footer = None
    if not observation.elements:
        header = "ELEMENTS (none — nothing on this page can be clicked or typed into):"

    body = [header, *lines]
    if footer:
        body.append(footer)

    text = observation.page_text
    if text:
        body.append("")
        body.append("PAGE TEXT:")
        body.append(text + ("\n… (truncated)" if observation.text_truncated else ""))

    return "\n".join(head + [""] + body)


def summarize(observation: Observation) -> dict[str, Any]:
    """The tool-result shape. `rendered` is what a summary/planner prompt reads;
    the structured fields are for code."""
    return {
        "url": observation.url,
        "title": observation.title,
        "rendered": render(observation),
        "element_count": observation.element_total,
        "elements_shown": min(len(observation.elements), observation.element_total),
        "text_truncated": observation.text_truncated,
        "observation_id": observation.observation_id,
    }


# ----------------------------------------------------- vision fallback (15.3)
# The Phase 8 screen-OCR discipline: a DOWNSCALED capture, held in memory, NEVER
# persisted. Only taken when the loop is stuck, and only sent to the vision model
# in that moment.
_SCREENSHOT_MAX_DIM = 1280        # px — the longer viewport edge, after downscale
_SCREENSHOT_JPEG_QUALITY = 60


# SET-OF-MARKS overlay (vision-first hybrid, 2026-07-21): a numbered badge +
# outline over every rendered element, drawn IN-PAGE from the SAME rects the
# element list carries — so the numbers the vision model sees in the picture
# ARE the indices it may answer with, and they can never disagree with the
# text list. pointer-events:none throughout: the overlay can intercept nothing.
_MARK_JS = """(items) => {
  const old = document.getElementById('jarvis-marks');
  if (old) old.remove();
  const holder = document.createElement('div');
  holder.id = 'jarvis-marks';
  holder.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;' +
    'z-index:2147483647;pointer-events:none;';
  for (const it of items) {
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;left:' + it.x + 'px;top:' + it.y +
      'px;width:' + it.w + 'px;height:' + it.h + 'px;' +
      'outline:2px solid rgba(225,29,72,0.85);pointer-events:none;';
    holder.appendChild(box);
    const badge = document.createElement('div');
    badge.textContent = String(it.i);
    badge.style.cssText = 'position:fixed;left:' + Math.max(0, it.x - 2) +
      'px;top:' + Math.max(0, it.y - 14) + 'px;' +
      'background:#e11d48;color:#fff;font:bold 11px/14px monospace;' +
      'padding:0 3px;border-radius:2px;pointer-events:none;';
    holder.appendChild(badge);
  }
  document.documentElement.appendChild(holder);
}"""

_UNMARK_JS = """() => {
  const h = document.getElementById('jarvis-marks');
  if (h) h.remove();
}"""


async def capture_marked(
    page: Any, observation: "Observation", skip_elements: int = 0
) -> Optional[bytes]:
    """A set-of-marks screenshot: overlay numbered badges + outlines for the
    elements in the rendered window, capture the viewport, remove the overlay.
    Best-effort at every stage — a mark failure degrades to the plain
    screenshot, a capture failure to None (the caller falls back to the
    text-only decision path)."""
    lo, hi = visible_span(observation, skip_elements)
    items = [
        {"i": e.index, "x": e.rect[0], "y": e.rect[1], "w": e.rect[2], "h": e.rect[3]}
        for e in observation.elements[lo:hi]
        if e.rect[2] > 0 and e.rect[3] > 0
    ]
    marked = False
    if items:
        try:
            await page.evaluate(_MARK_JS, items)
            marked = True
        except Exception as exc:
            logger.debug(f"set-of-marks overlay: {type(exc).__name__}: {exc}")
    try:
        return await capture_screenshot(page)
    finally:
        if marked:
            try:
                await page.evaluate(_UNMARK_JS)
            except Exception:
                pass


async def capture_screenshot(
    page: Any, *, max_dim: int = _SCREENSHOT_MAX_DIM
) -> Optional[bytes]:
    """A JPEG of the current VIEWPORT (not the full page), downscaled so its
    longer edge is at most `max_dim`. Held in memory by the caller and dropped
    after the one vision call — never written to disk (the Phase 8 no-full-res
    rule). Best-effort: returns None on any failure, and the loop then stops
    honestly rather than crashing.

    Downscaling uses Pillow when importable and is skipped otherwise (the raw
    viewport JPEG is returned) — dimension downscale is a cost/privacy nicety,
    not a correctness requirement: the vision fallback asks for FRACTIONAL
    coordinates, so mapping a point back to an element never depends on the
    image's pixel size."""
    try:
        raw = await page.screenshot(type="jpeg", quality=_SCREENSHOT_JPEG_QUALITY)
    except Exception as exc:
        logger.debug(f"screenshot capture: {type(exc).__name__}: {exc}")
        return None
    if not raw:
        return None
    return _downscale_jpeg(bytes(raw), max_dim)


def _downscale_jpeg(data: bytes, max_dim: int) -> bytes:
    """Shrink a JPEG so its longer edge ≤ max_dim, via a lazy Pillow import.
    Returns the original bytes unchanged when Pillow is absent or already small —
    the capture is best-effort, so a missing optional dep never fails it."""
    try:
        import io

        from PIL import Image  # optional — not a base dependency
    except Exception:
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            longest = max(width, height)
            if longest <= max_dim:
                return data
            scale = max_dim / float(longest)
            resized = img.convert("RGB").resize(
                (max(1, int(width * scale)), max(1, int(height * scale)))
            )
            buffer = io.BytesIO()
            resized.save(buffer, format="JPEG", quality=_SCREENSHOT_JPEG_QUALITY)
            return buffer.getvalue()
    except Exception as exc:
        logger.debug(f"screenshot downscale: {type(exc).__name__}: {exc}")
        return data


def rect_intersects_zones(
    rect: tuple[float, float, float, float],
    zones: list[tuple[float, float, float, float]],
) -> bool:
    """Does an element box overlap any challenge-widget box? The act-time half
    of the no-touch rule (browser_loop._act refuses the action) — the JS walk
    already skipped overlapping elements at stamping time; this catches drift
    (a widget that rendered between observation and act) and any path that
    hands an element to the loop without the walk."""
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return False
    return any(
        x < zx + zw and x + w > zx and y < zy + zh and y + h > zy
        for zx, zy, zw, zh in zones
    )


def resolve_point_to_index(
    observation: Observation, x_frac: float, y_frac: float
) -> Optional[int]:
    """A fractional viewport point (0..1 on each axis, as the vision model
    reports) → the index of the element whose on-screen box CONTAINS it, or None.

    This is the "vision LOCATES, DOM ACTS" hinge: vision gives a location, and
    this returns a real element index the loop then acts on through the ordinary
    index contract (resolve() by obs-id + index). A point over no listed element
    (a canvas with nothing clickable there) returns None — an honest miss, never
    a click on whatever happens to be nearby.

    On overlap (nested boxes — a label inside a button) the SMALLEST-area element
    wins: it is the most specific, tightest target under the cursor. Zero-area or
    unknown-viewport cases no-op to None (a fake page in a test)."""
    vw, vh = observation.viewport
    if vw <= 0 or vh <= 0:
        return None
    try:
        px = float(x_frac) * vw
        py = float(y_frac) * vh
    except (TypeError, ValueError):
        return None

    # A point inside a challenge widget's box maps to NOTHING — vision may
    # locate the "I'm not a robot" checkbox, but it can never be acted on
    # (the no-touch rule; the honest miss beats a click on whatever overlaps).
    if rect_intersects_zones((px, py, 1.0, 1.0), observation.challenge_zone_rects()):
        return None

    best_index: Optional[int] = None
    best_area = float("inf")
    for element in observation.elements:
        x, y, w, h = element.rect
        if w <= 0 or h <= 0:
            continue
        if x <= px <= x + w and y <= py <= y + h:
            area = w * h
            if area < best_area:
                best_area = area
                best_index = element.index
    return best_index

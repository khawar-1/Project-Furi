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

A PROMPT budget is not a CAPTURE budget (2026-07-26)
----------------------------------------------------
Those two budgets above bound what the DECISION MODEL READS. They used to bound
what left the page as well, which quietly made every later reader blind to the
same ceiling — see CAPTURE vs RENDER at the constants. The Observation now
carries `text_full` (the prose as captured) and `Element.name_full` (the name
before the prompt clip) alongside the rendered `page_text`/`name`. Both are CODE
data, exactly like `rect` and `frame_id`: `render()` does not touch them, so the
prompt, the action signature and the page fingerprint are unchanged. Anything
that needs the whole page — `browser.extract` above all — reads the full fields;
anything building a prompt keeps reading the clipped ones.
"""
from __future__ import annotations

import asyncio
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

# CAPTURE vs RENDER (2026-07-26) — they were the SAME number, and that was a bug
# with a measured cost. `_PAGE_TEXT_BUDGET` is a PROMPT budget: how much prose the
# decision model should read. It was also doing duty as a CAPTURE limit (the text
# was clipped the moment it left the page), so nothing downstream could ever see
# past it — `browser_loop._extract_data` reads `page_text` and clips it to its own
# 9000-char ceiling, a ceiling it could never reach. Live on daraz.pk's real
# results page: the first 4000 chars of body.innerText are header, nav, categories
# and filters, so `extract` returned ZERO records three times running on a page
# holding 158 product cards, and the run died reporting a page problem.
#
# So capture is now generous and render stays exactly as tight as it was. The
# prompt is byte-for-byte unchanged; only code that asks for `text_full` sees more.
_PAGE_TEXT_CAPTURE = 24000  # chars kept on the Observation for code to read

# An element's name is clipped for the PROMPT (_NAME_MAX) — but a results-grid card
# carries its title, price and rating inside one element's innerText, and cutting
# that at 120 chars is how the data went missing. `name_full` keeps what the page
# gave us (the in-page clip, _JS_NAME_MAX) for extraction; `name` is untouched, so
# the rendered list, the dedupe signature and the page fingerprint do not move.
_JS_NAME_MAX = 200         # must match the clip() in _EXTRACT_JS's nameOf()
_NAME_FULL_MAX = 240

_OBS_ATTR = "data-jarvis-obs"
_IDX_ATTR = "data-jarvis-idx"

# CROSS-FRAME OBSERVATION (2026-07-26). Bounded so a page full of ad iframes
# cannot turn one observation into thirty CDP round-trips, and so a frame cannot
# crowd the top document out of the element budget.
_MAX_FRAMES = 6
_FRAME_MIN_AREA = 60_000     # css px² ≈ 300x200 — a content frame, not a beacon
_FRAME_ELEMENT_CAP = 40      # per frame


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
    # WHICH DOCUMENT this element lives in (2026-07-26). "" = the top page; any
    # other value identifies a child frame. Like `rect`, these are CODE data and
    # are NOT rendered into the prompt, so the element budget is unchanged.
    #
    # `frame_id` is how resolve() knows which document to look in. `frame_url` is
    # the base a relative href must be joined against — the loop used to join
    # every href to the TOP page's URL, which for a frame element resolves to a
    # different address entirely.
    frame_id: str = ""
    frame_url: str = ""
    # The element's name BEFORE the prompt clip (see _NAME_FULL_MAX). CODE data
    # like `rect`/`frame_id` — never rendered, so the element budget, the action
    # signature and the page fingerprint are all unaffected. Read by
    # browser.extract, which needs a product card's whole text (title + price +
    # rating live in one card's innerText). Defaults empty so every fake element
    # in the suite, and any older observation shape, stays valid — callers use
    # `name_full or name`.
    name_full: str = ""

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
    # The page's prose as CAPTURED (up to _PAGE_TEXT_CAPTURE), where `page_text`
    # is the same prose clipped to the PROMPT budget. Defaulted so every fake
    # Observation in the suite stays valid; readers use `text_full or page_text`.
    # This exists because extraction was structurally unable to see past the
    # prompt budget — see CAPTURE vs RENDER above.
    text_full: str = ""
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
(arg) => {
  // Argument shape is backward-tolerant on purpose: a bare string is the obsId
  // (every pre-2026-07-26 caller, and every fake page in the suite that ignores
  // its arguments entirely), while {obsId, base} carries an index BASE so a
  // child frame's indices continue the top document's instead of restarting at 1.
  const obsId = (arg && arg.obsId) || arg;
  const base = (arg && arg.base) || 0;
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=textbox]', '[role=searchbox]',
    '[role=combobox]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[role=option]', '[contenteditable=""]',
    '[contenteditable=true]'
  ].join(',');

  // TIER 1 — the WIDE net (2026-07-26). The strict selector above lists only
  // semantic controls, and a modern results grid frequently has none: daraz.pk's
  // search results are <div> cards wired to a JS router, so a fully-rendered page
  // was observed as ZERO elements and the model was asked what to click on
  // nothing. This tier is what makes such a page addressable. It engages ONLY
  // when the strict pass comes back thin (see WIDE_THRESHOLD), so an ordinary
  // page's element list is byte-for-byte what it was before.
  const WIDE = [
    '[onclick]', '[tabindex]:not([tabindex="-1"])',
    '[role=listitem]', '[role=article]', '[role=gridcell]', '[role=treeitem]',
    '[data-testid]', '[data-test]', '[data-qa]', '[data-item-id]', '[data-sku]',
    'article',
    'li[class*=card]', 'li[class*=item]', 'li[class*=product]',
    'div[class*=card]', 'div[class*=item]', 'div[class*=product]',
    'div[class*=tile]', 'div[class*=result]'
  ].join(',');

  const WIDE_THRESHOLD = 8;   // strict hits below this ⇒ engage the wide tier
  const WIDE_MAX = 60;        // and never list more than this many of them
  const SCAN_CAP = 40000;     // nodes visited
  const HIT_CAP = 600;        // candidates collected

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

  // OCCLUSION (2026-07-26). visible() answers "is this painted", never "can it
  // actually be reached" — so an element under a cookie banner or a sticky modal
  // was listed, the click landed on the banner, and the step was spent for
  // nothing. The wide tier below would have multiplied those. Fails OPEN when
  // the answer is unknowable (off-screen), because a false negative here HIDES a
  // real control, which is the worse error.
  // SHADOW RETARGETING, and it is not a detail — it silently hid every shadow
  // element the first time this was written. document.elementFromPoint on the
  // OUTER document retargets a hit inside a shadow tree to the HOST, and
  // Node.contains does not cross shadow boundaries, so `host.contains(inner)` is
  // false and every shadow control read as occluded by its own host. Caught by
  // the real-browser fixture on its first run; unreachable by any string test.
  //
  // Two corrections: hit-test in the element's OWN root (ShadowRoot has its own
  // elementFromPoint, which sees inside), and accept a hit that is one of el's
  // ancestor hosts.
  const unoccluded = (el, r) => {
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return true;
    let top = null;
    let root = null;
    try {
      root = el.getRootNode ? el.getRootNode() : document;
      const from = (root && root.elementFromPoint) ? root : document;
      top = from.elementFromPoint(cx, cy);
    } catch (e) { return true; }
    if (!top || top === el) return true;
    if (el.contains(top) || top.contains(el)) return true;
    // Walk el's chain of shadow hosts: a hit reported as any of them is the
    // retargeting artefact, not an overlay.
    let node = root;
    for (let i = 0; i < 4 && node && node.host; i++) {
      if (node.host === top || top.contains(node.host)) return true;
      node = node.host.getRootNode ? node.host.getRootNode() : null;
    }
    return false;
  };

  const eligible = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') return false;
    if (el.disabled) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    if (!visible(el)) return false;
    const r = el.getBoundingClientRect();
    if (inChallengeZone(r)) return false;
    if (!unoccluded(el, r)) return false;
    return true;
  };

  // A wide candidate has to earn its budget line. Four rules:
  //   1. NO CONTAINERS — a card <div> wrapping a listed <a> is dropped in favour
  //      of the <a>, which carries an href and therefore routes to the loop's
  //      GET fast path instead of a synthetic click.
  //   2. It must have a NAME. "[12] div" costs budget and is unactionable.
  //   3. Box sanity — big enough to be a target, not a full-page wrapper that
  //      merely happens to carry cursor:pointer.
  //   4. Everything eligible() already rejects.
  const POINTERY = (el, tag) => {
    if (tag !== 'div' && tag !== 'li' && tag !== 'span' && tag !== 'section') return false;
    // Lazily, and only for these tags: forcing style resolution on every node of
    // a 40k-node page is a multi-second stall.
    try { return getComputedStyle(el).cursor === 'pointer'; } catch (e) { return false; }
  };

  document.querySelectorAll('[' + 'data-jarvis-obs' + ']').forEach((el) => {
    el.removeAttribute('data-jarvis-obs');
    el.removeAttribute('data-jarvis-idx');
  });

  // THE WALK. One explicit-stack descent in DOCUMENT ORDER through the light DOM
  // and every OPEN shadow root. querySelectorAll does not pierce shadow roots at
  // all, so a page built from web components (Lit/Stencil/Polymer, and much of
  // modern retail) reported zero elements no matter how well it had rendered.
  // Shadow children are pushed LAST so they pop FIRST — immediately after their
  // host, which is where they visually belong.
  //
  // attachShadow({mode:'closed'}) yields null here and is unreachable by any
  // JavaScript, ours or Playwright's. That is a real, permanent limit.
  // ELIGIBILITY IS APPLIED **DURING** THE WALK, and that is not a micro-
  // optimisation — it is the difference between seeing a page and not.
  //
  // The first cut collected up to HIT_CAP raw selector matches and filtered
  // afterwards. Measured on daraz.pk's real results page (2026-07-26): 1055
  // strict matches, of which only 200 are VISIBLE — the other 855 are collapsed
  // mega-menu panels sitting early in document order. The cap was therefore
  // spent almost entirely on invisible nav chrome before the walk ever reached a
  // product, and a page with 200 usable controls was observed as ELEVEN.
  //
  // A budget must bound useful OUTPUT, not wasted scanning. SCAN_CAP is what
  // bounds the work.
  const strictOk = [];
  const wide = [];
  const pointerOnly = new Set();
  const stack = [document.documentElement || document.body];
  let seen = 0;
  while (stack.length && (strictOk.length + wide.length) < HIT_CAP && seen < SCAN_CAP) {
    const el = stack.pop();
    if (!el || !el.tagName) continue;
    seen++;
    // ONE GATE, deliberately. Classify first, then admit through a SINGLE
    // eligible() call — so there is exactly one place an element can enter a
    // candidate list, and the CAPTCHA no-touch guarantee (and every other
    // eligibility rule) cannot be bypassed by a tier added later. A test pins
    // that this stays single.
    try {
      let tier = -1;
      if (el.matches(SELECTOR)) tier = 0;
      else if (el.matches(WIDE)) tier = 1;
      else if (POINTERY(el, el.tagName.toLowerCase())) tier = 2;
      if (tier >= 0 && eligible(el)) {
        if (tier === 0) {
          strictOk.push(el);
        } else {
          wide.push(el);
          if (tier === 2) pointerOnly.add(el);   // an INHERITED style, not markup
        }
      }
    } catch (e) {}
    const kids = el.children || [];
    for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
    if (el.shadowRoot) {
      const sk = el.shadowRoot.children || [];
      for (let i = sk.length - 1; i >= 0; i--) stack.push(sk[i]);
    }
  }
  let listed = strictOk;
  if (strictOk.length < WIDE_THRESHOLD) {
    const keep = [];
    for (const el of wide) {
      if (keep.length >= WIDE_MAX) break;
      // eligible() already ran during the walk — see the note there.
      if (!clip(nameOf(el), 120)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 24 || r.height < 16) continue;
      if (r.width * r.height > 0.6 * innerWidth * innerHeight) continue;

      // NESTING. Both directions are wrong in different situations, and which
      // one to prefer is decided by HOW the candidate matched. Both cases were
      // measured, not reasoned:
      //
      //   1. Wrapping a STRICT hit → drop, keep the control. It carries an href
      //      and routes to the loop's GET fast path instead of a synthetic click.
      //
      //   2. Matched by an INHERITED pointer cursor, inside another such match →
      //      keep the OUTER. `cursor: pointer` inherits, so a pointer-styled
      //      card hands the identical signal to every div inside it: live, one
      //      product card became four entries (card + title + price + rating).
      //      The card is the click target; its text lines are not.
      //
      //   3. Matched by MARKUP, wrapping another markup match → keep the INNER.
      //      A grid's `<div class="results">` matches `div[class*=result]` just
      //      as its `<div class="product-card">` children match `div[class*=card]`,
      //      and preferring the outer there swallows every card into one
      //      unclickable wrapper (measured: 3 cards became 1 wrapper).
      //
      // The principle: an inheritance ARTEFACT points outward to its origin, and
      // explicit markup points inward to the more specific thing.
      let nested = false;
      for (const other of strictOk) {
        if (el !== other && el.contains(other)) { nested = true; break; }
      }
      if (nested) continue;
      if (pointerOnly.has(el)) {
        for (const kept of keep) {
          if (kept !== el && kept.contains(el)) { nested = true; break; }
        }
      } else {
        for (const other of wide) {
          if (other !== el && !pointerOnly.has(other) && el.contains(other)) {
            nested = true;
            break;
          }
        }
      }
      if (nested) continue;
      keep.push(el);
    }
    // Re-sort into document order so indices read down the page, not
    // strict-then-wide. compareDocumentPosition handles shadow boundaries.
    listed = strictOk.concat(keep).sort((a, b) => {
      const rel = a.compareDocumentPosition(b);
      if (rel & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (rel & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
  }

  const out = [];
  let idx = 0;
  let total = 0;
  const wideSet = new Set(wide);
  for (const el of listed) {
    const tag = el.tagName.toLowerCase();
    total++;
    idx++;
    el.setAttribute('data-jarvis-obs', obsId);
    el.setAttribute('data-jarvis-idx', String(base + idx));
    // A wide-tier hit renders as 'item', not its tag name: "[12] item 'Yonex
    // Astrox — Rs 8,499'" tells the model this is a card it can open, where
    // "[12] div" tells it nothing and spends the same budget.
    const role = wideSet.has(el) ? 'item' : roleOf(el);
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
        // SEARCH-SHAPED by a POSITIVE signal only (2026-07-22). The old test
        // called a form 'search' whenever it had no other inputs — which is
        // true of a contenteditable messenger (LinkedIn's message SEND form has
        // zero <input>s), so a send was misread as a harmless search and the
        // submit-gesture gate stood down. Now: an explicit search role/type, or
        // exactly one visible text control whose name/placeholder/label actually
        // says 'search'/'find'/'query'. A JS action form is never 'search'.
        const searchLabel = (
          (el.getAttribute('name') || '') + ' ' +
          (el.getAttribute('placeholder') || '') + ' ' +
          (el.getAttribute('aria-label') || '') + ' ' +
          (f.getAttribute('aria-label') || '')
        ).toLowerCase();
        const textControls = f.querySelectorAll(
          'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=checkbox]):not([type=radio]):not([type=file]),textarea'
        ).length;
        const searchTokened = /(^|[^a-z])search([^a-z]|$)|\\bfind\\b|(^|[^a-z])query([^a-z]|$)/.test(searchLabel);
        const isSearch = !!(el.closest('[role=search]')) ||
          f.getAttribute('role') === 'search' ||
          t === 'search' ||
          (el.getAttribute('role') || '') === 'searchbox' ||
          (textControls === 1 && searchTokened);
        form = {
          method: (f.getAttribute('method') || 'GET').toUpperCase(),
          submit: (tag === 'button' && (t === 'submit' || t === '')) ||
                  (tag === 'input' && (t === 'submit' || t === 'image')),
          search: !!isSearch
        };
      }
    } catch (e) {}
    out.push({
      index: base + idx,
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


def _elements_of(
    raw: dict,
    *,
    frame_id: str = "",
    frame_url: str = "",
    dx: float = 0.0,
    dy: float = 0.0,
) -> list[Element]:
    """The JS element payload → Element objects, with rects translated into
    TOP-PAGE viewport coordinates.

    The translation is mandatory, not cosmetic. A frame element's
    getBoundingClientRect() is relative to its OWN viewport, while
    overlay_marks() draws set-of-marks badges in top-page coordinates and
    resolve_point_to_index() maps vision points in the same space. Skipping dx/dy
    would put every frame element's badge in the wrong place and silently feed
    the vision model a mislabelled screenshot — a corruption with no error."""
    out = []
    for item in (raw.get("elements") or []):
        if not isinstance(item, dict):
            continue
        x, y, w, h = _rect_of(item.get("rect"))
        raw_name = str(item.get("name") or "")
        out.append(
            Element(
                index=int(item.get("index", 0)),
                role=str(item.get("role") or "element"),
                name=raw_name[:_NAME_MAX],
                name_full=raw_name[:_NAME_FULL_MAX],
                value=str(item.get("value") or "")[:_VALUE_MAX],
                href=str(item.get("href") or "")[:_HREF_MAX],
                rect=(x + dx, y + dy, w, h),
                frame_id=frame_id,
                frame_url=frame_url,
                **_form_of(item.get("form")),
            )
        )
    return out


async def _worthwhile_frames(page: Any) -> list[tuple[str, Any, dict]]:
    """Child frames big enough to hold real content, with their offsets.

    GEOMETRIC selection, deliberately not thinness-based: a substantial content
    frame on an otherwise-rich page (a checkout widget, a booking calendar, an
    embedded player's controls) must still be read. Tracking pixels, 1x1 beacons,
    about:blank stubs and ad slots below the area floor are all skipped.

    On an ordinary page this costs ZERO extra evaluate() round-trips, because no
    frame passes the filter. A page object without `frames` (every fake in the
    suite) yields [] and the caller takes exactly its pre-frame path."""
    frames = list(getattr(page, "frames", None) or [])
    if not frames:
        return []
    main = getattr(page, "main_frame", None)
    out: list[tuple[str, Any, dict]] = []
    for i, frame in enumerate(frames):
        if frame is main or len(out) >= _MAX_FRAMES:
            continue
        url = str(getattr(frame, "url", "") or "")
        if not url.startswith(("http://", "https://")):
            continue
        try:
            handle = await frame.frame_element()
            box = await handle.bounding_box()
        except Exception:
            continue
        if not box or (box.get("width", 0) * box.get("height", 0)) < _FRAME_MIN_AREA:
            continue
        out.append((f"{i}:{url[:200]}", frame, box))
    return out


def _merge_challenge(
    base: Optional[dict], frame_probe: Any, box: dict
) -> Optional[dict]:
    """Union a frame's challenge ZONES into the top-page probe, translated.

    ⚠️ NON-NEGOTIABLE, and the reason is a hole this phase would otherwise punch
    in the 15.4 never-touch rule. The top document's probe descends only into
    SAME-ORIGIN subframes (a cross-origin contentDocument throws), but Playwright's
    frame.evaluate works cross-origin — so the moment frames are walked, a CAPTCHA
    widget living in a cross-origin frame becomes listable and clickable by an
    agent that must never touch one. Its zones have to come up with it.

    `blocking` is deliberately NOT propagated: a challenge INSIDE a frame is an
    embedded widget from the page's point of view, and promoting it would make
    every page carrying a reCAPTCHA read as a full-page interstitial."""
    if not isinstance(frame_probe, dict):
        return base
    zones = frame_probe.get("zones")
    if not isinstance(zones, list) or not zones:
        return base
    dx = float(box.get("x", 0) or 0)
    dy = float(box.get("y", 0) or 0)
    moved = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        try:
            moved.append({
                "x": float(zone.get("x", 0) or 0) + dx,
                "y": float(zone.get("y", 0) or 0) + dy,
                "w": float(zone.get("w", 0) or 0),
                "h": float(zone.get("h", 0) or 0),
            })
        except (TypeError, ValueError):
            continue
    if not moved:
        return base
    merged = dict(base) if isinstance(base, dict) else {
        "kind": frame_probe.get("kind") or "CAPTCHA",
        "mode": "embedded",
        "blocking": False,
        "solved": frame_probe.get("solved", False),
    }
    merged["zones"] = list(merged.get("zones") or []) + moved
    return merged


async def observe(page: Any) -> Observation:
    """Snapshot one page. Best-effort about the prose (a page with no body text
    is normal), strict about the elements (they are what the loop acts on).

    Reads the top document, then any child FRAME big enough to hold real content
    (2026-07-26). Frames are where checkout forms, booking widgets and embedded
    players live; before this they were simply invisible, and a page whose whole
    purpose sat inside one read as empty."""
    observation_id = uuid.uuid4().hex[:12]
    raw = await page.evaluate(_EXTRACT_JS, {"obsId": observation_id, "base": 0})
    if not isinstance(raw, dict):
        raise RuntimeError(f"page observation returned {type(raw).__name__}, expected an object")

    elements = _elements_of(raw)
    total = int(raw.get("total") or len(elements))
    challenge = raw.get("challenge") if isinstance(raw.get("challenge"), dict) else None

    for frame_id, frame, box in await _worthwhile_frames(page):
        try:
            sub = await frame.evaluate(
                _EXTRACT_JS, {"obsId": observation_id, "base": total}
            )
        except Exception as exc:
            # A frame that navigated, or a cross-origin one that refuses — normal,
            # never fatal. The top document's observation stands on its own.
            logger.debug(f"frame observe skipped ({frame_id[:60]}): {type(exc).__name__}: {exc}")
            continue
        if not isinstance(sub, dict):
            continue
        got = _elements_of(
            sub,
            frame_id=frame_id,
            frame_url=str(getattr(frame, "url", "") or ""),
            dx=float(box.get("x", 0) or 0),
            dy=float(box.get("y", 0) or 0),
        )[:_FRAME_ELEMENT_CAP]
        elements.extend(got)
        total += len(got)
        challenge = _merge_challenge(challenge, sub.get("challenge"), box)

    # CAPTURE generously, RENDER tightly. `truncated` keeps its old meaning — it
    # is the RENDER marker ("… (truncated)" in the prompt), so it must still be
    # true whenever the prompt shows less than the page had.
    full = str(raw.get("text") or "").strip()[:_PAGE_TEXT_CAPTURE]
    truncated = len(full) > _PAGE_TEXT_BUDGET
    text = full[:_PAGE_TEXT_BUDGET] if truncated else full

    return Observation(
        observation_id=observation_id,
        url=str(raw.get("url") or page.url or ""),
        title=str(raw.get("title") or ""),
        elements=elements,
        element_total=total,
        page_text=text,
        text_truncated=truncated,
        text_full=full,
        viewport=_viewport_of(raw.get("viewport")),
        challenge=challenge,
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
    # The top document FIRST, always. Playwright's CSS engine pierces OPEN shadow
    # roots for free, so this arm covers the light DOM and shadow DOM together —
    # which is why the 2026-07-26 shadow work needed no change here at all. It is
    # also the only arm any fake page in the suite ever reaches.
    handle = await page.query_selector(selector)
    if handle is not None:
        return handle

    # Then the frame this index was STAMPED in (2026-07-26). Only that frame: an
    # index means one element in one document, and searching every frame for a
    # matching stamp would be exactly the "click whatever happens to be third"
    # failure the index contract exists to prevent. A frame that navigated away
    # has taken its attributes with it, so this resolves to nothing and the
    # staleness promise holds in precisely the same structural way.
    element = observation.index_map().get(int(index))
    frame_key = getattr(element, "frame_id", "") if element is not None else ""
    if frame_key:
        frame = _find_frame(page, frame_key)
        if frame is not None:
            try:
                handle = await frame.query_selector(selector)
            except Exception as exc:
                logger.debug(f"frame resolve failed: {type(exc).__name__}: {exc}")
                handle = None
            if handle is not None:
                return handle

    raise StaleObservation(
        f"Element [{index}] is no longer on the page — it changed since it "
        f"was read. Look at the page again before acting on it."
    )


def _find_frame(page: Any, frame_key: str) -> Any:
    """The frame a `frame_id` names, or None. Matched on URL first and ordinal
    only as a fallback: frames get re-ordered, and an ordinal that has drifted
    would hand back the wrong document — which is worse than an honest
    StaleObservation."""
    frames = list(getattr(page, "frames", None) or [])
    if not frames:
        return None
    ordinal, _, url = frame_key.partition(":")
    if url:
        for frame in frames:
            if str(getattr(frame, "url", "") or "")[:200] == url:
                return frame
    try:
        idx = int(ordinal)
    except (TypeError, ValueError):
        return None
    return frames[idx] if 0 <= idx < len(frames) else None


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


def _mark_font(size: int) -> Any:
    """A bitmap font for the badge numbers. Pillow ≥10.1 sizes its default font;
    older Pillow ignores the size. None when Pillow's font module is absent (the
    caller draws no text — the outline still marks the element). Never raises."""
    try:
        from PIL import ImageFont
    except Exception:
        return None
    try:
        return ImageFont.load_default(size=size)   # Pillow ≥ 10.1
    except TypeError:
        try:
            return ImageFont.load_default()
        except Exception:
            return None
    except Exception:
        return None


def overlay_marks(
    base_jpeg: bytes, observation: "Observation", skip_elements: int = 0
) -> Optional[bytes]:
    """Draw the set-of-marks overlay in PYTHON (Pillow) onto an already-captured
    base screenshot — instead of the in-page _MARK_JS / _UNMARK_JS round-trips —
    so the capture can run CONCURRENTLY with observe() (Phase 6 pipelining).
    Replicates the in-page badge exactly: a rose outline per element + a numbered
    badge at its top-left, the number being the element's index (the vision model
    answers with it, so it must match the text list).

    The element rects are CSS px (viewport coords); the base image is device px,
    already downscaled by capture_screenshot. ONE scale factor per axis
    (image_size / viewport_size) maps between them and absorbs BOTH the
    device-pixel-ratio and the downscale — which is exactly why
    Observation.viewport is carried. Returns None when it cannot draw (Pillow
    absent, unknown viewport, decode failure) so the caller falls back to the
    in-page path. Never raises."""
    vw, vh = observation.viewport
    if vw <= 0 or vh <= 0:
        return None
    try:
        import io

        from PIL import Image, ImageDraw
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(base_jpeg)) as opened:
            img = opened.convert("RGB")
    except Exception as exc:
        logger.debug(f"overlay_marks decode: {type(exc).__name__}: {exc}")
        return None
    try:
        iw, ih = img.size
        sx = iw / float(vw)
        sy = ih / float(vh)
        lo, hi = visible_span(observation, skip_elements)
        draw = ImageDraw.Draw(img)
        rose = (225, 29, 72)      # #e11d48 — the in-page outline/badge colour
        white = (255, 255, 255)
        outline_w = max(1, round(2 * sx))
        font = _mark_font(int(min(20, max(11, round(12 * sy)))))
        for e in observation.elements[lo:hi]:
            x, y, w, h = e.rect
            if w <= 0 or h <= 0:
                continue
            x0, y0 = x * sx, y * sy
            x1, y1 = (x + w) * sx, (y + h) * sy
            draw.rectangle([x0, y0, x1, y1], outline=rose, width=outline_w)
            label = str(e.index)
            try:
                left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
                tw, th = (right - left), (bottom - top)
            except Exception:
                tw, th = 7 * len(label), 12
            bx0 = max(0.0, x0 - 1)
            by0 = max(0.0, y0 - th - 3)
            draw.rectangle([bx0, by0, bx0 + tw + 4, by0 + th + 2], fill=rose)
            draw.text((bx0 + 2, by0 + 1), label, fill=white, font=font)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=_SCREENSHOT_JPEG_QUALITY)
        return buffer.getvalue()
    except Exception as exc:
        logger.debug(f"overlay_marks draw: {type(exc).__name__}: {exc}")
        return None


async def capture_marked(
    page: Any,
    observation: "Observation",
    skip_elements: int = 0,
    *,
    base_image: Optional[bytes] = None,
) -> Optional[bytes]:
    """A set-of-marks screenshot: numbered badges + outlines for the elements in
    the rendered window.

    PIPELINED PATH (Phase 6): when `base_image` is supplied — a base screenshot
    the loop captured CONCURRENTLY with observe() — the marks are drawn in Python
    (overlay_marks), skipping the in-page _MARK_JS / screenshot / _UNMARK_JS
    round-trips entirely. Falls back to the IN-PAGE path when no base is supplied,
    or when overlay_marks can't draw (Pillow absent / unknown viewport). Every
    stage is best-effort: a mark failure degrades to the plain screenshot, a
    capture failure to None (the caller then falls back to the text-only path)."""
    if base_image is not None:
        marked = overlay_marks(base_image, observation, skip_elements)
        if marked is not None:
            return marked
        # Pillow absent / undrawable → fall through to the in-page path, which
        # draws marks in the DOM (no viewport scaling needed) and re-captures.
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
    # OFF THE BROWSER LOOP. Pillow decode+resize+encode is synchronous CPU work,
    # and the browser runtime is ONE loop on ONE thread that also resumes every
    # request paused by the interceptor — so doing it inline stalls the page's
    # own network while we shrink a picture of it. A thread costs nothing here
    # (the GIL is released inside Pillow's C code).
    return await asyncio.to_thread(_downscale_jpeg, bytes(raw), max_dim)


def _downscale_jpeg(data: bytes, max_dim: int) -> bytes:
    """Shrink a JPEG so its longer edge ≤ max_dim, via a lazy Pillow import.
    Returns the original bytes unchanged when Pillow is absent or already small —
    the capture is best-effort, so a missing optional dep never fails it.
    Synchronous by design; callers run it in a thread (see capture_screenshot)."""
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

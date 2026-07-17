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

    def render(self) -> str:
        line = f'[{self.index}] {self.role} "{self.name}"' if self.name else f"[{self.index}] {self.role}"
        if self.value:
            line += f" = {self.value!r}"
        if self.href:
            line += f" → {self.href}"
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

    def index_map(self) -> dict[int, Element]:
        return {e.index: e for e in self.elements}


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
    out.push({
      index: idx,
      role: role,
      name: clip(nameOf(el), 120),
      value: value,
      href: clip(href, 100)
    });
  }

  return {
    url: location.href,
    title: document.title || '',
    elements: out,
    total: total,
    text: (document.body ? document.body.innerText : '') || ''
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
    )


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
def render(observation: Observation) -> str:
    """The observation as the LLM sees it. Elements first and within their own
    budget — prose can never crowd out the actionable half (see THE BUDGET)."""
    head = [f"URL: {observation.url}"]
    if observation.title:
        head.append(f"TITLE: {observation.title}")

    lines: list[str] = []
    used = 0
    shown = 0
    for element in observation.elements:
        line = element.render()
        if used + len(line) + 1 > _ELEMENT_BUDGET:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    if shown < observation.element_total:
        header = f"ELEMENTS ({shown} of {observation.element_total} shown):"
        footer = f"… {observation.element_total - shown} more elements not shown."
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

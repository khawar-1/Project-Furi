"""
Jarvis OS — the three pages behind the 2026-08-09 e-commerce report

WHY THIS EXISTS. Two live runs produced four defects, and planning them off
INVENTED page shapes went wrong twice in a row — which is the recorded rule
("a fake page is a claim about the live DOM — check it") caught in the act:

  * I scored an imagined "you may also like" rail against the real kameez PDP.
    Page 9, rails 4-7 -> suppression WOULD have fired. The live run asked
    anyway, so the real rail labels must score >= 9 and no invented fixture can
    say why.
  * I scored the string "Men Kameez Shalwar" at 5 on /pages/men-collections and
    called it a unique leader nothing was clicking. That is the score of a
    string I typed. The trace says the gate found a 2-WAY TIE, which a unique
    leader at 5 would have prevented outright.

So this measures the three pages before any of Phases 1-5 is written. Each page
answers ONE question the design cannot proceed without:

  /pages/men-collections   is there a section-named element at all? what does
                           it score, what is the runner-up, and WHICH TWO TIED?
  the kameez PDP           the candidates at the top score: are they
                           byte-identical? does either carry no href? does the
                           page tie with ITSELF?
  /search?q=janan          which DOM signal carries "sold out" HERE — the
                           element's own class, aria-disabled, or a badge on an
                           ancestor card? (observe.eligible() already drops
                           el.disabled, so that one cannot fire on a listed
                           element.)

Run from backend/:

    venv\\Scripts\\python -u scripts\\_measure_ecommerce_round.py

NEVER collected by pytest (real browser, real network). Read-only: it navigates
and reads. Nothing is ever clicked, filled or submitted.
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001
# A legacy cp1252 console cannot encode a label out of a real product page, and a
# probe that dies mid-report is a probe that reports nothing (the recorded rule).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"

# The two incidents, verbatim. INTENT is what the user typed; GOAL is the
# planner's paraphrase, which is what `_target_words` used to read before
# `_inject_user_words` (2026-08-07) and is kept here because the difference is
# still worth seeing on a real page.
SECTION_INTENT = (
    "go to junaidjamshed.com in the men kameez shalwar section "
    "add black plain sharwar kameez to cart"
)
SECTION_GOAL = (
    "On junaidjamshed.com, navigate to the Men Kameez Shalwar section, find the "
    "black plain shalwar kameez, and add it to the cart."
)
# What the user answered at 14:32:38, so the PDP is scored exactly as the
# resumed run scored it.
SECTION_CHOSEN = "BLACK COTTON CASUAL KAMEEZ SHALWAR"

JANAN_INTENT = "go to junaidjamshed.com and add janan in cart"

PAGES = [
    # (url, intent, chosen_target)
    ("https://www.junaidjamshed.com/pages/men-collections", SECTION_INTENT, ""),
    # ⚠️ The URL in backend.log is TRUNCATED by the log formatter (it ends on a
    # bare "-"), and fetching that literally returns a 404 whose title then
    # scores 9 against the goal — a measurement that looks plausible and means
    # nothing. This is the real href, read off the men page's own DOM above.
    (
        "https://www.junaidjamshed.com/collections/mens-stitched/products/"
        "black-cotton-casual-kameez-shalwar-jjkss60094",
        SECTION_INTENT,
        SECTION_CHOSEN,
    ),
    ("https://www.junaidjamshed.com/search?q=janan", JANAN_INTENT, ""),
]

# Per OBSERVED element, the raw DOM facts the availability flag would have to
# read. Keyed by the observation's own stamp so this cannot drift out of step
# with what the loop actually saw (observe.py:685 sets both attributes).
#
# Four signals, because the 2026-08-08 axis round MEASURED that one page uses
# two different ones, and cards are a third shape again:
#   own_disabled   el.disabled          — expected always false here, because
#                                         observe.eligible() drops those before
#                                         they are ever listed
#   aria           aria-disabled="true"
#   own_class      the element's own className
#   card_*         the nearest card-ish ancestor's class and text — where a
#                  Shopify "Sold out" badge actually lives
STOCK_PROBE_JS = r"""
(obsId) => {
  // "you cannot buy this one", every way a storefront says it. Note `disabled`
  // as a substring is what catches Shopify's `is-disabled`.
  const DEAD_RE = /sold[\s_-]*out|unavailable|out[\s_-]*of[\s_-]*stock|disabled/i;
  // Selectors a Shopify theme uses for the badge itself.
  const BADGE_SEL = '[class*=sold], [class*=badge], [data-sold-out], .price--sold-out';
  const out = [];
  document.querySelectorAll('[data-jarvis-obs="' + obsId + '"]').forEach((el) => {
    // ⚠️ THE FIRST CUT USED el.closest('li,[class*=item],...') AND MATCHED THE
    // NAV BAR: 'hdt-top-bar__item' contains "item". A card is not a class name,
    // it is the ancestor that actually holds this product's own controls — so
    // walk up and report every step, and let the caller see which is which.
    const chain = [];
    let node = el;
    for (let i = 0; i < 8 && node && node !== document.body; i++) {
      const cls = String(node.className || '');
      const txt = String(node.innerText || '').replace(/\s+/g, ' ').trim();
      let badge = '';
      try {
        const b = node.querySelector(BADGE_SEL);
        badge = b ? (String(b.className || '') + '|' + String(b.innerText || '').trim()) : '';
      } catch (e) { badge = ''; }
      chain.push({
        tag: node.tagName.toLowerCase(),
        cls: cls.slice(0, 120),
        cls_dead: DEAD_RE.test(cls),
        txt_dead: DEAD_RE.test(txt),
        txt: txt.slice(0, 160),
        badge: badge.slice(0, 120),
        // ⚠️ THE CARD BOUNDARY TOOK TWO MEASURED ATTEMPTS TO GET RIGHT.
        //   "nearest ancestor with a buy control" -> matched the whole GRID, so
        //     one product's SOLD OUT badge was attributed to all twenty.
        //   "largest ancestor with exactly ONE product LINK" -> stopped one
        //     level too early, at the info sub-block that holds only the title
        //     and price, because a card legitimately links the SAME product
        //     several times (image, title, "View product").
        // So: count DISTINCT product paths. The card is the largest ancestor
        // still about a single product.
        product_paths: (() => {
          try {
            const seen = {};
            node.querySelectorAll('a[href*="/products/"]').forEach((a) => {
              seen[(a.getAttribute('href') || '').split('?')[0]] = 1;
            });
            return Object.keys(seen).length;
          } catch (e) { return 99; }
        })(),
        has_buy: !!node.querySelector('button, [name=add], form[action*="cart/add"]'),
        buy_disabled: (() => {
          try {
            const b = node.querySelector('button, [name=add]');
            if (!b) return null;
            return !!b.disabled || DEAD_RE.test(String(b.className || '')) ||
                   DEAD_RE.test(String(b.innerText || ''));
          } catch (e) { return null; }
        })(),
      });
      node = node.parentElement;
    }
    out.push({
      idx: parseInt(el.getAttribute('data-jarvis-idx') || '-1', 10),
      tag: el.tagName.toLowerCase(),
      own_disabled: !!el.disabled,
      aria: el.getAttribute('aria-disabled'),
      own_class: String(el.className || '').slice(0, 120),
      chain: chain,
    });
  });
  return out;
}
"""


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


async def _look(session, url: str, intent: str, chosen: str) -> dict:
    from app.browser import choice
    from app.browser import extract as browser_extract
    from app.browser import observe as dom_observe

    await session.goto(url)
    await session.settle()
    obs = await dom_observe.observe(session.page)

    # EXACTLY what the loop computes: intent + the user's answer as `extra`
    # (loop.py:4024-4027). Scoring anything else would measure a different run.
    target = choice.target_tokens(intent, obs.url, extra=[chosen] if chosen else [])
    cands = choice.candidates_of(obs.elements, find_price=browser_extract.find_price)
    scored = sorted(
        ((choice._score(target, c.label), c) for c in cands),
        key=lambda pair: -pair[0],
    )
    hits = [(s, c) for s, c in scored if s > 0]
    top = hits[0][0] if hits else 0
    tied = [c for s, c in hits if s == top]
    runner = next((s for s, _ in hits if s < top), 0)

    subject = choice.page_subject(obs.title, obs.url)
    row: dict = {
        "url": obs.url,
        "title": obs.title,
        "intent": intent,
        "chosen_target": chosen,
        "elements": obs.element_total,
        "candidates": len(cands),
        "target_words": choice.plain_words(target),
        "page_subject": subject,
        "page_score": choice._score(target, subject),
        "top_score": top,
        "runner_up_score": runner,
        "tied": len(tied),
        # THE QUESTION FOR THE PDP: are the tied candidates the same string, and
        # does either lack an href? _dedupe_key is (label tokens, href), so two
        # identical labels with DIFFERENT hrefs are not merged — which would mean
        # the user was offered two byte-identical options.
        "tied_detail": [
            {
                "label": c.label,
                "index": c.index,
                "role": "",  # filled below from the element
                "href": c.href,
                "price": c.price,
            }
            for c in tied[:12]
        ],
        # THE QUESTION FOR THE MEN PAGE: the whole ranked head, so a
        # section-named element is either visibly there or visibly absent.
        "ranked": [
            {"score": s, "label": c.label, "href": c.href, "index": c.index}
            for s, c in hits[:25]
        ],
        "page_is_the_target": choice.page_is_the_target(target, obs.title, obs.url, tied),
        "page_covers_chosen": (
            choice.page_covers_target(choice.target_tokens(chosen), obs.title, obs.url)
            if chosen
            else None
        ),
    }

    by_index = obs.index_map()
    for entry in row["tied_detail"]:
        el = by_index.get(entry["index"])
        entry["role"] = str(getattr(el, "role", "") or "") if el is not None else "?"

    # ⚠️ The page's own title among the candidates is what makes the janan-leather
    # PDP suppress (test_browse_item_choice.py:289-295), so name it explicitly
    # rather than leaving it to be inferred from the labels.
    row["self_named_candidates"] = [
        {"label": c.label, "href": c.href, "role": ""}
        for c in tied
        if choice.page_covers_target(choice.target_tokens(c.label), obs.title, obs.url)
    ][:12]

    # THE QUESTION FOR THE SEARCH PAGE: which signal says "sold out" here.
    # Scoped to the TIED candidates — those are the ones the user was offered,
    # so they are the only ones whose availability the report is about.
    stock: list[dict] = []
    wanted = {c.index for c in tied}
    try:
        raw = await session.page.evaluate(STOCK_PROBE_JS, obs.observation_id)
        stock = [r for r in (raw or []) if int(r.get("idx", -1)) in wanted]
    except Exception as exc:  # noqa: BLE001 — a probe failure is a finding, not a crash
        row["stock_probe_error"] = f"{type(exc).__name__}: {exc}"

    def _card(r: dict) -> Optional[dict]:
        """The LARGEST ancestor still containing only this product's link.

        Measured: "the nearest ancestor with a buy control" walked all the way
        to the grid, so one product's SOLD OUT badge was attributed to all
        twenty. Widening while `product_links <= 1` stops exactly at the card."""
        best = None
        for i, link in enumerate(r.get("chain") or []):
            if int(link.get("product_paths") or 0) > 1:
                break
            best = {
                "depth": i,
                "tag": link["tag"],
                "cls": link["cls"],
                "badge": link["badge"],
                "txt": link["txt"],
                "cls_dead": bool(link.get("cls_dead")),
                "txt_dead": bool(link.get("txt_dead")),
                "buy_disabled": link.get("buy_disabled"),
            }
        return best

    def _dead(r: dict, card: Optional[dict]) -> str:
        """Which signal, if any, says this one cannot be bought."""
        if str(r.get("aria") or "") == "true":
            return "aria-disabled"
        if r.get("own_disabled"):
            return "el.disabled"
        if card:
            if card["cls_dead"]:
                return "card.class"
            if card["txt_dead"]:
                return "card.text"
            if "sold" in str(card.get("badge") or "").lower():
                return "card.badge"
            if card.get("buy_disabled"):
                return "card.buy_disabled"
        return ""

    by_idx = {c.index: c for c in tied}
    rows = []
    for r in stock:
        card = _card(r)
        rows.append(
            {
                "idx": r["idx"],
                "label": getattr(by_idx.get(r["idx"]), "label", ""),
                "dead_by": _dead(r, card),
                "own_class": r.get("own_class", ""),
                "aria": r.get("aria"),
                "card": card,
            }
        )
    row["stock_rows"] = rows
    row["stock_summary"] = {
        "tied_probed": len(rows),
        "dead": sum(1 for r in rows if r["dead_by"]),
        "by_signal": sorted({r["dead_by"] for r in rows if r["dead_by"]}),
    }
    return row


def _report(row: dict) -> None:
    print(f"\n=== {row['url']}")
    print(f"    title    {row['title']!r}")
    print(f"    said     {row['target_words']}")
    print(f"    subject  {row['page_subject']!r}  -> page scores {row['page_score']}")
    print(
        f"    top={row['top_score']} runner_up={row['runner_up_score']} "
        f"tied={row['tied']} candidates={row['candidates']} "
        f"page_is_the_target={row['page_is_the_target']} "
        f"page_covers_chosen={row['page_covers_chosen']}"
    )
    print("    -- ranked head --")
    for r in row["ranked"][:12]:
        print(f"      {r['score']:>2}  {r['label']!r:<58} {r['href'][:60]}")
    if row["tied_detail"]:
        print("    -- the tie --")
        for d in row["tied_detail"]:
            print(
                f"      [{d['index']:>3}] {d['role']:<9} {d['label']!r:<52} "
                f"href={d['href'][:52]!r}"
            )
        labels = [d["label"] for d in row["tied_detail"]]
        if len(set(labels)) < len(labels):
            print("      ⚠️ TWO TIED CANDIDATES CARRY THE SAME LABEL")
        if any(not d["href"] for d in row["tied_detail"]):
            print("      ⚠️ A TIED CANDIDATE HAS NO HREF (a self-href test cannot see it)")
    if row["self_named_candidates"]:
        print("    -- candidates that ARE this page --")
        for d in row["self_named_candidates"]:
            print(f"      {d['label']!r:<58} href={d['href'][:52]!r}")
    sig = row["stock_summary"]
    print(
        f"    -- stock over the {sig['tied_probed']} TIED candidates: "
        f"{sig['dead']} unbuyable, signals={sig['by_signal']} --"
    )
    for r in row["stock_rows"][:22]:
        card = r["card"] or {}
        mark = f"DEAD({r['dead_by']})" if r["dead_by"] else "buyable"
        print(f"      [{r['idx']:>3}] {mark:<26} {r['label'][:40]!r}")
        if card:
            print(
                f"            card d={card['depth']} {card['tag']} "
                f"cls={card['cls'][:44]!r} badge={card['badge'][:40]!r}"
            )
            print(f"            text={card['txt'][:110]!r}")
    if row.get("stock_probe_error"):
        print(f"      probe error: {row['stock_probe_error']}")


async def _main_async() -> int:
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    try:
        await run_browser(ensure_playwright_driver(), timeout=90)
        print("playwright driver warm")
    except Exception as exc:  # noqa: BLE001
        print(f"driver warm-up skipped: {type(exc).__name__}: {exc}")

    async def _work() -> list[dict]:
        session = await browser_session.BrowserSession.open({_host(PAGES[0][0])})
        rows: list[dict] = []
        try:
            for url, intent, chosen in PAGES:
                try:
                    row = await _look(session, url, intent, chosen)
                except Exception as exc:  # noqa: BLE001 — one dead page is not the run
                    print(f"\n=== {url}\n    FAILED: {type(exc).__name__}: {exc}")
                    rows.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                rows.append(row)
                _report(row)
        finally:
            await session.close()
        return rows

    rows = await run_browser(_work(), timeout=600)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "ecommerce-round.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def main() -> int:
    # THE SELECTOR LOOP production actually has — the browse_observe_profile.py
    # rule: a standalone asyncio.run() is Proactor on Windows, which hides the
    # whole Playwright-subprocess bug class.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        print(f"event loop: {type(loop).__name__}")
        code = loop.run_until_complete(_main_async())
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

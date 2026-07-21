"""The browser automation stack — one real Chromium plus the agent machinery
that drives it.

Layout (built up over the refactor phases; see the module docstrings):

    runtime.py     — the dedicated Proactor event-loop thread every Playwright
                     touch marshals onto (Windows subprocess constraint)
    session.py     — BrowserSession lifecycle, network interception, and the
                     held-session registries (being carved into net/profile/
                     registry modules in later refactor phases)
    observe.py     — DOM observation: numbered element lists + the index/obs-id
                     staleness contract, screenshots, challenge probe
    loop.py        — the observe→decide→act agent loop
    commit_flow.py — commit-mode discovery/perform/multi-commit resume
    grounding.py   — the exfiltration bound: origins/uploads/fills must trace
                     to the user's own words, never page content

IMPORT COMPATIBILITY: the old module paths (app.core.browser_session,
app.core.dom_observe, app.core.browser_runtime, app.agents.browser_loop,
app.agents.browser_commit, app.agents.browser_grounding) are shims that alias
themselves to these modules via sys.modules replacement, so existing imports
AND test monkeypatches keep working unchanged until the importers migrate.

This package deliberately has no import-time side effects: importing
app.browser must never start the browser thread or touch Playwright.
"""

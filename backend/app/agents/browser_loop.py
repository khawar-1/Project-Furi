"""Compatibility shim — the implementation moved to app.browser.loop.

sys.modules self-replacement: this module PATH resolves to the implementation
module itself, so existing imports and test monkeypatches (run_browse,
BROWSE_DECISION_TIMEOUT_SECONDS, ...) keep working unchanged. Deleted in the
refactor's final importer-migration phase.
"""
import sys

from app.browser import loop as _impl

sys.modules[__name__] = _impl

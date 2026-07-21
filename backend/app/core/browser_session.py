"""Compatibility shim — the implementation moved to app.browser.session.

sys.modules self-replacement: this module PATH resolves to the implementation
module itself, so existing imports and test monkeypatches (BROWSER_FACTORY,
_PROFILE_REAPER, CLEAN_BROWSER_LAUNCHER, the registry globals, ...) keep
working unchanged. Deleted in the refactor's final importer-migration phase.
"""
import sys

from app.browser import session as _impl

sys.modules[__name__] = _impl

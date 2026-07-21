"""Compatibility shim — the implementation moved to app.browser.commit_flow.

sys.modules self-replacement: this module PATH resolves to the implementation
module itself, so existing imports and test monkeypatches (discover, perform,
COMMIT_PARAM, ...) keep working unchanged. Deleted in the refactor's final
importer-migration phase.
"""
import sys

from app.browser import commit_flow as _impl

sys.modules[__name__] = _impl

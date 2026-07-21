"""Compatibility shim — the implementation moved to app.browser.runtime.

sys.modules self-replacement: after this executes, this module PATH resolves to
the implementation module itself, so `import app.core.browser_runtime`,
`from app.core.browser_runtime import run_browser`, and test monkeypatches on
this path all operate on the real module. Deleted in the refactor's final
importer-migration phase.
"""
import sys

from app.browser import runtime as _impl

sys.modules[__name__] = _impl

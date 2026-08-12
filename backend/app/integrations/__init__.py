"""
Furi OS — External integrations (Phase 5).

One self-contained module per provider; nothing outside this package ever
touches provider credentials directly.
"""
from app.integrations.google_auth import (  # noqa: F401
    GoogleAuthManager,
    GoogleNotConnectedError,
    auth_manager,
)

"""
Jarvis OS — Google service factories (Phase 5, Part 1)

The ONE way business code gets a Gmail or Calendar client. Module-level
factory indirection follows the SESSION_FACTORY pattern (memory_tools /
task_runner): tests assign GMAIL_SERVICE_FACTORY / CALENDAR_SERVICE_FACTORY
and the entire suite runs against fakes — no test ever touches the real
Google API.

Real builds go through auth_manager().get_credentials(), so "not connected"
surfaces as GoogleNotConnectedError here too — callers (email/calendar
tools, the daily briefing) catch it and degrade clean, never crash.
"""
import asyncio
import inspect
from typing import Any, Callable, Optional

from app.integrations.google_auth import auth_manager

# Zero-arg callables (sync or async) returning a service object. None = real.
GMAIL_SERVICE_FACTORY: Optional[Callable[[], Any]] = None
CALENDAR_SERVICE_FACTORY: Optional[Callable[[], Any]] = None


async def _from_factory(factory: Callable[[], Any]) -> Any:
    service = factory()
    if inspect.isawaitable(service):
        service = await service
    return service


def _build_service(api: str, version: str, creds: Any) -> Any:
    from googleapiclient.discovery import build

    # cache_discovery=False: the discovery doc ships with the client library;
    # the legacy file cache is noisy and unnecessary.
    return build(api, version, credentials=creds, cache_discovery=False)


async def get_gmail_service() -> Any:
    """Gmail v1 client, or GoogleNotConnectedError."""
    if GMAIL_SERVICE_FACTORY is not None:
        return await _from_factory(GMAIL_SERVICE_FACTORY)
    creds = await auth_manager().get_credentials()
    return await asyncio.to_thread(_build_service, "gmail", "v1", creds)


async def get_calendar_service() -> Any:
    """Calendar v3 client, or GoogleNotConnectedError."""
    if CALENDAR_SERVICE_FACTORY is not None:
        return await _from_factory(CALENDAR_SERVICE_FACTORY)
    creds = await auth_manager().get_credentials()
    return await asyncio.to_thread(_build_service, "calendar", "v3", creds)

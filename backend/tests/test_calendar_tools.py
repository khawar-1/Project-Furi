"""
Phase 5 Part 4 — CalendarTool suite + the event-id lock.

The suite never touches the real Calendar API: CALENDAR_SERVICE_FACTORY is
pointed at a chained-call fake that records every request the tools build, so
tests assert the EXACT body / query that would leave the machine (RFC3339
offsets, inclusive-vs-exclusive date bounds, patch-not-replace, sendUpdates).

The event-id lock mirrors the Part 3 recipient lock:
  1. tool-level:    times are ISO-only (non-ISO refused before any API call);
                    update/delete need an event_id.
  2. planner-level: _event_id_violation rejects an update/delete event_id not
                    returned by a read step in THIS plan.
  3. approval-level: _step_action_detail renders the full field contract, and
                    _enrich_event_action_detail names the real event.
And placeholder_resolver fills a PENDING event_id from a read that pins one
event — the calendar mirror of the recipient fill.
"""
import pytest

import app.tools  # noqa: F401 — registers every tool
from app.agents import placeholder_resolver
from app.agents.planner import (
    _completed_events,
    _enrich_event_action_detail,
    _event_id_grounding,
    _event_id_violation,
    _step_action_detail,
)
from app.agents.rendering import _RESULT_FORMATTERS
from app.agents.schemas import AgentPlan, PlanStep, StepStatus
from app.core.base_tool import PermissionLevel, ToolResult
from app.integrations import google_services
from app.integrations.google_auth import GoogleNotConnectedError
from app.tools.calendar_tools import (
    LIST_DEFAULT_RESULTS,
    LIST_MAX_RESULTS,
    format_event_when,
)
from app.tools.registry import execute_tool, registry


# --------------------------------------------------------- fake Calendar API

class _Request:
    def __init__(self, response):
        self._response = response

    def execute(self):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _Bound:
    """events() surface: any method becomes a recorded call keyed
    'events.<method>'."""

    def __init__(self, fake, prefix):
        self._fake, self._prefix = fake, prefix

    def __getattr__(self, name):
        def call(**kwargs):
            return self._fake._respond(f"{self._prefix}.{name}", kwargs)
        return call


class FakeCalendar:
    """Chained-call fake mirroring googleapiclient's calendar surface. Records
    (method, kwargs); responses set per method — a list is consumed one per
    call."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict = {}

    def _respond(self, method, kwargs):
        self.calls.append((method, kwargs))
        response = self.responses.get(method)
        if isinstance(response, list):
            response = response.pop(0)
        return _Request(response if response is not None else {})

    def events(self):
        return _Bound(self, "events")

    def sent(self, method):
        return [kwargs for m, kwargs in self.calls if m == method]


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(google_services, "CALENDAR_SERVICE_FACTORY", lambda: fake)
    return fake


def _event(eid="e1", summary="Standup", start="2026-07-14T10:00:00+05:00",
           end="2026-07-14T10:30:00+05:00", location="Room 3"):
    """A RAW Google event, as an events.list `items` entry (the tool turns it
    into a row via _event_row)."""
    return {
        "id": eid, "summary": summary,
        "start": {"dateTime": start}, "end": {"dateTime": end},
        "location": location, "htmlLink": f"https://cal/{eid}",
    }


def _row(eid="e1", summary="Standup", start="2026-07-14T10:00:00+05:00",
         end="2026-07-14T10:30:00+05:00", all_day=False, location="Room 3"):
    """An event ROW, as a completed list_events/find_events step returns it
    (start/end are strings — what the grounding / enrichment / fill helpers
    consume)."""
    return {
        "id": eid, "summary": summary, "start": start, "end": end,
        "all_day": all_day, "location": location, "link": f"https://cal/{eid}",
    }


# ----------------------------------------------------------------- read tools

async def test_list_events_returns_rows_and_defaults_to_upcoming(cal):
    cal.responses["events.list"] = {"items": [_event()]}
    result = await registry.get("list_events").execute()
    assert result.success is True
    row = result.output["events"][0]
    assert row["id"] == "e1" and row["summary"] == "Standup"
    assert row["all_day"] is False and row["location"] == "Room 3"
    sent = cal.sent("events.list")[0]
    assert sent["calendarId"] == "primary"
    assert sent["singleEvents"] is True and sent["orderBy"] == "startTime"
    # Criterion-less is valid: a default timeMin (now) makes it "upcoming".
    assert "timeMin" in sent
    assert sent["maxResults"] == LIST_DEFAULT_RESULTS


async def test_list_events_caps_max_results(cal):
    cal.responses["events.list"] = {"items": []}
    await registry.get("list_events").execute(max_results=999)
    assert cal.sent("events.list")[0]["maxResults"] == LIST_MAX_RESULTS


async def test_list_events_inclusive_end_date_shifts_one_day(cal):
    cal.responses["events.list"] = {"items": []}
    await registry.get("list_events").execute(time_min="2026-07-01", time_max="2026-07-31")
    sent = cal.sent("events.list")[0]
    # A bare end DATE is inclusive of the whole day → Google's exclusive
    # timeMax is the NEXT day.
    assert sent["timeMax"].startswith("2026-08-01")
    assert sent["timeMin"].startswith("2026-07-01")


async def test_list_events_refuses_non_iso_bound(cal):
    result = await registry.get("list_events").execute(time_min="3pm")
    assert result.success is False
    assert "ISO" in result.error
    assert cal.calls == []  # refused before any API call


async def test_find_events_passes_query_and_requires_text(cal):
    cal.responses["events.list"] = {"items": [_event()]}
    result = await registry.get("find_events").execute(text="standup")
    assert result.success is True
    assert cal.sent("events.list")[0]["q"] == "standup"
    # Missing text fails clean, no API call.
    empty = await registry.get("find_events").execute(text="  ")
    assert empty.success is False
    assert len(cal.calls) == 1


# ---------------------------------------------------------------- create_event

async def test_create_event_builds_rfc3339_with_local_offset(cal):
    cal.responses["events.insert"] = {"id": "new1", "htmlLink": "https://cal/new1"}
    result = await registry.get("create_event").execute(
        summary="Sync", start="2026-07-14T10:00", location="HQ",
    )
    assert result.success is True and result.output["id"] == "new1"
    body = cal.sent("events.insert")[0]["body"]
    # Naive local → RFC3339 with the machine's UTC offset; a missing end
    # defaults to +1 hour.
    assert body["start"]["dateTime"].startswith("2026-07-14T10:00:00")
    assert "+" in body["start"]["dateTime"] or "-" in body["start"]["dateTime"][11:]
    assert body["end"]["dateTime"].startswith("2026-07-14T11:00:00")
    assert body["location"] == "HQ"
    # No attendees are ever set; invitations are suppressed.
    assert "attendees" not in body
    assert cal.sent("events.insert")[0]["sendUpdates"] == "none"


async def test_create_all_day_event_uses_exclusive_end(cal):
    cal.responses["events.insert"] = {"id": "d1"}
    await registry.get("create_event").execute(summary="Holiday", start="2026-07-14")
    body = cal.sent("events.insert")[0]["body"]
    assert body["start"] == {"date": "2026-07-14"}
    # Google's all-day end is exclusive: a one-day event ends the NEXT day.
    assert body["end"] == {"date": "2026-07-15"}


async def test_create_event_requires_summary_and_start(cal):
    assert (await registry.get("create_event").execute(start="2026-07-14T10:00")).success is False
    assert (await registry.get("create_event").execute(summary="X")).success is False
    assert cal.calls == []


async def test_create_event_refuses_ambiguous_time(cal):
    result = await registry.get("create_event").execute(summary="X", start="03/04/2026")
    assert result.success is False
    assert "ISO" in result.error
    assert cal.calls == []


# ---------------------------------------------------------------- update_event

async def test_update_event_patches_only_given_fields(cal):
    cal.responses["events.patch"] = {"id": "e1", "summary": "Renamed"}
    result = await registry.get("update_event").execute(event_id="e1", summary="Renamed")
    assert result.success is True
    sent = cal.sent("events.patch")[0]
    assert sent["eventId"] == "e1"
    assert sent["body"] == {"summary": "Renamed"}  # patch, never a full replace
    assert sent["sendUpdates"] == "none"


async def test_update_event_requires_a_field_to_change(cal):
    result = await registry.get("update_event").execute(event_id="e1")
    assert result.success is False
    assert "nothing to update" in result.error
    assert cal.calls == []


async def test_update_event_requires_event_id(cal):
    result = await registry.get("update_event").execute(summary="X")
    assert result.success is False
    assert cal.calls == []


# ---------------------------------------------------------------- delete_event

async def test_delete_event_calls_delete_with_send_updates_none(cal):
    cal.responses["events.delete"] = {}
    result = await registry.get("delete_event").execute(event_id="e1")
    assert result.success is True and result.output["deleted"] == "e1"
    sent = cal.sent("events.delete")[0]
    assert sent["eventId"] == "e1" and sent["sendUpdates"] == "none"


async def test_delete_event_requires_event_id(cal):
    result = await registry.get("delete_event").execute(event_id="   ")
    assert result.success is False
    assert cal.calls == []


# ------------------------------------------------ degradation + error handling

async def test_not_connected_degrades_clean_on_every_tool(monkeypatch):
    def raise_not_connected():
        raise GoogleNotConnectedError()
    monkeypatch.setattr(google_services, "CALENDAR_SERVICE_FACTORY", raise_not_connected)
    for name, params in (
        ("list_events", {}),
        ("find_events", {"text": "x"}),
        ("create_event", {"summary": "s", "start": "2026-07-14T10:00"}),
        ("update_event", {"event_id": "e1", "summary": "s"}),
        ("delete_event", {"event_id": "e1"}),
    ):
        result = await registry.get(name).execute(**params)
        assert result.success is False, name
        assert "not connected" in result.error.lower(), name


async def test_calendar_api_errors_become_clean_failures(cal):
    cal.responses["events.list"] = RuntimeError("boom")
    result = await registry.get("list_events").execute()
    assert result.success is False
    assert "Calendar API error" in result.error


# ------------------------------------------------- permissions + approval gate

def test_permission_levels_match_the_contract():
    levels = {
        "list_events": PermissionLevel.READ,
        "find_events": PermissionLevel.READ,
        "create_event": PermissionLevel.WRITE,
        "update_event": PermissionLevel.WRITE,
        "delete_event": PermissionLevel.DESTRUCTIVE,
    }
    for name, level in levels.items():
        assert registry.get(name).permission_level == level, name


async def test_delete_event_never_runs_unapproved(cal, db_session):
    result = await execute_tool(
        "delete_event", {"event_id": "e1"}, db_session, approved=False,
    )
    assert result.success is False
    assert cal.calls == []  # the structural gate blocked it before the tool ran


async def test_create_event_never_runs_unapproved(cal, db_session):
    result = await execute_tool(
        "create_event", {"summary": "s", "start": "2026-07-14T10:00"},
        db_session, approved=False,
    )
    assert result.success is False
    assert cal.calls == []


# ------------------------------------------------------- planner: action detail

def test_action_detail_carries_the_full_create_contract():
    detail = _step_action_detail("create_event", {
        "summary": "Team sync", "start": "2026-07-14T10:00",
        "end": "2026-07-14T11:00", "location": "HQ", "description": "weekly",
    })
    assert "Team sync" in detail
    assert "start: 2026-07-14T10:00" in detail
    assert "end: 2026-07-14T11:00" in detail
    assert "location: HQ" in detail


def test_action_detail_update_and_delete_name_the_id():
    upd = _step_action_detail("update_event", {"event_id": "e1", "summary": "New"})
    assert "e1" in upd and "summary: New" in upd
    dele = _step_action_detail("delete_event", {"event_id": "e1"})
    assert "e1" in dele


def test_enrich_action_detail_names_the_real_event():
    plan = AgentPlan(goal="delete the standup", steps=[
        _read_events_step([_row(eid="e1", summary="Standup")]),
        PlanStep(
            description="Delete the standup", tool="delete_event",
            parameters={"event_id": "e1"},
            permission_level=PermissionLevel.DESTRUCTIVE, requires_approval=True,
            action_detail=_step_action_detail("delete_event", {"event_id": "e1"}),
        ),
    ])
    _enrich_event_action_detail(plan, plan.steps[1])
    assert "Standup" in plan.steps[1].action_detail
    assert "2026-07-14 10:00" in plan.steps[1].action_detail


# --------------------------------------------------- planner: event-id lock

def _read_events_step(events, tool="find_events") -> PlanStep:
    return PlanStep(
        description="Find events", tool=tool, parameters={"text": "standup"},
        permission_level=PermissionLevel.READ, requires_approval=False,
        status=StepStatus.COMPLETED,
        result=ToolResult(success=True, output={"events": events, "count": len(events)}),
    )


def _delete_step(event_id="e1", tool="delete_event") -> PlanStep:
    return PlanStep(
        description=f"Delete event {event_id}", tool=tool,
        parameters={"event_id": event_id},
        permission_level=PermissionLevel.DESTRUCTIVE, requires_approval=True,
    )


def test_event_id_guard_rejects_a_concrete_id_at_draft_time():
    # Nothing has run yet → any concrete id is ungrounded.
    feedback = _event_id_violation([_delete_step("hallucinated")], set())
    assert feedback is not None
    assert "hallucinated" in feedback
    assert "list_events" in feedback or "find_events" in feedback


def test_event_id_guard_accepts_an_id_from_a_completed_read():
    plan = AgentPlan(goal="delete standup", steps=[_read_events_step([_row("e1")])])
    grounding = _event_id_grounding(plan)
    assert _event_id_violation([_delete_step("e1")], grounding) is None


def test_event_id_guard_skips_placeholders_and_other_tools():
    assert _event_id_violation([_delete_step("PENDING: which event")], set()) is None
    upd = PlanStep(
        description="update", tool="update_event",
        parameters={"event_id": "PENDING: the standup", "summary": "x"},
        permission_level=PermissionLevel.WRITE, requires_approval=True,
    )
    assert _event_id_violation([upd], set()) is None


def test_completed_events_only_reads_from_calendar_read_steps():
    plan = AgentPlan(goal="x", steps=[_read_events_step([_row("e1"), _row("e2")])])
    ids = {e["id"] for e in _completed_events(plan)}
    assert ids == {"e1", "e2"}


# ------------------------------------------ placeholder resolver: event-id fill

def _pending_delete(text="PENDING: the standup event", tool="delete_event") -> PlanStep:
    return PlanStep(
        description="Delete the standup", tool=tool,
        parameters={"event_id": text},
        permission_level=PermissionLevel.DESTRUCTIVE, requires_approval=True,
    )


def test_pending_event_id_fills_from_the_named_event():
    plan = AgentPlan(goal="delete the standup", steps=[
        _read_events_step([_row("e1", "Standup"), _row("e2", "Lunch")]),
        _pending_delete("PENDING: the standup event"),
    ])
    out = placeholder_resolver.resolve(plan, 1, max_new=28)
    assert out is not None and len(out) == 1
    assert out[0].parameters["event_id"] == "e1"
    # Regenerated action_detail + description name the real event for approval.
    assert "Standup" in out[0].description


def test_pending_event_id_fills_when_only_one_event_found():
    plan = AgentPlan(goal="delete it", steps=[
        _read_events_step([_row("only1", "Whatever")]),
        _pending_delete("PENDING: the event"),
    ])
    out = placeholder_resolver.resolve(plan, 1, max_new=28)
    assert out is not None and out[0].parameters["event_id"] == "only1"


def test_pending_event_id_never_picks_between_matches():
    plan = AgentPlan(goal="delete standup", steps=[
        _read_events_step([_row("e1", "Standup AM"), _row("e2", "Standup PM")]),
        _pending_delete("PENDING: the standup event"),
    ])
    # Two events whose summaries match "standup" — code never picks.
    assert placeholder_resolver.resolve(plan, 1, max_new=28) is None


def test_pending_event_id_fresh_signature():
    template = _pending_delete()
    plan = AgentPlan(goal="delete standup", steps=[
        _read_events_step([_row("e1", "Standup")]), template,
    ])
    out = placeholder_resolver.resolve(plan, 1, max_new=28)
    assert out is not None
    assert out[0].signature() != template.signature()  # user approves the real id


# ------------------------------------------------------------ result rendering

def test_fmt_calendar_events_lists_summary_time_and_location():
    text = _RESULT_FORMATTERS["list_events"]({
        "events": [{
            "summary": "Standup", "start": "2026-07-14T10:00:00+05:00",
            "end": "2026-07-14T10:30:00+05:00", "all_day": False, "location": "Room 3",
        }],
        "count": 1,
    })
    assert "Standup" in text
    assert "2026-07-14 10:00" in text
    assert "Room 3" in text


def test_fmt_calendar_events_handles_empty():
    assert "No events" in _RESULT_FORMATTERS["find_events"]({"events": [], "count": 0})


def test_format_event_when_all_day_shows_date_only():
    assert format_event_when(
        {"start": "2026-07-14", "end": "2026-07-15", "all_day": True}
    ) == "2026-07-14"

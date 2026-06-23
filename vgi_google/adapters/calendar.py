"""Calendar adapter — ``events.list`` rows (Google Calendar API v3).

Maps calendar events into clean rows. Pagination is Calendar's native
``pageToken``/``nextPageToken`` carried as scan state.

All-day events use a ``date`` (no time); timed events use a ``dateTime``. We
expose both the parsed UTC ``start_time``/``end_time`` (NULL for all-day events,
which have no instant) and keep the raw start/end objects in ``extra`` so no
information is lost.

Required OAuth scope (read-only): ``calendar.readonly`` (or the narrower
``calendar.events.readonly``). The service account must be granted access to the
calendar (share it with the service-account e-mail, or use domain-wide
delegation for a Workspace user's ``primary`` calendar).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from ..discovery import execute, resolve_method
from ..schema_utils import (
    TIMESTAMPTZ,
    afield,
    json_dumps,
    json_field,
    parse_timestamp,
)
from .base import Page

SCHEMA: pa.Schema = pa.schema(
    [
        afield("id", pa.string(), "Event ID.", nullable=False),
        afield("summary", pa.string(), "Event title/summary."),
        afield("description", pa.string(), "Event description / notes."),
        afield("location", pa.string(), "Free-text event location."),
        afield("status", pa.string(), "Event status ('confirmed', 'tentative', 'cancelled')."),
        afield("start_time", TIMESTAMPTZ, "Start as a UTC timestamp; NULL for all-day events."),
        afield("end_time", TIMESTAMPTZ, "End as a UTC timestamp; NULL for all-day events."),
        afield("all_day", pa.bool_(), "True when the event is an all-day (date-only) event."),
        afield("organizer", pa.string(), "Organizer e-mail, when present."),
        afield("creator", pa.string(), "Creator e-mail, when present."),
        afield("html_link", pa.string(), "A link to the event in the Calendar UI."),
        json_field("extra", "Raw start/end objects, attendees and recurrence, JSON-encoded."),
    ]
)


class CalendarAdapter:
    """List calendar events as rows via ``events.list``."""

    api = "calendar"
    version = "v3"
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    schema = SCHEMA

    def fetch_page(self, service: Any, args: Any, cursor: str | None) -> Page:
        """Fetch one page of rows from the API for this adapter."""
        params: dict[str, Any] = {
            "calendarId": getattr(args, "calendar_id", "primary"),
            "maxResults": max(1, min(getattr(args, "page_size", 0) or 250, 2500)),
            "singleEvents": getattr(args, "single_events", True),
        }
        # orderBy=startTime is only valid with singleEvents=True.
        if params["singleEvents"]:
            params["orderBy"] = "startTime"
        if getattr(args, "time_min", None):
            params["timeMin"] = _to_rfc3339(args.time_min)
        if getattr(args, "time_max", None):
            params["timeMax"] = _to_rfc3339(args.time_max)
        if getattr(args, "query", None):
            params["q"] = args.query
        if cursor:
            params["pageToken"] = cursor

        method = resolve_method(service, "events.list")
        payload = execute(method(**params))

        rows = [self._map(e) for e in payload.get("items", [])]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))

    @staticmethod
    def _map(e: dict[str, Any]) -> dict[str, Any]:
        start = e.get("start") or {}
        end = e.get("end") or {}
        all_day = "date" in start and "dateTime" not in start

        start_time = parse_timestamp(start.get("dateTime")) if not all_day else None
        end_time = parse_timestamp(end.get("dateTime")) if not all_day else None

        extra: dict[str, Any] = {}
        if start:
            extra["start"] = start
        if end:
            extra["end"] = end
        if e.get("attendees"):
            extra["attendees"] = e["attendees"]
        if e.get("recurrence"):
            extra["recurrence"] = e["recurrence"]

        return {
            "id": e.get("id"),
            "summary": e.get("summary"),
            "description": e.get("description"),
            "location": e.get("location"),
            "status": e.get("status"),
            "start_time": start_time,
            "end_time": end_time,
            "all_day": all_day,
            "organizer": (e.get("organizer") or {}).get("email"),
            "creator": (e.get("creator") or {}).get("email"),
            "html_link": e.get("htmlLink"),
            "extra": json_dumps(extra),
        }


def _to_rfc3339(value: str) -> str:
    """Accept a date or datetime string; pad a bare date to an RFC-3339 instant.

    Calendar's ``timeMin``/``timeMax`` require an RFC-3339 timestamp. A bare
    ``YYYY-MM-DD`` is promoted to midnight UTC so callers can pass plain dates.
    """
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text}T00:00:00Z"
    return text

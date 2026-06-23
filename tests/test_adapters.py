"""Fixture parser unit tests: raw Google response -> typed adapter rows.

Each adapter's ``fetch_page`` is driven with a stubbed discovery client that
returns a captured, representative API response, and we assert the mapping into
the adapter's clean schema: the flat typed columns, LIST<VARCHAR> columns, the
JSON ``extra`` payload, TIMESTAMPTZ parsing, and — importantly — that missing
fields become ``None`` (SQL NULL) rather than raising.

No network, no credentials: the ``_StubService`` mimics just enough of the
discovery client surface (``files().list(...).execute()``) to feed each adapter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from vgi_google.adapters.calendar import CalendarAdapter
from vgi_google.adapters.drive import DriveAdapter
from vgi_google.adapters.sheets import SheetsAdapter
from vgi_google.adapters.youtube import YouTubeAdapter


class _Req:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self, num_retries: int = 0) -> Any:
        return self._payload


class _Resource:
    """A discovery resource whose every method returns a canned payload.

    ``responses`` maps a method name to either a payload or a callable
    ``(**kwargs) -> payload`` so a stub can vary by request parameters (used to
    serve YouTube's two-call search + videos flow).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses

    def __getattr__(self, name: str) -> Any:
        if name not in self._responses:
            raise AttributeError(name)
        resp = self._responses[name]

        def call(**kwargs: Any) -> _Req:
            payload = resp(**kwargs) if callable(resp) else resp
            return _Req(payload)

        return call


class _StubService:
    """A discovery service exposing nested resources from a spec dict."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec

    def __getattr__(self, name: str) -> Any:
        node = self._spec[name]

        def factory() -> Any:
            if isinstance(node, dict) and all(not callable(v) and isinstance(v, dict) for v in node.values()):
                # Could be a nested resource OR a methods map; disambiguate by
                # checking whether values look like sub-resources.
                if all(isinstance(v, dict) and "__methods__" in v for v in node.values()):
                    return _StubService({k: v["__methods__"] for k, v in node.items()})
            return _Resource(node)

        return factory


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_drive_maps_fields_and_missing_to_none() -> None:
    payload = {
        "files": [
            {
                "id": "f1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-04T12:30:00Z",
                "createdTime": "2026-01-01T00:00:00Z",
                "size": "20480",
                "webViewLink": "https://drive/f1",
                "parents": ["folderA"],
                "owners": [{"displayName": "Ada", "emailAddress": "ada@example.com"}],
                "trashed": False,
                "starred": True,
            },
            {"id": "f2"},  # everything else missing -> NULL
        ],
        "nextPageToken": "TOK",
    }
    service = _StubService({"files": {"list": payload}})
    page = DriveAdapter().fetch_page(service, _Args(query="q", page_size=10), None)

    assert page.next_cursor == "TOK"
    assert [r["id"] for r in page.rows] == ["f1", "f2"]

    first = page.rows[0]
    assert first["name"] == "Report.pdf"
    assert first["mime_type"] == "application/pdf"
    assert first["modified_time"] == datetime(2026, 3, 4, 12, 30, tzinfo=UTC)
    assert first["size"] == 20480
    assert first["parents"] == ["folderA"]
    assert first["owner"] == "Ada"
    assert json.loads(first["extra"])["owner_email"] == "ada@example.com"

    second = page.rows[1]
    assert second["name"] is None
    assert second["size"] is None
    assert second["parents"] is None
    assert second["owner"] is None
    assert second["extra"] is None  # empty extra -> NULL


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_timed_and_all_day_events() -> None:
    payload = {
        "items": [
            {
                "id": "e1",
                "summary": "Standup",
                "status": "confirmed",
                "start": {"dateTime": "2026-04-02T09:00:00Z"},
                "end": {"dateTime": "2026-04-02T09:15:00Z"},
                "organizer": {"email": "org@example.com"},
            },
            {
                "id": "e2",
                "summary": "Holiday",
                "start": {"date": "2026-04-03"},
                "end": {"date": "2026-04-04"},
            },
        ],
        "nextPageToken": "C2",
    }
    service = _StubService({"events": {"list": payload}})
    page = CalendarAdapter().fetch_page(service, _Args(calendar_id="primary", page_size=10), None)

    assert page.next_cursor == "C2"
    timed, all_day = page.rows
    assert timed["start_time"] == datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
    assert timed["all_day"] is False
    assert timed["organizer"] == "org@example.com"
    # All-day events have no instant -> NULL start/end, all_day True.
    assert all_day["start_time"] is None
    assert all_day["end_time"] is None
    assert all_day["all_day"] is True
    # The raw start/end objects are preserved in extra.
    assert json.loads(all_day["extra"])["start"] == {"date": "2026-04-03"}


# ---------------------------------------------------------------------------
# YouTube (two-call: search.list then videos.list)
# ---------------------------------------------------------------------------


def test_youtube_search_enriched_with_stats() -> None:
    search_payload = {
        "items": [
            {
                "id": {"videoId": "abc"},
                "snippet": {
                    "title": "DuckDB intro",
                    "channelTitle": "DataChan",
                    "channelId": "ch1",
                    "publishedAt": "2026-02-05T15:00:00Z",
                    "thumbnails": {"default": {"url": "https://t/abc"}},
                },
            },
            {"id": {"videoId": "xyz"}, "snippet": {"title": "No stats"}},
        ],
        "nextPageToken": "Y2",
    }
    videos_payload = {
        "items": [
            {
                "id": "abc",
                "statistics": {"viewCount": "1234", "likeCount": "56", "commentCount": "7"},
                "contentDetails": {"duration": "PT4M13S"},
            }
            # 'xyz' deliberately absent -> its stats stay NULL.
        ]
    }
    service = _StubService({"search": {"list": search_payload}, "videos": {"list": videos_payload}})
    page = YouTubeAdapter().fetch_page(service, _Args(query="duckdb", page_size=10), None)

    assert page.next_cursor == "Y2"
    abc, xyz = page.rows
    assert abc["video_id"] == "abc"
    assert abc["url"] == "https://www.youtube.com/watch?v=abc"
    assert abc["view_count"] == 1234
    assert abc["like_count"] == 56
    assert abc["duration"] == "PT4M13S"
    assert abc["published_at"] == datetime(2026, 2, 5, 15, 0, tzinfo=UTC)
    # Missing stats -> NULL, not an error.
    assert xyz["view_count"] is None
    assert xyz["duration"] is None


# ---------------------------------------------------------------------------
# Sheets (single-shot, optional header)
# ---------------------------------------------------------------------------


def test_sheets_header_builds_record_json() -> None:
    payload = {"range": "S!A1:B3", "values": [["name", "score"], ["Ada", "90"], ["Bob", "85"]]}
    service = _StubService({"spreadsheets": {"values": {"__methods__": {"get": payload}}}})
    page = SheetsAdapter().fetch_page(service, _Args(spreadsheet_id="S", range="S!A1:B3", header=True), None)

    assert [r["row_number"] for r in page.rows] == [1, 2]
    assert page.rows[0]["values"] == ["Ada", "90"]
    assert json.loads(page.rows[0]["record"]) == {"name": "Ada", "score": "90"}
    # Single-shot: a second tick (non-None cursor) yields nothing.
    assert SheetsAdapter().fetch_page(service, _Args(spreadsheet_id="S", range="x", header=True), "__done__").rows == []


def test_sheets_without_header_has_null_record() -> None:
    payload = {"values": [["x", "y"], ["1", "2"]]}
    service = _StubService({"spreadsheets": {"values": {"__methods__": {"get": payload}}}})
    page = SheetsAdapter().fetch_page(service, _Args(spreadsheet_id="S", range="r", header=False), None)
    assert len(page.rows) == 2
    assert page.rows[0]["record"] is None
    assert page.rows[0]["values"] == ["x", "y"]

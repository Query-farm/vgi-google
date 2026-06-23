"""A canned, paginated Google transport + static discovery docs for tests.

Two pieces drive every deterministic E2E with no network and no credentials:

* :data:`DISCOVERY_DOCS` — minimal but real discovery documents for the four
  curated adapters (Sheets / Drive / Calendar / YouTube) plus a tiny ``gmail``
  doc for the generic-hatch test. ``build_from_document`` builds a genuine client
  from these, so the *same* adapter code path runs as against live Google.

* :class:`FakeGoogleHttp` — an injectable transport implementing the
  ``httplib2``-style ``request()`` contract that
  ``google-api-python-client`` calls. It routes by URL path + query string and
  serves deterministic, PAGINATED responses: Drive / Calendar / YouTube hand back
  ONE item per page with a ``nextPageToken`` until ``TOTAL`` is reached, then a
  final token-less page. That is what lets the suite prove the
  ``pageToken``/``nextPageToken`` scan state round-trips across batch boundaries
  with every row emitted exactly once.

A fresh transport is created per built client (per scan tick), so the cursor in
the request URL — not transport state — is the source of truth, exactly as in
production. This makes the single-worker-pin / no-duplication test meaningful.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httplib2

# How many synthetic items exist for any paged query.
TOTAL = 5


# ---------------------------------------------------------------------------
# Minimal discovery documents (only the methods the adapters/tests use).
# ---------------------------------------------------------------------------


def _doc(name: str, version: str, root: str, resources: dict[str, Any], schemas: dict[str, Any] | None = None) -> dict:
    return {
        "kind": "discovery#restDescription",
        "id": f"{name}:{version}",
        "name": name,
        "version": version,
        "rootUrl": root,
        "servicePath": "",
        "baseUrl": root,
        "schemas": schemas or {},
        "resources": resources,
    }


def _method(mid: str, path: str, http_method: str, params: dict[str, Any], order: list[str]) -> dict:
    return {
        "id": mid,
        "path": path,
        "httpMethod": http_method,
        "parameters": params,
        "parameterOrder": order,
    }


_STR = {"type": "string", "location": "query"}
_PATH = {"type": "string", "location": "path", "required": True}

SHEETS_DOC = _doc(
    "sheets",
    "v4",
    "https://sheets.googleapis.com/",
    {
        "spreadsheets": {
            "resources": {
                "values": {
                    "methods": {
                        "get": _method(
                            "sheets.spreadsheets.values.get",
                            "v4/spreadsheets/{spreadsheetId}/values/{range}",
                            "GET",
                            {"spreadsheetId": _PATH, "range": _PATH,
                             "majorDimension": _STR, "valueRenderOption": _STR},
                            ["spreadsheetId", "range"],
                        )
                    }
                }
            }
        }
    },
)

DRIVE_DOC = _doc(
    "drive",
    "v3",
    "https://www.googleapis.com/drive/v3/",
    {
        "files": {
            "methods": {
                "list": _method(
                    "drive.files.list",
                    "files",
                    "GET",
                    {k: _STR for k in (
                        "q", "pageSize", "pageToken", "fields", "orderBy",
                        "driveId", "corpora", "includeItemsFromAllDrives", "supportsAllDrives")},
                    [],
                )
            }
        }
    },
)

CALENDAR_DOC = _doc(
    "calendar",
    "v3",
    "https://www.googleapis.com/calendar/v3/",
    {
        "events": {
            "methods": {
                "list": _method(
                    "calendar.events.list",
                    "calendars/{calendarId}/events",
                    "GET",
                    {"calendarId": _PATH, **{k: _STR for k in (
                        "maxResults", "pageToken", "singleEvents", "orderBy",
                        "timeMin", "timeMax", "q")}},
                    ["calendarId"],
                )
            }
        }
    },
)

YOUTUBE_DOC = _doc(
    "youtube",
    "v3",
    "https://www.googleapis.com/youtube/v3/",
    {
        "search": {
            "methods": {
                "list": _method(
                    "youtube.search.list",
                    "search",
                    "GET",
                    {k: _STR for k in ("q", "part", "type", "maxResults", "order", "pageToken")},
                    ["part"],
                )
            }
        },
        "videos": {
            "methods": {
                "list": _method(
                    "youtube.videos.list",
                    "videos",
                    "GET",
                    {k: _STR for k in ("part", "id")},
                    ["part"],
                )
            }
        },
    },
)

GMAIL_DOC = _doc(
    "gmail",
    "v1",
    "https://gmail.googleapis.com/",
    {
        "users": {
            "resources": {
                "labels": {
                    "methods": {
                        "list": _method(
                            "gmail.users.labels.list",
                            "gmail/v1/users/{userId}/labels",
                            "GET",
                            {"userId": _PATH},
                            ["userId"],
                        )
                    }
                }
            }
        }
    },
)

DISCOVERY_DOCS: dict[tuple[str, str], dict] = {
    ("sheets", "v4"): SHEETS_DOC,
    ("drive", "v3"): DRIVE_DOC,
    ("calendar", "v3"): CALENDAR_DOC,
    ("youtube", "v3"): YOUTUBE_DOC,
    ("gmail", "v1"): GMAIL_DOC,
}


# ---------------------------------------------------------------------------
# Canned response payloads.
# ---------------------------------------------------------------------------


def _page_index(qs: dict[str, list[str]]) -> int:
    """Map a pageToken ('' or 'p<N>') in the query string to a 0-based index."""
    token = qs.get("pageToken", [""])[0]
    if token.startswith("p"):
        try:
            return int(token[1:])
        except ValueError:
            return 0
    return 0


def _next_token(idx: int) -> str | None:
    return f"p{idx + 1}" if idx + 1 < TOTAL else None


def _drive_file(i: int) -> dict:
    return {
        "id": f"file{i}",
        "name": f"Document {i}.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": f"2026-03-0{(i % 9) + 1}T12:00:00Z",
        "createdTime": f"2026-01-0{(i % 9) + 1}T08:00:00Z",
        "size": str(1000 * (i + 1)),
        "webViewLink": f"https://drive.google.com/file/d/file{i}/view",
        "iconLink": f"https://drive-icons/{i}",
        "parents": [f"folder{i}"],
        "owners": [{"displayName": f"Owner {i}", "emailAddress": f"owner{i}@example.com"}],
        "trashed": False,
        "starred": i % 2 == 0,
    }


def _calendar_event(i: int) -> dict:
    return {
        "id": f"event{i}",
        "summary": f"Meeting {i}",
        "description": f"Notes {i}",
        "location": f"Room {i}",
        "status": "confirmed",
        "start": {"dateTime": f"2026-04-0{(i % 9) + 1}T09:00:00Z"},
        "end": {"dateTime": f"2026-04-0{(i % 9) + 1}T10:00:00Z"},
        "organizer": {"email": f"organizer{i}@example.com"},
        "creator": {"email": f"creator{i}@example.com"},
        "htmlLink": f"https://calendar.google.com/event?eid={i}",
        "attendees": [{"email": f"guest{i}@example.com"}],
    }


def _youtube_search_item(i: int) -> dict:
    return {
        "id": {"kind": "youtube#video", "videoId": f"vid{i}"},
        "snippet": {
            "title": f"Video {i}",
            "description": f"Description {i}",
            "channelTitle": f"Channel {i}",
            "channelId": f"chan{i}",
            "publishedAt": f"2026-02-0{(i % 9) + 1}T15:00:00Z",
            "thumbnails": {"default": {"url": f"https://i.ytimg.com/vi/vid{i}/default.jpg"}},
        },
    }


def _youtube_video_stats(vid: str) -> dict:
    i = int(vid.replace("vid", "")) if vid.startswith("vid") else 0
    return {
        "id": vid,
        "statistics": {"viewCount": str(1000 * (i + 1)), "likeCount": str(10 * (i + 1)), "commentCount": str(i)},
        "contentDetails": {"duration": f"PT{i + 1}M30S"},
    }


class FakeGoogleHttp:
    """An injectable transport returning canned, paginated Google responses.

    Implements the ``httplib2``-style ``request()`` the discovery client calls.
    Routing is by URL path; pagination is by the ``pageToken`` query parameter so
    the cursor in the request (not transport state) drives the page returned.
    """

    def request(  # noqa: D401 - transport contract
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        redirections: int = 1,
        connection_type: Any = None,
    ) -> tuple[httplib2.Response, bytes]:
        parsed = urlparse(uri)
        path = parsed.path
        qs = parse_qs(parsed.query)
        return _ok(self._route(path, qs))

    def _route(self, path: str, qs: dict[str, list[str]]) -> dict:
        if "/values/" in path:
            return self._sheets(qs)
        if path.endswith("/files") or path.endswith("drive/v3/files"):
            return self._paged(qs, "files", _drive_file)
        if "/events" in path:
            return self._paged(qs, "items", _calendar_event)
        if path.endswith("/search"):
            return self._paged(qs, "items", _youtube_search_item)
        if path.endswith("/videos"):
            return self._youtube_videos(qs)
        if path.endswith("/labels"):
            return {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "SENT", "name": "SENT"}]}
        return {}

    def _sheets(self, qs: dict[str, list[str]]) -> dict:
        # A 3x2 grid with a header row.
        return {
            "range": "Sheet1!A1:B3",
            "majorDimension": "ROWS",
            "values": [["name", "score"], ["Ada", "90"], ["Bob", "85"]],
        }

    def _paged(self, qs: dict[str, list[str]], field: str, factory: Any) -> dict:
        idx = _page_index(qs)
        if idx >= TOTAL:
            return {field: []}
        body: dict[str, Any] = {field: [factory(idx)]}
        token = _next_token(idx)
        if token is not None:
            body["nextPageToken"] = token
        return body

    def _youtube_videos(self, qs: dict[str, list[str]]) -> dict:
        ids = qs.get("id", [""])[0].split(",")
        return {"items": [_youtube_video_stats(v) for v in ids if v]}


def _ok(payload: dict) -> tuple[httplib2.Response, bytes]:
    resp = httplib2.Response({"status": "200", "content-type": "application/json"})
    return resp, json.dumps(payload).encode("utf-8")


def http_factory(api: str, version: str) -> tuple[FakeGoogleHttp, dict]:
    """A ``set_http_factory`` callable: a fresh transport + the static doc.

    Raises KeyError for an API with no canned discovery doc, surfacing the gap
    rather than silently building nothing.
    """
    return FakeGoogleHttp(), DISCOVERY_DOCS[(api, version)]

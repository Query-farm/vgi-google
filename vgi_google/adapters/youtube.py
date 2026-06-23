"""YouTube adapter — Data API v3 search + video statistics (API key).

``search.list`` returns matching videos (id + snippet) with native
``pageToken``/``nextPageToken`` pagination; one ``videos.list`` call per page
then enriches the rows with statistics (view/like/comment counts) and duration.
So each scan tick makes at most two requests and emits one page.

Auth: an **API key** is sufficient for public search/list (no user consent), so
this adapter is the natural fit for the API-key credential mode. Quota is the
real cost here — a ``search.list`` call is 100 units against the default daily
quota; mind it.
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
    parse_int,
    parse_timestamp,
)
from .base import Page

SCHEMA: pa.Schema = pa.schema(
    [
        afield("video_id", pa.string(), "YouTube video ID.", nullable=False),
        afield("title", pa.string(), "Video title."),
        afield("description", pa.string(), "Video description snippet."),
        afield("channel_title", pa.string(), "Channel display name."),
        afield("channel_id", pa.string(), "Channel ID."),
        afield("published_at", TIMESTAMPTZ, "Publication time (TIMESTAMPTZ)."),
        afield("view_count", pa.int64(), "View count, when statistics are available."),
        afield("like_count", pa.int64(), "Like count, when statistics are available."),
        afield("comment_count", pa.int64(), "Comment count, when statistics are available."),
        afield("duration", pa.string(), "ISO-8601 duration (e.g. 'PT4M13S'), when available."),
        afield("url", pa.string(), "Canonical watch URL.", nullable=False),
        json_field("extra", "Thumbnails and other snippet fields, JSON-encoded."),
    ]
)


class YouTubeAdapter:
    """Search YouTube videos as rows via ``search.list`` + ``videos.list``."""

    api = "youtube"
    version = "v3"
    # Public search/list needs no OAuth scope when using an API key; the
    # read-only scope is listed for the OAuth-user path (documented follow-up).
    scopes = ["https://www.googleapis.com/auth/youtube.readonly"]
    schema = SCHEMA

    def fetch_page(self, service: Any, args: Any, cursor: str | None) -> Page:
        """Fetch one page of rows from the API for this adapter."""
        search_params: dict[str, Any] = {
            "q": getattr(args, "query", ""),
            "part": "snippet",
            "type": "video",
            "maxResults": max(1, min(getattr(args, "page_size", 0) or 25, 50)),
        }
        if getattr(args, "order", None):
            search_params["order"] = args.order
        if cursor:
            search_params["pageToken"] = cursor

        search = resolve_method(service, "search.list")
        payload = execute(search(**search_params))

        items = payload.get("items", [])
        rows = [self._map_search(it) for it in items]
        rows = [r for r in rows if r["video_id"]]

        # Enrich with statistics + duration in one videos.list call per page.
        ids = [r["video_id"] for r in rows]
        if ids:
            stats = self._fetch_stats(service, ids)
            for r in rows:
                s = stats.get(r["video_id"])
                if s:
                    r.update(s)

        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))

    @staticmethod
    def _fetch_stats(service: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
        videos = resolve_method(service, "videos.list")
        payload = execute(videos(part="statistics,contentDetails", id=",".join(ids)))
        out: dict[str, dict[str, Any]] = {}
        for item in payload.get("items", []):
            vid = item.get("id")
            if not vid:
                continue
            stats = item.get("statistics") or {}
            content = item.get("contentDetails") or {}
            out[vid] = {
                "view_count": parse_int(stats.get("viewCount")),
                "like_count": parse_int(stats.get("likeCount")),
                "comment_count": parse_int(stats.get("commentCount")),
                "duration": content.get("duration"),
            }
        return out

    @staticmethod
    def _map_search(item: dict[str, Any]) -> dict[str, Any]:
        vid = (item.get("id") or {}).get("videoId")
        snippet = item.get("snippet") or {}
        extra: dict[str, Any] = {}
        if snippet.get("thumbnails"):
            extra["thumbnails"] = snippet["thumbnails"]
        return {
            "video_id": vid,
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "channel_title": snippet.get("channelTitle"),
            "channel_id": snippet.get("channelId"),
            "published_at": parse_timestamp(snippet.get("publishedAt")),
            "view_count": None,
            "like_count": None,
            "comment_count": None,
            "duration": None,
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else None,
            "extra": json_dumps(extra),
        }

"""Drive adapter — ``files.list`` rows (Drive API v3).

Maps a Drive file-search (``files.list``) into clean file rows. Pagination is
Drive's native ``pageToken``/``nextPageToken`` carried as scan state.

Required OAuth scope (read-only): ``drive.metadata.readonly`` for metadata-only
listing, or ``drive.readonly`` if you also intend to read file content via the
generic hatch. The service account must be granted access to the files/Shared
Drives it lists (share them with the service-account e-mail, or use domain-wide
delegation).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from ..discovery import execute, resolve_method
from ..schema_utils import (
    LIST_VARCHAR,
    TIMESTAMPTZ,
    afield,
    json_dumps,
    json_field,
    parse_int,
    parse_timestamp,
)
from .base import Page

# The fields we request and map. Requesting an explicit fields mask keeps the
# response small and stable (and is a Drive best practice).
_FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,createdTime,size,webViewLink,"
    "iconLink,parents,owners(displayName,emailAddress),trashed,starred"
)
_FIELDS_MASK = f"nextPageToken,files({_FILE_FIELDS})"

SCHEMA: pa.Schema = pa.schema(
    [
        afield("id", pa.string(), "Drive file ID.", nullable=False),
        afield("name", pa.string(), "File name."),
        afield("mime_type", pa.string(), "MIME type (e.g. 'application/pdf')."),
        afield("modified_time", TIMESTAMPTZ, "Last modification time (TIMESTAMPTZ)."),
        afield("created_time", TIMESTAMPTZ, "Creation time (TIMESTAMPTZ)."),
        afield("size", pa.int64(), "Size in bytes, when the file has a binary content size."),
        afield("web_view_link", pa.string(), "A link to open the file in a browser."),
        afield("icon_link", pa.string(), "A link to the file's icon."),
        afield("parents", LIST_VARCHAR, "Parent folder IDs (LIST<VARCHAR>)."),
        afield("owner", pa.string(), "Primary owner display name, when available."),
        afield("trashed", pa.bool_(), "Whether the file is in the trash."),
        afield("starred", pa.bool_(), "Whether the file is starred."),
        json_field("extra", "Owner e-mail and other fields not in flat columns, JSON-encoded."),
    ]
)


class DriveAdapter:
    """List/search Drive files as rows via ``files.list``."""

    api = "drive"
    version = "v3"
    scopes = ["https://www.googleapis.com/auth/drive.metadata.readonly"]
    schema = SCHEMA

    def fetch_page(self, service: Any, args: Any, cursor: str | None) -> Page:
        """Fetch one page of rows from the API for this adapter."""
        params: dict[str, Any] = {
            "pageSize": max(1, min(getattr(args, "page_size", 0) or 100, 1000)),
            "fields": _FIELDS_MASK,
        }
        if getattr(args, "query", None):
            params["q"] = args.query
        if getattr(args, "order_by", None):
            params["orderBy"] = args.order_by
        if getattr(args, "drive_id", None):
            params["driveId"] = args.drive_id
            params["corpora"] = "drive"
            params["includeItemsFromAllDrives"] = True
            params["supportsAllDrives"] = True
        if cursor:
            params["pageToken"] = cursor

        method = resolve_method(service, "files.list")
        payload = execute(method(**params))

        rows = [self._map(f) for f in payload.get("files", [])]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))

    @staticmethod
    def _map(f: dict[str, Any]) -> dict[str, Any]:
        owners = f.get("owners") or []
        owner_name = owners[0].get("displayName") if owners else None
        owner_email = owners[0].get("emailAddress") if owners else None

        extra: dict[str, Any] = {}
        if owner_email:
            extra["owner_email"] = owner_email

        return {
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType"),
            "modified_time": parse_timestamp(f.get("modifiedTime")),
            "created_time": parse_timestamp(f.get("createdTime")),
            "size": parse_int(f.get("size")),
            "web_view_link": f.get("webViewLink"),
            "icon_link": f.get("iconLink"),
            "parents": f.get("parents"),
            "owner": owner_name,
            "trashed": f.get("trashed"),
            "starred": f.get("starred"),
            "extra": json_dumps(extra),
        }

"""Sheets adapter — a values range as rows (Google Sheets API v4).

``spreadsheets.values.get`` returns a 2-D array of cell values for a range. This
adapter exposes that as rows with a STABLE, fixed schema (so it composes in SQL
without a discover-the-columns round-trip):

* ``row_number`` — 1-based row index within the returned range (after the header
  row is consumed, when ``header := true``).
* ``values`` — the row's cells as a LIST<VARCHAR> (every Sheets value rendered as
  text; Sheets itself returns strings/numbers untyped per cell).
* ``record`` — when ``header := true``, a JSON object mapping the header row's
  column names to this row's cell values; NULL when ``header := false``.

Sheets ``values.get`` is not paginated (the range bounds the result), so this is
a single-shot adapter: one fetch, all rows, then done.

Required OAuth scope (read-only): ``spreadsheets.readonly``. The service account
must be granted at least viewer access to the spreadsheet (share it with the
service-account e-mail). For a *public* sheet, an API key suffices.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from ..discovery import execute, resolve_method
from ..schema_utils import LIST_VARCHAR, afield, json_dumps, json_field
from .base import Page

SCHEMA: pa.Schema = pa.schema(
    [
        afield("row_number", pa.int64(), "1-based row index within the range (after any header row).", nullable=False),
        afield("values", LIST_VARCHAR, "The row's cells as text (LIST<VARCHAR>)."),
        json_field("record", "When header := true, a JSON object of header->cell for this row; else NULL."),
    ]
)


class SheetsAdapter:
    """Read a Sheets range as rows via ``spreadsheets.values.get``."""

    api = "sheets"
    version = "v4"
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    schema = SCHEMA

    def fetch_page(self, service: Any, args: Any, cursor: str | None) -> Page:
        # Single-shot: a non-None cursor means "already fetched" — return empty.
        if cursor is not None:
            return Page(rows=[], next_cursor=None)

        method = resolve_method(service, "spreadsheets.values.get")
        payload = execute(
            method(
                spreadsheetId=args.spreadsheet_id,
                range=args.range,
                majorDimension="ROWS",
                valueRenderOption="FORMATTED_VALUE",
            )
        )

        grid = payload.get("values", []) or []
        header_row: list[str] | None = None
        data_rows = grid
        if getattr(args, "header", False) and grid:
            header_row = [str(c) for c in grid[0]]
            data_rows = grid[1:]

        rows: list[dict[str, Any]] = []
        for i, raw in enumerate(data_rows, start=1):
            cells = [_cell_text(c) for c in raw]
            record = None
            if header_row is not None:
                record = json_dumps({header_row[j]: cells[j] for j in range(min(len(header_row), len(cells)))})
            rows.append({"row_number": i, "values": cells, "record": record})

        # "page_token" sentinel marks the single page as consumed.
        return Page(rows=rows, next_cursor="__done__")

    @staticmethod
    def map_done_cursor(cursor: str | None) -> bool:
        """Whether ``cursor`` is the single-shot 'already fetched' sentinel."""
        return cursor == "__done__"


def _cell_text(value: Any) -> str | None:
    """Render a Sheets cell to text (Sheets returns mixed str/number cells)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)

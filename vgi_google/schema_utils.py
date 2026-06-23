"""Shared Arrow-schema helpers used by every curated adapter.

Each adapter declares its output as a ``pa.Schema`` of :func:`afield` columns and
maps the raw Google API response into a list of plain ``dict`` rows; :func:`
rows_to_batch` turns those rows into a RecordBatch in the (possibly projected)
output schema, with any missing key rendered as SQL ``NULL``.

Parameterized Arrow types (LIST, TIMESTAMPTZ) and the JSON-tagged VARCHAR column
are pinned here once so every adapter declares them identically and the SDK gets
the explicit ``arrow_type`` it requires for these returns.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

# DuckDB TIMESTAMPTZ is microsecond precision with a UTC tz marker.
TIMESTAMPTZ = pa.timestamp("us", tz="UTC")
#: LIST<VARCHAR>, the type for repeated string columns (e.g. Drive ``parents``).
LIST_VARCHAR = pa.list_(pa.string())

# Field-metadata key the VGI framework maps to DuckDB's JSON logical type, so a
# plain Arrow string column is surfaced to SQL as JSON.
_LOGICAL_TYPE_JSON = b"JSON"


def afield(
    name: str,
    type: pa.DataType,
    comment: str,
    *,
    nullable: bool = True,
) -> pa.Field:
    """Build a ``pa.Field`` carrying a column comment in its metadata.

    The ``comment`` metadata key is the framework's transport for column
    comments — DuckDB surfaces it via ``duckdb_columns()`` and ``DESCRIBE``.
    """
    return pa.field(
        name,
        type,
        nullable=nullable,
        metadata={b"comment": comment.encode("utf-8")},
    )


def json_field(name: str, comment: str) -> pa.Field:
    """Build a JSON-typed VARCHAR column (string Arrow type, tagged JSON).

    Use for nested / object fields that have no flat column. The value stored is
    a JSON-encoded string (see :func:`json_dumps`); DuckDB exposes it as JSON.
    """
    return pa.field(
        name,
        pa.string(),
        nullable=True,
        metadata={b"comment": comment.encode("utf-8"), b"logical_type": _LOGICAL_TYPE_JSON},
    )


def json_dumps(value: Any) -> str | None:
    """JSON-encode a value for a JSON column, or None when empty/None.

    ``None`` and empty containers map to SQL NULL; everything else is stable,
    sorted JSON (``default=str`` so stray datetimes never explode).
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)) and not value:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 date/datetime into a tz-aware UTC datetime, or None.

    Accepts ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD`` and full RFC-3339 timestamps
    (the shape Google APIs emit, e.g. ``2026-01-02T03:04:05Z``). Naive inputs are
    assumed UTC. Returns None on anything unparseable rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if len(text) == 4 and text.isdigit():
        text = f"{text}-01-01"
    elif len(text) == 7 and text[4] == "-":
        text = f"{text}-01"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_int(value: Any) -> int | None:
    """Coerce a possibly-stringy integer (Google often sends counts as strings)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def rows_to_batch(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build one RecordBatch in ``schema`` from a list of dict rows.

    For each column named by ``schema`` we pull that key from every row
    (missing → ``None`` → SQL NULL) and build a typed Arrow array. Honoring the
    passed-in (projected) schema means projection pushdown works without any
    per-adapter code.
    """
    arrays = [
        pa.array([row.get(name) for row in rows], type=schema.field(name).type) for name in schema.names
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=schema)

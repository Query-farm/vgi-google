"""Mock E2E: drive the ``google_*`` table functions against a canned transport.

Installs :func:`tests.mock_google.http_factory` as the client's HTTP seam, so
every adapter builds a real discovery client from a static doc and is served
deterministic, PAGINATED responses — no live Google, no credentials. These
assert the externally important behavior end-to-end:

* each adapter's fixed output schema (column names + LIST/TIMESTAMPTZ/JSON types);
* the CENTERPIECE — the ``pageToken``/``nextPageToken`` scan state ROUND-TRIPS
  across batch boundaries: with one item per page, ``count`` forces ``count``
  paged ticks and we get every row exactly once, in order, with no dupes/drops;
* a proof that the single-worker pin makes the scan exactly-once — two
  independent scan instances (as parallel workers would be) each replay the whole
  result, which WOULD duplicate rows if the table functions were not pinned;
* ``count`` caps total rows below what is available;
* a clean error (raised RuntimeError, not a crash) on an API failure;
* the generic ``google_call`` hatch expands a list response into rows and pages.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyarrow as pa
import pytest

from tests.harness import invoke_table_function
from tests.mock_google import TOTAL, FakeGoogleHttp, http_factory
from vgi_google import client
from vgi_google.tables import (
    GoogleCalendarFunction,
    GoogleCallFunction,
    GoogleDriveFunction,
    GoogleSheetFunction,
    GoogleYouTubeFunction,
    _run_paged_tick,
    _ScanState,
)


@pytest.fixture()
def mock_google() -> Iterator[None]:
    client.set_http_factory(http_factory)
    try:
        yield
    finally:
        client.set_http_factory(None)


# ---------------------------------------------------------------------------
# Schemas + typed columns
# ---------------------------------------------------------------------------


def test_drive_schema_types(mock_google: None) -> None:
    table = invoke_table_function(GoogleDriveFunction, named={"count": 1, "page_size": 1})
    assert table.schema.field("parents").type == pa.list_(pa.string())
    mt = table.schema.field("modified_time").type
    assert pa.types.is_timestamp(mt) and mt.tz == "UTC"
    assert table.schema.field("size").type == pa.int64()
    rows = table.to_pylist()
    assert rows[0]["id"] == "file0"
    assert rows[0]["parents"] == ["folder0"]


def test_sheet_header_record(mock_google: None) -> None:
    table = invoke_table_function(GoogleSheetFunction, positional=("SID", "Sheet1!A1:B3"), named={"header": True})
    rows = table.to_pylist()
    assert [r["row_number"] for r in rows] == [1, 2]
    assert rows[0]["values"] == ["Ada", "90"]


def test_youtube_enriched(mock_google: None) -> None:
    table = invoke_table_function(GoogleYouTubeFunction, positional=("duckdb",), named={"count": 2, "page_size": 1})
    rows = table.to_pylist()
    assert rows[0]["video_id"] == "vid0"
    assert rows[0]["view_count"] == 1000  # enriched from videos.list
    assert rows[0]["url"] == "https://www.youtube.com/watch?v=vid0"


# ---------------------------------------------------------------------------
# CENTERPIECE: pageToken scan-state round-trip across batch boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("func", "positional", "named"),
    [
        (GoogleDriveFunction, (), {"count": TOTAL, "page_size": 1}),
        (GoogleCalendarFunction, (), {"count": TOTAL, "page_size": 1}),
        (GoogleYouTubeFunction, ("q",), {"count": TOTAL, "page_size": 1}),
    ],
)
def test_pagetoken_roundtrip_every_row_once(mock_google: None, func: type, positional: tuple, named: dict) -> None:
    """One item per page; count=TOTAL forces TOTAL paged ticks.

    If the pageToken scan state did not round-trip across batches we would either
    re-fetch page 0 forever (dupes) or stop after one row (drops). Getting all
    TOTAL distinct rows, in page order, proves the cursor advanced and was
    preserved across batch boundaries.
    """
    table = invoke_table_function(func, positional=positional, named=named)
    rows = table.to_pylist()
    assert len(rows) == TOTAL
    # Each row carries a page-index suffix in its id-like field; collect it.
    key = "id" if "id" in table.schema.names else "video_id"
    ids = [r[key] for r in rows]
    assert len(set(ids)) == TOTAL  # no duplicates
    # Page order 0..TOTAL-1 confirms the cursor advanced one page per tick.
    assert [s[-1] for s in ids] == [str(i) for i in range(TOTAL)]


def test_count_caps_below_available(mock_google: None) -> None:
    table = invoke_table_function(GoogleDriveFunction, named={"count": 2, "page_size": 1})
    assert table.num_rows == 2


# ---------------------------------------------------------------------------
# Single-worker pin: independent scan instances would each replay everything.
# ---------------------------------------------------------------------------


def test_single_worker_pin_prevents_duplication(mock_google: None) -> None:
    """Prove WHY the @init_single_worker pin matters.

    A parallel scan would spin up multiple independent worker instances, each
    with its OWN initial scan state — each re-runs the whole paged scan and emits
    every row. Here we simulate TWO such instances and show that, unpinned, they
    together emit 2*TOTAL rows (every row twice). The ``@init_single_worker``
    decorator is what forbids that fan-out in production, making the real scan
    emit each row exactly once (see test_pagetoken_roundtrip_every_row_once).
    """
    adapter = GoogleDriveFunction._adapter
    schema = GoogleDriveFunction.FIXED_SCHEMA

    class _Args:
        query = ""
        count = TOTAL
        page_size = 1
        order_by = ""
        drive_id = ""

    class _Params:
        args = _Args()
        secrets: dict = {}
        output_schema = schema

    class _Out:
        def __init__(self) -> None:
            self.rows: list = []
            self.fin = False

        def emit(self, batch: pa.RecordBatch) -> None:
            self.rows.extend(batch.column(0).to_pylist())

        def finish(self) -> None:
            self.fin = True

    def run_one_instance() -> list:
        state = _ScanState()
        out = _Out()
        while not out.fin:
            _run_paged_tick(adapter, _Params(), state, out, require_auth=True)
        return out.rows

    instance_a = run_one_instance()
    instance_b = run_one_instance()

    # A single pinned instance emits each row exactly once...
    assert len(instance_a) == TOTAL
    assert len(set(instance_a)) == TOTAL
    # ...but two unpinned parallel instances each replay the full result, so the
    # combined stream double-counts every id. THIS is the duplication the
    # single-worker pin exists to prevent.
    combined = instance_a + instance_b
    assert len(combined) == 2 * TOTAL
    assert sorted(combined) == sorted(instance_a + instance_a)


# ---------------------------------------------------------------------------
# Generic hatch + clean errors.
# ---------------------------------------------------------------------------


def test_google_call_expands_list_rows(mock_google: None) -> None:
    table = invoke_table_function(
        GoogleCallFunction, positional=("gmail", "v1", "users.labels.list", '{"userId":"me"}')
    )
    rows = table.to_pylist()
    assert {r["result"] for r in rows} == {'{"id": "INBOX", "name": "INBOX"}', '{"id": "SENT", "name": "SENT"}'}


def test_google_call_invalid_json_rejected_at_bind(mock_google: None) -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        invoke_table_function(GoogleCallFunction, positional=("gmail", "v1", "users.labels.list", "{bad"))


def test_api_error_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTP error from the transport surfaces as a clean RuntimeError."""
    import httplib2
    from googleapiclient.errors import HttpError

    class _ErrorHttp(FakeGoogleHttp):
        def request(self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None):
            resp = httplib2.Response({"status": "500"})
            resp.reason = "Server Error"
            raise HttpError(resp, b'{"error":{"message":"boom"}}', uri=uri)

    from tests.mock_google import DISCOVERY_DOCS

    client.set_http_factory(lambda api, version: (_ErrorHttp(), DISCOVERY_DOCS[(api, version)]))
    monkeypatch.setattr("vgi_google.discovery._sleep", lambda *_: None)  # no backoff wait
    try:
        with pytest.raises(RuntimeError, match="DriveAdapter failed"):
            invoke_table_function(GoogleDriveFunction, named={"count": 1})
    finally:
        client.set_http_factory(None)

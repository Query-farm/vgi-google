"""The ``google_*`` table functions exposed to SQL.

Four curated READ adapters (TABLE functions, so ``name := value`` args are
allowed) — ``google_sheet`` / ``google_drive`` / ``google_calendar`` /
``google_youtube`` — plus the generic escape hatch ``google_call`` and the
discovery helpers ``google_apis`` / ``google_methods``.

Pagination is the externalized scan state: Google's
``pageToken``/``nextPageToken`` is carried in :class:`_ScanState` (an
``ArrowSerializableDataclass``), which the framework round-trips across
``process()`` ticks and, under HTTP transport, across batch boundaries. Each tick
fetches ONE page, emits it, advances the cursor, and stops when ``count`` rows are
produced or the API runs out.

CRITICAL: the paged adapters are pinned to a single worker
(``@init_single_worker``). Parallel scan instances would EACH re-emit the whole
result and duplicate rows — the bug that bit vgi-search and vgi-wikipedia. The
pin makes the scan state authoritative and the row stream exactly-once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import (
    BindParams,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from . import auth, client, discovery
from .adapters import CalendarAdapter, DriveAdapter, SheetsAdapter, YouTubeAdapter
from .adapters.base import Adapter
from .schema_utils import afield, json_field, rows_to_batch


@dataclass(kw_only=True)
class _ScanState(ArrowSerializableDataclass):
    """Externalized pagination state carried across ``process()`` ticks.

    Attributes:
        cursor: Google's opaque ``nextPageToken``, or None to start / when
            exhausted. THIS is the scan state that round-trips across batches.
        emitted: Rows emitted so far (to honor ``count``).
        started: False until the first tick runs (distinguishes "begin" from
            "the API returned no further token").
        done: True once we should stop (count reached or API exhausted).
    """

    cursor: str | None = None
    emitted: int = 0
    started: bool = False
    done: bool = False


def _run_paged_tick(
    adapter: Adapter,
    params: ProcessParams[Any],
    state: _ScanState,
    out: OutputCollector,
    *,
    require_auth: bool,
) -> None:
    """Shared one-page-per-tick driver for every curated adapter.

    Resolves the client (canned transport in tests; real auth otherwise),
    fetches one page from the adapter, emits it, advances the cursor, and stops
    on ``count`` / exhaustion. Any adapter, discovery, or auth failure becomes a
    clean DuckDB error (RuntimeError) — the worker never crashes.
    """
    count = params.args.count

    if state.done or state.emitted >= count:
        out.finish()
        return
    # Continuation tick with no cursor after we have started: API had no next
    # page, so we are done.
    if state.started and state.cursor is None:
        out.finish()
        return

    try:
        service = client.build_client(
            adapter.api,
            adapter.version,
            secrets=params.secrets,
            scopes=adapter.scopes,
            require_auth=require_auth,
        )
        page = adapter.fetch_page(service, params.args, state.cursor)
    except (discovery.GoogleApiError, auth.AuthError, ValueError) as exc:
        raise RuntimeError(f"{type(adapter).__name__} failed: {exc}") from exc

    state.started = True
    remaining = count - state.emitted
    rows = page.rows[:remaining]
    if rows:
        out.emit(rows_to_batch(rows, params.output_schema))
        state.emitted += len(rows)

    state.cursor = page.next_cursor
    if page.next_cursor is None or state.emitted >= count:
        state.done = True
    if not rows and page.next_cursor is None:
        out.finish()


# ---------------------------------------------------------------------------
# google_sheet
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _SheetArgs:
    spreadsheet_id: Annotated[str, Arg(0, doc="Spreadsheet ID (from the sheet URL).")]
    range: Annotated[str, Arg(1, doc="A1 range, e.g. 'Sheet1!A1:Z'.")]
    header: Annotated[bool, Arg("header", default=False, doc="Treat the first row as column headers.")]
    count: Annotated[int, Arg("count", default=10000, doc="Maximum rows to return.", ge=1, le=1_000_000)]


@init_single_worker
@bind_fixed_schema
class GoogleSheetFunction(TableFunctionGenerator[_SheetArgs, _ScanState]):
    """Read a Google Sheets range as rows (service account or API key)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = SheetsAdapter.schema
    _adapter: ClassVar[Adapter] = SheetsAdapter()

    class Meta:
        """Function metadata."""

        name = "google_sheet"
        description = "Read a Google Sheets range as rows (values, optional header)"
        categories = ["google", "sheets", "spreadsheet"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM google_sheet('1AbC...', 'Sheet1!A1:Z', header := true)",
                description="Read a sheet range with the first row as headers",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_SheetArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_SheetArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_SheetArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_paged_tick(cls._adapter, params, state, out, require_auth=True)


# ---------------------------------------------------------------------------
# google_drive
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _DriveArgs:
    query: Annotated[str, Arg("query", default="", doc="Drive 'q' search query (Drive query syntax).")]
    count: Annotated[int, Arg("count", default=100, doc="Maximum files to return.", ge=1, le=1_000_000)]
    page_size: Annotated[int, Arg("page_size", default=0, doc="Files per request (0 = automatic).", ge=0, le=1000)]
    order_by: Annotated[str, Arg("order_by", default="", doc="Sort key, e.g. 'modifiedTime desc'.")]
    drive_id: Annotated[str, Arg("drive_id", default="", doc="Shared Drive ID to search within.")]


@init_single_worker
@bind_fixed_schema
class GoogleDriveFunction(TableFunctionGenerator[_DriveArgs, _ScanState]):
    """List/search Google Drive files as rows (service account)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = DriveAdapter.schema
    _adapter: ClassVar[Adapter] = DriveAdapter()

    class Meta:
        """Function metadata."""

        name = "google_drive"
        description = "List/search Google Drive files as rows"
        categories = ["google", "drive", "files"]
        examples = [
            FunctionExample(
                sql="SELECT id, name FROM google_drive(query := \"mimeType='application/pdf'\", count := 100)",
                description="List up to 100 PDFs",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_DriveArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 100), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_DriveArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_DriveArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_paged_tick(cls._adapter, params, state, out, require_auth=True)


# ---------------------------------------------------------------------------
# google_calendar
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _CalendarArgs:
    calendar_id: Annotated[str, Arg("calendar_id", default="primary", doc="Calendar ID ('primary' or an address).")]
    time_min: Annotated[str, Arg("time_min", default="", doc="Lower time bound (date or RFC-3339).")]
    time_max: Annotated[str, Arg("time_max", default="", doc="Upper time bound (date or RFC-3339).")]
    query: Annotated[str, Arg("query", default="", doc="Free-text event search.")]
    count: Annotated[int, Arg("count", default=250, doc="Maximum events to return.", ge=1, le=1_000_000)]
    page_size: Annotated[int, Arg("page_size", default=0, doc="Events per request (0 = automatic).", ge=0, le=2500)]
    single_events: Annotated[bool, Arg("single_events", default=True, doc="Expand recurring events into instances.")]


@init_single_worker
@bind_fixed_schema
class GoogleCalendarFunction(TableFunctionGenerator[_CalendarArgs, _ScanState]):
    """List Google Calendar events as rows (service account)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CalendarAdapter.schema
    _adapter: ClassVar[Adapter] = CalendarAdapter()

    class Meta:
        """Function metadata."""

        name = "google_calendar"
        description = "List Google Calendar events as rows"
        categories = ["google", "calendar", "events"]
        examples = [
            FunctionExample(
                sql="SELECT id, summary, start_time FROM google_calendar(calendar_id := 'primary', count := 50)",
                description="List up to 50 events on the primary calendar",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_CalendarArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 250), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_CalendarArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_CalendarArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_paged_tick(cls._adapter, params, state, out, require_auth=True)


# ---------------------------------------------------------------------------
# google_youtube
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _YouTubeArgs:
    query: Annotated[str, Arg(0, doc="Search query.")]
    count: Annotated[int, Arg("count", default=25, doc="Maximum videos to return.", ge=1, le=1_000_000)]
    page_size: Annotated[int, Arg("page_size", default=0, doc="Videos per request (0 = automatic).", ge=0, le=50)]
    order: Annotated[str, Arg("order", default="", doc="Result order: relevance/date/viewCount/rating.")]


@init_single_worker
@bind_fixed_schema
class GoogleYouTubeFunction(TableFunctionGenerator[_YouTubeArgs, _ScanState]):
    """Search YouTube videos as rows (API key)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = YouTubeAdapter.schema
    _adapter: ClassVar[Adapter] = YouTubeAdapter()

    class Meta:
        """Function metadata."""

        name = "google_youtube"
        description = "Search YouTube videos as rows (with view/like/comment counts)"
        categories = ["google", "youtube", "video"]
        examples = [
            FunctionExample(
                sql="SELECT video_id, title, view_count FROM google_youtube('duckdb', count := 25)",
                description="Search YouTube for 'duckdb'",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_YouTubeArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 25), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_YouTubeArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_YouTubeArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_paged_tick(cls._adapter, params, state, out, require_auth=True)


# ---------------------------------------------------------------------------
# google_call — generic escape hatch
# ---------------------------------------------------------------------------

_CALL_SCHEMA: pa.Schema = pa.schema([json_field("result", "One result object as JSON, per API item / response.")])


@dataclass(slots=True, frozen=True)
class _CallArgs:
    api: Annotated[str, Arg(0, doc="Discovery API name, e.g. 'gmail'.")]
    version: Annotated[str, Arg(1, doc="API version, e.g. 'v1'.")]
    method: Annotated[str, Arg(2, doc="Dotted method path, e.g. 'users.messages.list'.")]
    params_json: Annotated[str, Arg(3, doc='JSON object of method parameters, e.g. \'{"userId":"me"}\'.')]
    count: Annotated[int, Arg("count", default=1000, doc="Maximum rows to return.", ge=1, le=1_000_000)]


@init_single_worker
@bind_fixed_schema
class GoogleCallFunction(TableFunctionGenerator[_CallArgs, _ScanState]):
    """Call any Google API method, returning JSON rows (the generic escape hatch).

    Maps the response into rows: if the top-level response has a single list
    field (e.g. ``files``, ``items``, ``messages``), each element becomes a row;
    otherwise the whole response is one row. ``nextPageToken`` is followed as
    scan state, so a long list pages cleanly. ``count`` caps total rows.
    """

    FIXED_SCHEMA: ClassVar[pa.Schema] = _CALL_SCHEMA

    class Meta:
        """Function metadata."""

        name = "google_call"
        description = "Call any Google API method; returns JSON rows (generic escape hatch)"
        categories = ["google", "generic", "json"]
        examples = [
            FunctionExample(
                sql="SELECT * FROM google_call('gmail', 'v1', 'users.labels.list', '{\"userId\":\"me\"}')",
                description="List Gmail labels via the generic hatch",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[_CallArgs]) -> BindResponse:
        """Validate ``params_json`` and pin the output schema."""
        # Validate params_json early so a malformed JSON is a clean bind error.
        try:
            parsed = json.loads(params.args.params_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"params_json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("params_json must be a JSON object")
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def cardinality(cls, params: BindParams[_CallArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 100), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_CallArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_CallArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        a = params.args
        count = a.count
        if state.done or state.emitted >= count:
            out.finish()
            return
        if state.started and state.cursor is None:
            out.finish()
            return

        try:
            method_params = json.loads(a.params_json or "{}")
            service = client.build_client(a.api, a.version, secrets=params.secrets, scopes=None, require_auth=True)
            request_method = discovery.resolve_method(service, a.method)
            if state.cursor:
                method_params = {**method_params, "pageToken": state.cursor}
            payload = discovery.execute(request_method(**method_params))
        except (discovery.GoogleApiError, auth.AuthError, ValueError) as exc:
            raise RuntimeError(f"google_call({a.api}:{a.version} {a.method}) failed: {exc}") from exc

        state.started = True
        items, next_token = _rows_from_payload(payload)
        remaining = count - state.emitted
        items = items[:remaining]
        if items:
            batch = pa.RecordBatch.from_arrays(
                [pa.array([json.dumps(it, ensure_ascii=False, default=str) for it in items], type=pa.string())],
                schema=params.output_schema,
            )
            out.emit(batch)
            state.emitted += len(items)

        state.cursor = next_token
        if next_token is None or state.emitted >= count:
            state.done = True
        if not items and next_token is None:
            out.finish()


def _rows_from_payload(payload: Any) -> tuple[list[Any], str | None]:
    """Split a Google response into (row-items, nextPageToken).

    Heuristic, matching the connector's documented behavior: if the response is a
    dict with exactly one list-valued field (besides paging metadata), each
    element is a row; otherwise the whole response is a single row.
    """
    next_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        list_fields = [(k, v) for k, v in payload.items() if isinstance(v, list) and k not in ("etag",)]
        if len(list_fields) == 1:
            return list(list_fields[0][1]), next_token
    return [payload], next_token


# ---------------------------------------------------------------------------
# Discovery helpers: google_apis() / google_methods(api, version)
# ---------------------------------------------------------------------------

_APIS_SCHEMA: pa.Schema = pa.schema(
    [
        afield("name", pa.string(), "API name to pass as the first google_call argument.", nullable=False),
        afield("version", pa.string(), "API version.", nullable=False),
        afield("title", pa.string(), "Human-readable API title."),
        afield("preferred", pa.bool_(), "Whether this is the preferred version of the API."),
        afield("discovery_url", pa.string(), "URL of the API's discovery document."),
    ]
)


@dataclass(slots=True, frozen=True)
class _ApisArgs:
    name: Annotated[str, Arg("name", default="", doc="Optional substring filter on API name.")]


@init_single_worker
@bind_fixed_schema
class GoogleApisFunction(TableFunctionGenerator[_ApisArgs, _ScanState]):
    """List Google APIs reachable via discovery (one row per API/version)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _APIS_SCHEMA

    class Meta:
        """Function metadata."""

        name = "google_apis"
        description = "List Google APIs reachable via the Discovery Service"
        categories = ["google", "discovery", "metadata"]
        examples = [
            FunctionExample(
                sql="SELECT name, version, title FROM google_apis() WHERE name LIKE '%sheets%'",
                description="Find the Sheets API and its versions",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_ApisArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=300, max=1000)

    @classmethod
    def initial_state(cls, params: ProcessParams[_ApisArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_ApisArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        if state.done:
            out.finish()
            return
        try:
            items = discovery.list_apis(name_filter=params.args.name or None)
        except discovery.GoogleApiError as exc:
            raise RuntimeError(f"google_apis() failed: {exc}") from exc
        state.done = True
        if items:
            out.emit(rows_to_batch(items, params.output_schema))
        out.finish()


_METHODS_SCHEMA: pa.Schema = pa.schema(
    [
        afield("method", pa.string(), "Dotted method path to pass to google_call.", nullable=False),
        afield("http_method", pa.string(), "HTTP verb (GET/POST/...)."),
        afield("path", pa.string(), "URL path template."),
        afield("description", pa.string(), "Method description."),
        afield("parameters", pa.list_(pa.string()), "Parameter names (LIST<VARCHAR>)."),
        afield("required_parameters", pa.list_(pa.string()), "Required parameter names (LIST<VARCHAR>)."),
    ]
)


@dataclass(slots=True, frozen=True)
class _MethodsArgs:
    api: Annotated[str, Arg(0, doc="Discovery API name, e.g. 'drive'.")]
    version: Annotated[str, Arg(1, doc="API version, e.g. 'v3'.")]


@init_single_worker
@bind_fixed_schema
class GoogleMethodsFunction(TableFunctionGenerator[_MethodsArgs, _ScanState]):
    """List the callable methods of one Google API (one row per method)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = _METHODS_SCHEMA

    class Meta:
        """Function metadata."""

        name = "google_methods"
        description = "List the callable methods of a Google API (for google_call)"
        categories = ["google", "discovery", "metadata"]
        examples = [
            FunctionExample(
                sql="SELECT method, http_method FROM google_methods('drive', 'v3')",
                description="List Drive v3 methods reachable via google_call",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[_MethodsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=100, max=2000)

    @classmethod
    def initial_state(cls, params: ProcessParams[_MethodsArgs]) -> _ScanState:
        """Create the initial scan state."""
        return _ScanState()

    @classmethod
    def process(cls, params: ProcessParams[_MethodsArgs], state: _ScanState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        if state.done:
            out.finish()
            return
        a = params.args
        try:
            doc = discovery.fetch_discovery_doc(a.api, a.version)
            items = discovery.list_methods(doc)
        except discovery.GoogleApiError as exc:
            raise RuntimeError(f"google_methods({a.api}:{a.version}) failed: {exc}") from exc
        state.done = True
        if items:
            out.emit(rows_to_batch(items, params.output_schema))
        out.finish()


TABLE_FUNCTIONS: list[type] = [
    GoogleSheetFunction,
    GoogleDriveFunction,
    GoogleCalendarFunction,
    GoogleYouTubeFunction,
    GoogleCallFunction,
    GoogleApisFunction,
    GoogleMethodsFunction,
]

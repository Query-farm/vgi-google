# CLAUDE.md — vgi-google

Guidance for working in this repo. vgi-google is a **VGI worker** (Python) that
queries **Google APIs from DuckDB/SQL**: a discovery-driven core, curated table
adapters (Sheets / Drive / Calendar / YouTube), and a generic `google_call`
escape hatch. It is an **egress / commodity** connector — the value is upstream
in Google's APIs (quotas/billing), not in the worker; the durable cleverness is
the discovery-driven breadth, not a moat. Keep the README's honest framing
intact.

## What this is

- A VGI worker on the `vgi-python` SDK, launched by DuckDB (`ATTACH ... (TYPE vgi,
  LOCATION '...')`) as a stdio subprocess, or over HTTP via `serve.py`.
- The SQL surface is seven **table functions** in the `google` catalog assembled
  in `google_worker.py`: `google_sheet`, `google_drive`, `google_calendar`,
  `google_youtube`, `google_call`, `google_apis`, `google_methods`.
- v1 is **READ only**. Sheets write and the OAuth-user flow are documented
  follow-ups, NOT built.

## Layout

- `vgi_google/discovery.py` — the discovery-backed client factory
  (`build_service` via `google-api-python-client`'s `build` / `build_from_document`),
  generic dotted-method traversal (`resolve_method`), `execute` (timeout +
  bounded 429/5xx/quota retry), and discovery introspection (`list_apis`,
  `fetch_discovery_doc`, `list_methods`).
- `vgi_google/auth.py` — credential resolution (service-account-first, then API
  key) via the VGI secret provider; env hatches for tests only.
- `vgi_google/client.py` — `build_client(api, version, secrets, scopes)` ties auth
  + discovery together, and holds the **test seam** `set_http_factory`.
- `vgi_google/adapters/*` — one curated adapter per API (`api`/`version`/`scopes`/
  `schema` + `fetch_page(service, args, cursor) -> Page`).
- `vgi_google/tables.py` — the table functions; `_run_paged_tick` is the shared
  one-page-per-tick driver; `_ScanState` is the serializable pagination cursor.
- `vgi_google/schema_utils.py` — `afield` / `json_field` / `rows_to_batch` and
  the pinned `TIMESTAMPTZ` / `LIST_VARCHAR` types + `parse_*` helpers.

## VGI conventions that matter here

- **Table functions take `name := value` named args; scalars are positional-only.**
  Every function here is a table function. Positional args CANNOT have a default
  (DuckDB always binds them) — `google_call`'s `params_json` is positional and
  required; everything optional is a named `Arg("name", default=...)`.
- **LIST / TIMESTAMPTZ / JSON returns REQUIRE explicit `arrow_type`.** Pinned once
  in `schema_utils.py`. JSON columns are plain string Arrow fields tagged with
  `metadata={b"logical_type": b"JSON"}`; read them in SQL with the `json`
  extension's `->>` (the `.test` file `LOAD json;` first).
- **Do NOT request secrets in `on_bind`.** Using `@bind_fixed_schema` AND calling
  `params.secrets.get(...)` in a custom `on_bind` triggers a two-phase bind retry
  that returns an EMPTY output schema (every batch comes back 0-row). Resolve
  secrets lazily in `process()` via `params.secrets` instead — `auth.resolve`
  tolerates a missing/empty accessor. (`google_call` keeps an `on_bind` only to
  validate `params_json`, and does not touch secrets there.)
- **Scan state must be serializable.** `_ScanState` extends
  `ArrowSerializableDataclass`; the framework round-trips Google's
  `nextPageToken` across `process()` ticks (and HTTP requests). One page per
  tick; `count` caps total rows.
- **Pin paged functions to one worker.** Every paged table function is
  `@init_single_worker`. Without it, parallel scan instances each re-emit the
  whole result and DUPLICATE rows (the bug that bit vgi-search / vgi-wikipedia).
  `tests/test_mock_e2e.py::test_single_worker_pin_prevents_duplication` documents
  why.
- **Never crash the worker.** `discovery.execute` raises `GoogleApiError`;
  `auth.resolve` raises `AuthError`; `process()` converts both (and `ValueError`)
  into a clean `RuntimeError` → DuckDB error.

## The discovery / test seam

`build_from_document` builds a real client from a static discovery doc, and an
injectable `httplib2`-style transport serves canned responses — so the SAME
adapter code runs in tests as live. `client.set_http_factory(factory)` installs
`(api, version) -> (http, discovery_doc)`; tests use `tests/mock_google.py`'s
`http_factory`. The worker subprocess enables it via `VGI_GOOGLE_MOCK=1` (see
`google_worker._maybe_install_mock`). `google_apis`/`google_methods` read static
docs from `VGI_GOOGLE_DISCOVERY_DIR`.

Note: `execute` json-decodes any raw bytes/str body, so adapters get a dict even
when a minimal discovery doc omits a method's `response` schema.

## Auth

Service-account-first, via `params.secrets` only — never inline. Secret types:
`google_service_account` (JSON key; optional `subject` for delegation, `scopes`
to narrow) and `google_api_key`. Each adapter declares least-privilege `scopes`
(documented in the README table). Env hatches `VGI_GOOGLE_SERVICE_ACCOUNT_FILE` /
`VGI_GOOGLE_API_KEY` are for tests/local only.

## Adding a curated adapter

1. `vgi_google/adapters/<name>.py`: a class with `api`/`version`/`scopes`/`schema`
   and `fetch_page(service, args, cursor) -> Page` mapping the API response to
   dict rows + `next_cursor` (Google's `nextPageToken`).
2. Register it in `adapters/__init__.py`; add a `Google<Name>Function` in
   `tables.py` (`@init_single_worker @bind_fixed_schema`, a `_<Name>Args`
   dataclass, `process` delegating to `_run_paged_tick`) and to `TABLE_FUNCTIONS`.
3. Add a discovery doc to `tests/mock_google.py` `DISCOVERY_DOCS` + canned
   responses, a parser test in `tests/test_adapters.py`, and `.test` coverage.

## Testing (NO live Google in the CI gate)

- `make test-unit` / `pytest` — parser tests (`test_adapters.py`), auth
  (`test_auth.py`), discovery (`test_discovery.py`), mock E2E (`test_mock_e2e.py`,
  incl. the pageToken round-trip + single-worker-pin proofs).
- `make test-sql` — the real worker subprocess under DuckDB via `haybarn-unittest`,
  launched with `VGI_GOOGLE_MOCK=1` by `scripts/run_sql_e2e.py`. The `.test` file
  uses `LOAD vgi;` (NEVER `require vgi`), `require-env VGI_GOOGLE_WORKER`, ATTACHes
  `${VGI_GOOGLE_WORKER}`, and globs `test/sql/*`.
- `make lint` (ruff) + `make typecheck` (mypy `vgi_google`) must be clean.
- Live smoke (`tests/test_live_smoke.py`) is GATED by `VGI_GOOGLE_LIVE=1`, not in
  the CI gate.

When changing pagination or a schema, re-run BOTH `make test-unit` and
`make test-sql` — the scan-state round-trip is asserted in both.

## Licensing

Worker code is MIT (`LICENSE`). `google-api-python-client` and `google-auth` are
Apache-2.0; httplib2 / google-auth-httplib2 are permissive. No GPL/AGPL. Quotas,
billing and Google's ToS are the user's responsibility.

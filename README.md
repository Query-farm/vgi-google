<p align="center">
  <img src="https://raw.githubusercontent.com/Query-farm/vgi/main/docs/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

<p align="center"><em>A <a href="https://query.farm">Query.Farm</a> VGI worker for DuckDB.</em></p>

# vgi-google

A [VGI](https://github.com/query-farm/vgi-python) worker that queries **Google
APIs from DuckDB/SQL**. A discovery-driven core (works against ~any Google REST
API via Google's first-party discovery documents and
[`google-api-python-client`](https://github.com/googleapis/google-api-python-client)),
**curated table adapters** for the high-demand "as SQL" APIs (Sheets, Drive,
Calendar, YouTube), and a **raw escape hatch** for the long tail. One worker,
because the hard part — Google auth — is shared.

```sql
INSTALL vgi FROM community; LOAD vgi;
ATTACH 'google' (TYPE vgi, LOCATION 'uv run google_worker.py');

-- Sheets: a range as a table (service account must have access, or use an API key for a public sheet)
SELECT * FROM google.google_sheet('1AbC...', 'Sheet1!A1:Z', header := true);

-- Drive: list/search files as rows
SELECT id, name, mime_type, modified_time, size, web_view_link
FROM google.google_drive(query := "mimeType='application/pdf' and trashed=false", count := 100);

-- Calendar: events as rows
SELECT id, summary, start_time, end_time, status, organizer
FROM google.google_calendar(calendar_id := 'primary', time_min := '2026-01-01', count := 250);

-- YouTube Data: search/list (API key)
SELECT video_id, title, channel_title, published_at, view_count
FROM google.google_youtube('duckdb', count := 50);

-- Generic escape hatch for any other Google API
SELECT * FROM google.google_call('gmail', 'v1', 'users.messages.list', '{"userId":"me"}');

-- Discovery: what is reachable?
SELECT name, version, title FROM google.google_apis() WHERE name LIKE '%calendar%';
SELECT method, http_method FROM google.google_methods('drive', 'v3');
```

The curated adapters are **table functions**, so they accept DuckDB's
`name := value` arguments. They page Google as needed to satisfy `count`,
carrying Google's `nextPageToken` as externalized scan state.

## Honest framing (read this first)

This is an **egress / commodity connector**. The value lives in the **upstream
Google APIs** — their data, their quotas, their billing — not in this worker, and
your data **leaves the engine** when you call out (note for data-residency).
It is **not a moat**: it is useful glue and breadth.

What is genuinely good here is the **discovery-driven design**. Google ships
first-party, high-quality discovery documents for nearly all its REST APIs, and
`google-api-python-client` builds a working client for ANY of them at runtime
from those docs. So the generic path is sound (unlike OpenAPI heuristics over a
random REST API): broad reach, low maintenance. That breadth — plus a clean
"Sheets/Drive/Calendar-as-SQL" experience that has real pull — is the durable
cleverness, not a defensibility story. Same bucket as `vgi-search` and `vgi-hf`.

"Generic" means *broad reach via discovery*, not *raw method calls as the primary
UX* (that is clunky). The curated adapters are the good UX; `google_call` is the
escape hatch for everything else.

## Surface (v1 — READ only)

| Function | Returns | Auth | Pagination |
| --- | --- | --- | --- |
| `google_sheet(spreadsheet_id, range, header := false, count := 10000)` | values as rows: `row_number`, `values` (LIST), `record` (JSON when `header`) | service account, or API key (public sheet) | single-shot (range-bounded) |
| `google_drive(query := '', count := 100, page_size, order_by, drive_id)` | file rows (id, name, mime_type, times, size, parents LIST, owner, …) | service account | `pageToken` |
| `google_calendar(calendar_id := 'primary', time_min, time_max, query, count := 250, …)` | event rows (summary, start/end TIMESTAMPTZ, status, organizer, …) | service account | `pageToken` |
| `google_youtube(query, count := 25, page_size, order)` | video rows enriched with view/like/comment counts + duration | API key | `pageToken` |
| `google_call(api, version, method, params_json)` | one JSON row per response item (list responses expanded), follows `nextPageToken` | service account / API key | `pageToken` |
| `google_apis(name := '')` | reachable APIs (name, version, title, preferred) | none | — |
| `google_methods(api, version)` | callable methods of an API (dotted path, params) | none | — |

Nested/object fields that have no flat column are exposed as a JSON `extra`
column. Timestamps are `TIMESTAMPTZ` (UTC); repeated fields are `LIST<VARCHAR>`.
Missing fields become SQL `NULL`.

`count` caps total rows; `page_size` tunes the per-request page. The paged
functions are **pinned to a single worker** so a parallel scan cannot re-emit and
duplicate rows.

## Auth (service-account-first) — via the secret provider, never inline

Credentials are resolved through the VGI **secret provider**, in priority order:

1. **Service account (JSON key)** — the **default** and natural server-side fit:
   no interactive consent; accesses exactly what the account is granted (a shared
   Sheet/Drive, or a Workspace domain via delegation). Create a
   `google_service_account` secret whose value carries the key JSON (a `key_json`
   string, or the raw service-account object). Optional `subject` enables
   domain-wide delegation; optional `scopes` narrows the grant.
2. **API key** — for **public-data** APIs (YouTube public, public Sheets, Maps).
   Create a `google_api_key` secret with an `api_key` value.
3. **OAuth2 user (3-legged refresh token)** — for **personal** data (a user's
   own Gmail/Drive). This is the harder, authorization-sensitive path. **It is a
   documented follow-up, NOT built in v1** — see [Roadmap](#roadmap). If you ever
   front it with `vgi-cache`, scope the cache **per principal** or you risk a
   cross-user data leak.

For local development and the test suite there are two env escape hatches
(developer convenience only — production should use the secret provider):
`VGI_GOOGLE_SERVICE_ACCOUNT_FILE` (path to a key file) and `VGI_GOOGLE_API_KEY`.

### Required OAuth scopes (per adapter, least-privilege)

| Adapter | Scope | Notes |
| --- | --- | --- |
| `google_sheet` | `spreadsheets.readonly` | share the sheet with the service-account e-mail; a *public* sheet works with an API key |
| `google_drive` | `drive.metadata.readonly` | share the files/Shared Drive with the service-account e-mail (or use delegation) |
| `google_calendar` | `calendar.readonly` | share the calendar with the service-account e-mail (or delegate for a user's `primary`) |
| `google_youtube` | `youtube.readonly` *(or just an API key)* | public search/list needs only an API key; the scope is for the OAuth-user path |

**Quotas, billing, and Terms of Service are your responsibility.** A
`google_youtube` search costs 100 units against your YouTube Data quota; Sheets,
Drive and Calendar have their own per-minute and daily limits. Quota / rate-limit
errors are retried with backoff and then surfaced as a clean DuckDB error — they
never crash the worker.

## Out of scope (don't rebuild)

- **BigQuery / GCS** — DuckDB reads GCS via `httpfs`; BigQuery is commodity
  warehouse-bridging.
- **Maps geocoding** — overlaps `vgi-geocode`; paid. Reach it via `google_call`
  if you must.
- **Gemini** — that is the `vgi-llm` / `vgi-hf` bucket.

These remain reachable through the generic `google_call` hatch where it makes
sense, but they are not curated adapters.

## Reliability

Every call has a **per-request timeout** and **bounded retry with exponential
backoff** on `429` / `5xx` and Google's `rateLimitExceeded` / `quotaExceeded`
reasons (honoring the status). Discovery, transport, HTTP, auth and quota
failures all become a clean `RuntimeError` (a DuckDB error) — the worker never
crashes.

## Install / run

```sh
# stdio (DuckDB subprocess) — the PEP-723 header pins the deps for `uv run`
uv run google_worker.py

# HTTP
python serve.py --port 8000
```

The worker depends on `google-api-python-client` and `google-auth` (both
Apache-2.0) plus `vgi-python`.

## Development & testing

```sh
make test        # pytest (parsers + mock E2E) + haybarn SQL E2E (mock transport)
make test-unit   # pytest only
make test-sql    # haybarn SQL E2E only
make lint        # ruff
make typecheck   # mypy
```

There is **no live Google in the CI gate**. Tests use
`google-api-python-client`'s injectable transport with static discovery documents
to serve canned, paginated responses deterministically, and bypass
service-account auth. The centerpiece test proves the `pageToken` scan state
round-trips across a batch boundary (every row exactly once, no dupes/drops), and
a companion test demonstrates why the single-worker pin prevents duplication.

An **optional, gated live smoke** suite (`tests/test_live_smoke.py`) runs only
when `VGI_GOOGLE_LIVE=1` and you provide real credentials — it is **not** part of
the CI gate.

## Roadmap

- **Sheets write** (buffer a query result → a sheet range).
- **OAuth2-user flow** (login-once + refresh token) for personal data, with
  per-principal scoping helpers for safe `vgi-cache` fronting.
- **GA4 / Search Console** adapters (real marketing-data demand).

## Licensing

Worker code is **MIT** (see `LICENSE`). Runtime dependencies
`google-api-python-client` and `google-auth` are **Apache-2.0**; `httplib2` /
`google-auth-httplib2` are permissive. **No GPL/AGPL.** Your use of the Google
APIs themselves is governed by Google's terms, quotas and billing.

---

## Authorship & License

Written by [Query.Farm](https://query.farm) — every VGI worker is designed and built by Query.Farm.

Copyright 2026 Query Farm LLC - https://query.farm


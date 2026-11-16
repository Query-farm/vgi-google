<p align="center">
  <img src="https://query.farm/media-kit/products/vgi/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

<p align="center"><em>A <a href="https://query.farm">Query.Farm</a> VGI worker for DuckDB.</em></p>

# Query Google APIs — Sheets, Drive, Calendar, YouTube & Workspace Admin in DuckDB

> **vgi-google** · a [Query.Farm](https://query.farm) VGI worker

A [VGI](https://github.com/query-farm/vgi-python) worker that queries **Google
APIs from DuckDB/SQL**. A discovery-driven core (works against ~any Google REST
API via Google's first-party discovery documents and
[`google-api-python-client`](https://github.com/googleapis/google-api-python-client)),
**curated table adapters** for the high-demand "as SQL" APIs (Sheets, Drive,
Calendar, YouTube), and a **raw escape hatch** for the long tail. One worker,
because the hard part — Google auth — is shared.

```sql
INSTALL vgi FROM community; LOAD vgi;
ATTACH 'google' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-google@v0.4.3 vgi-google-worker');

-- Sheets: a range as a table (service account must have access, or use an API key for a public sheet)
SELECT * FROM google.google_sheet('1AbC...', 'Sheet1!A1:Z', header := true);

-- Drive: list/search files as rows
SELECT id, name, mime_type, modified_time, size, web_view_link
FROM google.google_drive(query := "mimeType='application/pdf' and trashed=false", count := 100);

-- Calendar: events as rows
SELECT id, summary, start_time, end_time, status, organizer
FROM google.google_calendar(calendar_id := 'primary', time_min := '2026-01-01', count := 250);

-- Compose adapters: Drive PDFs not yet recorded in a tracking sheet's file_id column
SELECT f.id, f.name, f.modified_time
FROM google.google_drive(query := "mimeType='application/pdf' and trashed=false", count := 500) AS f
ANTI JOIN google.google_sheet('1AbC...', 'Log!A1:B', header := true) AS l
  ON l.record ->> '$.file_id' = f.id
ORDER BY f.modified_time DESC;

-- YouTube Data: search/list (API key)
SELECT video_id, title, channel_title, published_at, view_count
FROM google.google_youtube('duckdb', count := 50);

-- Generic escape hatch for any other Google API
SELECT * FROM google.google_call('gmail', 'v1', 'users.messages.list', '{"userId":"me"}');

-- Google Workspace admin data (schema `workspace`): WHERE predicates are pushed to the API
SELECT time, actor_email, ip_address
FROM google.workspace.activities('login')
WHERE event_name = 'login_failure' AND time >= TIMESTAMPTZ '2026-11-01 00:00:00+00';

-- Discovery: what is reachable?
SELECT name, version, title FROM google.google_apis() WHERE name LIKE '%calendar%';
SELECT method, http_method FROM google.google_methods('drive', 'v3');
```

The curated adapters are **table functions**, so they accept DuckDB's
`name := value` arguments. They page Google as needed to satisfy `count`,
carrying Google's `nextPageToken` as externalized scan state.

## Quick start

The worker isn't on PyPI; [uv](https://docs.astral.sh/uv/) runs it straight from a tagged release on GitHub. `uvx` builds an isolated environment on the first ATTACH and reuses it afterwards:

```sql
INSTALL vgi FROM community;
LOAD vgi;

ATTACH 'google' AS google (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-google@v0.4.3 vgi-google-worker');

-- Discover the Sheets API's reachable versions via the Discovery Service.
SELECT name, version, title FROM google.main.google_apis() WHERE name LIKE '%sheets%' ORDER BY name LIMIT 5;

-- Browse the Drive v3 methods you can invoke through google_call.
SELECT method, http_method FROM google.main.google_methods('drive', 'v3') ORDER BY method LIMIT 5;
```

Every adapter needs Google credentials, supplied through a DuckDB secret: a
`google_service_account` secret (service-account key JSON) for shared Sheets,
Drive and Calendar data, or a `google_api_key` secret for public-data APIs such
as YouTube search. See [Auth](#auth-service-account-first--via-the-secret-provider-never-inline)
for the details and the required scopes.

Change `@v0.4.1` to move to a newer release.

### Other ways to run it

- **Docker** (the image bundles the worker and its dependencies):

  ```sql
  ATTACH 'google' AS google (TYPE vgi, LOCATION 'docker://ghcr.io/query-farm/vgi-google:0.4.1');
  ```

- **From a local checkout** (`git clone https://github.com/Query-farm/vgi-google`), point `LOCATION` at `uv run --project /path/to/vgi-google vgi-google-worker`.

### Build from source

To run the worker from a checkout (stdio or HTTP), see
[Running from a checkout](#running-from-a-checkout); for the test suite, see
[Development & testing](#development--testing).

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
| `google_youtube(query, count := 25, page_size, order_by)` | video rows enriched with view/like/comment counts + duration | API key | `pageToken` |
| `google_call(api, version, method, params_json)` | one JSON row per response item (list responses expanded), follows `nextPageToken` | service account / API key | `pageToken` |
| `google_apis(name := '')` | reachable APIs (name, version, title, preferred) | none | — |
| `google_methods(api, version)` | callable methods of an API (dotted path, params) | none | — |

Nested/object fields that have no flat column are exposed as a JSON `extra`
column. Timestamps are `TIMESTAMPTZ` (UTC); repeated fields are `LIST<VARCHAR>`.
Missing fields become SQL `NULL`.

`count` caps total rows; `page_size` tunes the per-request page. The paged
functions are **pinned to a single worker** so a parallel scan cannot re-emit and
duplicate rows.

## Google Workspace admin data (`google.workspace`)

The `workspace` schema reads a Workspace tenant's administrative data as typed
tables: the **Admin SDK Directory** and **Reports** APIs, the **Alert Center**
API and the **Groups Settings** API. Everything is READ-only.

| Function | Rows | API method | Read-only scope (`https://www.googleapis.com/auth/…`) |
| --- | --- | --- | --- |
| `users(customer, domain, query, show_deleted, order_by, projection)` | user accounts | `users.list` | `admin.directory.user.readonly` |
| `groups(customer, domain, user_key, query)` | groups (or one user's groups) | `groups.list` | `admin.directory.group.readonly` |
| `group_members(group_key, roles, include_derived_membership)` | members of one group | `members.list` | `admin.directory.group.member.readonly` |
| `org_units(customer, org_unit_path, type)` | organizational units | `orgunits.list` | `admin.directory.orgunit.readonly` |
| `domains(customer)` | domains + alias domains | `domains.list` | `admin.directory.domain.readonly` |
| `roles(customer)` | admin roles; `privileges` is `LIST<STRUCT>` | `roles.list` | `admin.directory.rolemanagement.readonly` |
| `role_assignments(customer, role_id, user_key)` | who holds which role | `roleAssignments.list` | `admin.directory.rolemanagement.readonly` |
| `chromeos_devices(customer, query, org_unit_path)` | ChromeOS devices | `chromeosdevices.list` | `admin.directory.device.chromeos.readonly` |
| `mobile_devices(customer, query)` | mobile devices | `mobiledevices.list` | `admin.directory.device.mobile.readonly` |
| `activities(application, user_key, start_time, end_time, event_name, actor_ip_address, filters, org_unit_id, group_id_filter)` | audit log, **one row per event**; `parameters` is a JSON object | `activities.list` | `admin.reports.audit.readonly` |
| `usage_reports(report, date, parameters, user_key, filters, org_unit_id, customer_id)` | daily usage, one row per (entity, date, metric) | `customerUsageReports.get` / `userUsageReport.get` | `admin.reports.usage.readonly` |
| `alerts(filter, order_by, customer_id)` | Alert Center alerts | `alerts.list` | `apps.alerts` |
| `group_settings(group_email)` | one group's access/moderation settings | Groups Settings `groups.get` | `apps.groups.settings` |

`activities` takes any Reports application: `login`, `admin`, `drive`, `token`,
`saml`, `groups`, `calendar`, `meet`, `mobile`, `chrome`, `user_accounts`,
`context_aware_access`, `vault`, … (the full list is the argument's declared
choices). Every function also takes `count` (row cap) and, where the API pages,
`page_size`.

### What pushes down

The functions declare filter pushdown: DuckDB hands the worker the `WHERE`
predicates, the worker translates what the API can express into its own
server-side filters, and then **re-applies the exact SQL predicate** to every
batch, so a partly-pushable `WHERE` is still correct. Pushed filters are always
a superset of the SQL predicate (time bounds are widened by a second; prefix /
org-unit-chain matches are broader than equality).

| Function | SQL predicate | Becomes |
| --- | --- | --- |
| `activities` | `time >=`, `>`, `<`, `<=`, `BETWEEN` | `startTime` / `endTime` |
| | `event_name = …` | `eventName` |
| | `actor_email = …` | the `userKey` path segment (when `user_key` is `'all'`) |
| | `ip_address = …` | `actorIpAddress` |
| `usage_reports` | `date =` / `IN` / range | the report days requested (one request per day, ≤ 92 days) |
| | `parameter =` / `IN` (`app:metric` names) | `parameters` |
| | `user_email = …` | `userKey` (user report) |
| `users` | `primary_email = …` | search clause `email='…'` (also matches aliases) |
| | `org_unit_path = …` | `orgUnitPath='…'` (also matches sub-units) |
| | `is_admin`, `is_delegated_admin`, `suspended`, `archived`, `is_enrolled_in_2sv`, `is_enforced_in_2sv` (bare, `NOT`, `= true/false`) | `isAdmin=true` … |
| `groups` | `email = …`, `name = …` | search clauses (not combined with `user_key`) |
| `group_members` | `role =` / `IN` | `roles` |
| `role_assignments` | `role_id = …` / `assigned_to = …` | `roleId` / `userKey` (one per request) |
| `chromeos_devices` | `org_unit_path = …` | `orgUnitPath` |
| `alerts` | bounds on `create_time` / `start_time`; `type` / `source` `=` or `IN` | an Alert Center `filter` expression, AND-ed with the `filter` argument |

`count` caps the rows fetched from the API, i.e. before any predicate the API
could not apply. `usage_reports` needs a day: pass `date := 'YYYY-MM-DD'` or
filter the `date` column.

### Auth: service account with domain-wide delegation

Workspace admin data is read **as an administrator**. Use a service account
with [domain-wide delegation](https://support.google.com/a/answer/162106) and
set the secret's `subject` to an admin's e-mail (or grant the service account
an admin role directly and omit `subject`):

1. Create a service account and a JSON key in Google Cloud; enable the Admin
   SDK API (plus the Alert Center / Groups Settings APIs if you use them).
2. In the Admin console (Security → API controls → Domain-wide delegation), add
   the service account's client ID with the read-only scopes from the table
   above for the functions you use. Each function requests only its own scope.
3. Create the secret:

```sql
CREATE SECRET workspace_admin (
    TYPE google_service_account,
    key_json '{"type": "service_account", ...}',
    subject 'admin@example.com'
);

SELECT primary_email, last_login_time
FROM google.workspace.users()
WHERE is_admin AND NOT is_enrolled_in_2sv;

-- Who holds which admin role
SELECT r.role_name, u.primary_email, a.scope_type
FROM google.workspace.role_assignments() AS a
JOIN google.workspace.roles() AS r USING (role_id)
LEFT JOIN google.workspace.users() AS u ON u.id = a.assigned_to;

-- Daily e-mails sent per user over a week
SELECT date, user_email, int_value AS emails_sent
FROM google.workspace.usage_reports(report := 'user', parameters := 'gmail:num_emails_sent')
WHERE date BETWEEN DATE '2026-11-01' AND DATE '2026-11-07';

-- High-severity alerts this month, and a group's external-member setting
SELECT create_time, type, status FROM google.workspace.alerts()
WHERE create_time >= TIMESTAMPTZ '2026-11-01 00:00:00+00' AND severity = 'HIGH';
SELECT who_can_join, allow_external_members FROM google.workspace.group_settings('eng@example.com');
```

A `401`/`403`/token error from these functions carries a hint naming the scope
to authorize. Reports data lags by hours (activities) to days (usage), and the
audit log keeps about 180 days. Anything the curated tables don't cover (data
transfers, tokens, Chrome printers, …) stays reachable through
`google_call('admin', 'directory_v1', …)`.

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
reasons, honoring a `Retry-After` header when Google sends one (capped at 60 s). Discovery, transport, HTTP, auth and quota
failures all become a clean `RuntimeError` (a DuckDB error) — the worker never
crashes.

## Running from a checkout

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
make test-sql    # haybarn SQL E2E only (stdio, launch, http)
make lint        # ruff + mypy + pydoclint
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

Written by [Query.Farm](https://query.farm).

Copyright 2026 Query Farm LLC - https://query.farm


## Executed notebooks

See the [Drive and Sheets reconciliation walkthrough](notebooks/README.md) for a portable, credential-free synthetic example through the real Google worker.

# CI: the vgi-google worker integration suite

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the unit tests
and this repo's sqllogictest suite (`test/sql/*.test`) against the vgi-google
VGI worker through the **real DuckDB `vgi` extension** on every push / PR.

## How it works (no C++ build)

Rather than building the vgi DuckDB extension from source, CI drives a
**prebuilt** standalone `haybarn-unittest` (the DuckDB/Haybarn sqllogictest
runner, published in Haybarn's releases) and installs the **signed** `vgi`
extension from the Haybarn community channel:

1. **Install the worker** — `uv sync --frozen --extra http` into a venv.
   `google_worker.py` is a self-contained PEP 723 stdio worker the extension can
   spawn via `uv run google_worker.py`.
2. **Download the runner** — the matching `haybarn_unittest-*` asset per platform
   from the latest Haybarn release.
3. **Preprocess** — [`preprocess-require.awk`](preprocess-require.awk) rewrites
   each `require <ext>` into an explicit signed `INSTALL <ext> FROM
   {community,core}; LOAD <ext>;`, and injects `INSTALL vgi FROM community;`
   before each bare `LOAD vgi;` (haybarn silently SKIPs `require vgi`, so the
   tests `LOAD vgi;` directly). `require-env` and everything else pass through.
4. **Run** — [`run-integration.sh`](run-integration.sh) stages the preprocessed
   tree, resolves `VGI_GOOGLE_WORKER` (the ATTACH `LOCATION`) per `$TRANSPORT`,
   warms the extension cache once, then runs the suite in a single
   `haybarn-unittest` invocation. Any failed assertion fails the job.

## Offline mocking (all transports)

The worker is run with **`VGI_GOOGLE_MOCK=1`**, which installs the canned test
transport (`tests/mock_google.py`): its adapters build from static discovery
docs and are served deterministic, paginated responses — no live Google, no
credentials. `run-integration.sh` also builds a temp directory of static
discovery artifacts (`apis.list.json` + `drive.v3.json` from
`tests.mock_google.DRIVE_DOC`) and exports it via **`VGI_GOOGLE_DISCOVERY_DIR`**
so `google_apis` / `google_methods` resolve offline too.

Crucially, both env vars are **exported by the script** before it boots the
worker, so they are inherited by the worker process for **every transport**
(subprocess, http, unix). No separate mock server is needed — the mock lives
inside the worker itself. The authoritative SQL suite therefore drives the
*real* worker end to end (real ATTACH, real bind/init/process, real pageToken
scan-state round-trips) against deterministic fixtures, with no keys, no cost,
and no real network egress.

## Transport matrix (subprocess | http | unix)

The same `test/sql/*.test` suite is run over all three VGI transports — the
extension picks the transport from the `LOCATION` string the `.test` files
`ATTACH`, and `run-integration.sh` builds that string from `$TRANSPORT`:

| `TRANSPORT`  | `VGI_GOOGLE_WORKER` (LOCATION)            | How the worker is reached |
|--------------|-------------------------------------------|---------------------------|
| `subprocess` | `.venv/bin/python google_worker.py`       | extension spawns the worker per query; Arrow IPC over stdin/stdout (default) |
| `http`       | `http://127.0.0.1:<port>`                 | harness boots `google_worker.py --http --port 0 --port-file <f>`, waits for the port-file, then ATTACHes that URL |
| `unix`       | `unix:///tmp/google-<pid>.sock`           | harness boots `google_worker.py --unix <sock>`, waits for the socket, then ATTACHes it |

The CI `integration` job is a `transport: [subprocess, http, unix]` × `os`
matrix; each leg runs `ci/run-integration.sh` with `TRANSPORT=<t>`. Run a single
transport locally with e.g. `TRANSPORT=http ci/run-integration.sh`.

### Port / readiness discovery

- **http**: the worker writes its auto-selected port to `--port-file`
  atomically, so the harness watches for that file (not stdout). Boot line:
  `google_worker.py --http --port 0 --port-file <f>`.
- **unix**: the worker binds the socket and prints `UNIX:<abs-path>`; the
  harness polls for the socket file (`test -S`). Boot line:
  `google_worker.py --unix <sock>`.

Both out-of-band server processes run with cwd = the repo root (so the worker
resolves `tests.mock_google` / `vgi_google`), inherit the
`VGI_GOOGLE_MOCK`/`VGI_GOOGLE_DISCOVERY_DIR` exports, and are trap-killed on exit.

### HTTP transport needs the `httpfs` extension (resolved, not gated)

The vgi extension implements HTTP transport on top of DuckDB's **httpfs**
extension, so an `http://` ATTACH binds with `VGI HTTP transport requires the
httpfs extension` unless httpfs is loaded first. This is a **dependency**, not a
protocol limitation, so we resolve it: the http leg injects a signed `INSTALL
httpfs FROM core; LOAD httpfs;` into each staged `.test` (after the awk-injected
`LOAD vgi;`). The leg also needs the worker's `http` extra (waitress) —
`pyproject.toml` ships an `http` extra (`vgi-python[http]`), the PEP 723 header
in `google_worker.py` lists it, and CI runs `uv sync --frozen --extra http`.

> **Sharp edge — the runner silently SKIPs HTTP errors.** The haybarn/DuckDB
> sqllogictest runner's default skip list skips any statement whose error
> contains `"HTTP"` or `"Unable to connect"`, so a broken http setup reports
> "All tests were skipped" — a green-looking **fake pass**.
> `run-integration.sh` fails the leg unless the runner reports `All tests passed
> (N assertions …)` with N > 0 and zero skips.

### Pagination over HTTP (externalized cursor — no gate)

The curated table functions (`google_drive`, `google_calendar`,
`google_youtube`, `google_sheet`) and the generic `google_call` are
streaming/paging: each tick fetches ONE Google page (following
`nextPageToken`), emits it, and advances. Streaming table functions run fine
over the **stateless** HTTP transport **because the cursor is externalized**:
the per-scan position lives in a plain-serializable
`_ScanState(ArrowSerializableDataclass)` (`cursor` = Google's `nextPageToken`,
plus `emitted`/`started`/`done`) that the framework round-trips through its
continuation token on every `process()` tick — and so across batch boundaries
under HTTP. So the http leg runs the **full** suite including the centerpiece
pageToken round-trip (`google_drive(count := 5, page_size := 1)` returning five
distinct `file0..file4` rows across five paged ticks) — nothing is gated. (This
is the same "externalize the scan position into the serialized state" pattern as
the vgi-cve cursor fix; the paged functions are also `@init_single_worker`, so
parallel scan instances never re-emit and duplicate rows.)

### Per-transport status

- **subprocess**: GREEN — 31 assertions.
- **http**: GREEN — 33 assertions (31 + the injected httpfs INSTALL/LOAD). Full
  suite incl. the pageToken scan-state round-trip across page boundaries.
- **unix**: GREEN — 31 assertions.

## Run it locally

```bash
uv sync --python 3.13 --extra http
HAYBARN_UNITTEST=/path/to/haybarn-unittest \
WORKER_CMD="uv run --python 3.13 google_worker.py" \
  TRANSPORT=subprocess ci/run-integration.sh    # or TRANSPORT=http / TRANSPORT=unix
```

`TRANSPORT` defaults to `subprocess`, and `WORKER_CMD` defaults to
`uv run --python 3.13 <repo>/google_worker.py`.

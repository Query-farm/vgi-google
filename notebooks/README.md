# Executed Drive / Sheets reconciliation

[Which older files are absent from the tracking sheet?](01_reconcile_drive_tracking.ipynb) joins a paginated synthetic PDF listing to a synthetic tracking range through the real Google worker. It deduplicates trimmed keys, reports keys absent from listing scope, preserves missing dates/sizes, and varies an elapsed-age cutoff. SQL performs reconciliation and calculations; Python handles configuration and presentation.

## Install and run from this directory

Requirements: [uv](https://docs.astral.sh/uv/), Git, Python 3.13/3.14 and network access on first install. No Google account, API key, service account, quota or global kernel is needed. Copy this whole directory to run without the worker source checkout.

```sh
cd notebooks
uv sync --frozen
uv run --frozen python execute.py --timeout 240
```

The separate lock pins published Haybarn 2.0.0rc11. A temporary kernel uses the runner's interpreter with notebook-directory cwd. Outputs are saved atomically only after success. Failed runs leave the successful notebook unchanged; root dependencies and locks are untouched.

For interactive editing:

```sh
uv run --frozen python -m ipykernel install --user --name vgi-google-notebooks --display-name 'Python (Google notebooks)'
```

Use that kernel, keep cwd here, edit the visible parameter, restart and run all. Python helpers and plot source are folded; parameters and SQL remain expanded. Official branding is local and unchanged.

## Focused age experiment

`MIN_AGE_DAYS=30` accepts integer days 0–365. An unlogged file qualifies at or beyond the inclusive elapsed-age cutoff relative to 2026-04-01 00:00 UTC. This is a fixed synthetic reference, not today's clock.

```sh
uv run --frozen python execute.py --timeout 240 --parameters '{"MIN_AGE_DAYS":90}' --output-dir artifacts/age-90
```

Reset 30 for defaults. Overrides require a separate destination. Successful copies receive assets, fixture documents/adapter, helpers, README and environment files. Logs, request audits, extracted charts and experiments are ignored.

## Literal portable launch

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'google' (TYPE vgi, LOCATION 'uvx --python 3.13 --with vgi-python==0.51.0 --with vgi-rpc==0.53.1 --with pyarrow==26.0.1 --with google-api-python-client==2.201.0 --with google-auth==2.62.1 --with httplib2==0.32.1 --with google-auth-httplib2==0.4.4 --from git+https://github.com/Query-farm/vgi-google@v0.4.0 python fixture_worker.py');
```

The verified Git tag resolves to `9be790cc6a361c9e37e289e0857fdf12cf088fdc`. The notebook-only adapter imports that unchanged worker, installs its existing `set_http_factory` seam and invokes `GoogleWorker.main()`. Google's actual client builds requests from minimal static discovery documents. The in-memory httplib2-style transport accepts only the documented read shapes and supplies authored JSON; it does not open a network socket, resolve credentials or fall back to Google. All discovery-client construction, worker pagination, parsing and Arrow serialization still run. Upgrade pins deliberately and rerun both scenarios and checks.

[Fixture notes](fixtures/README.md) distinguish synthetic responses and minimal discovery descriptions from real Google/account data. The exact request audit expects three Drive pages and one Sheets fetch. `CREATE TABLE AS SELECT` materializes each worker call before DataFrame capture to avoid repeat requests. No file content, account identity, real credential or private data is saved.

This notebook intentionally has no live mode. Live account authorization, API quota behavior and user permissions are not validated by its transport fixture. The repository's normal worker supports separately configured credentials; those are outside this reproducible, read-only exercise.

## Interpretation limits

IDs preserve case and only trim surrounding Sheet whitespace. A distinct key set avoids join amplification. Ledger presence is a membership fact, not proof of review completion. An ID absent from this filtered PDF listing might belong to another scope; it does not establish deletion. Modification ages use UTC instants and elapsed 24-hour days. Missing and future timestamps are separate from observed age zero.

Category counts partition the listing. The size panel sums only known bytes (MiB = 1,048,576 bytes), marks unknown sizes and distinguishes observed zero bytes. A cutoff changes only the shortlist; it does not change account state or justify deleting or modifying files.

## Validation

Validated with published Haybarn 2.0.0rc11 on 2026-11-14: default and a separate 90-day experiment ran from a standalone copy containing only this directory (17 code cells and two charts each). All ten SQL invariants and the exact four-request transport audit passed in both. The shortlist shrank from three files (11 known MiB and one unknown size) to one file (8 known MiB), with identical source records. All four charts were inspected; three failure/override guards preserved the successful default.

Current source checks passed: 27 unit tests, with two live credential-gated tests skipped; 48 SQL assertions in one case; Ruff, formatting, strict mypy and pydoclint. The configured metadata gate scored 100/100 at L1 with zero findings; example execution is disabled for this credentialed worker, so that metadata score does not claim live Google validation. Root source and the root lockfile remain unchanged.

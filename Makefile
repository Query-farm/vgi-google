# vgi-google worker -- dev and test targets.
#
# Usage:
#   make test        # unit (pytest) + SQL end-to-end (haybarn-unittest, mock transport)
#   make test-unit   # pytest only (fixture parsers + mock E2E, no live network)
#   make test-sql    # SQL end-to-end only (haybarn glob, driven against the mock transport)
#   make lint        # ruff
#   make typecheck   # mypy
#
# The SQL suite drives the worker as a real subprocess over stdio: haybarn-unittest
# ATTACHes `${VGI_GOOGLE_WORKER}`, then runs the .test files in test/sql/. The
# worker is launched with VGI_GOOGLE_MOCK=1 so its adapters build from static
# discovery docs and are served canned, paginated responses (tests/mock_google.py),
# so NOTHING in the CI gate touches a live Google API.

# Worker stdio command (overridable). The PEP-723 header in google_worker.py
# pins the google client libs, so `uv run` gives the worker its dependencies.
WORKER_STDIO   ?= uv run --python 3.13 google_worker.py

# haybarn-unittest: the DuckDB sqllogictest runner (uv tool install haybarn-unittest).
HAYBARN        ?= haybarn-unittest
TEST_DIR        = .
TEST_PATTERN    = test/sql/*

.PHONY: test test-unit test-sql pytest lint typecheck

test: test-unit test-sql

test-unit: pytest

pytest:
	uv run --no-sync pytest -q

# End-to-end SQL: a tiny Python launcher enables the mock transport (and a temp
# discovery dir for the discovery helpers), then runs the haybarn glob with the
# worker command exported.
test-sql:
	@command -v $(HAYBARN) >/dev/null 2>&1 || { \
		echo "ERROR: $(HAYBARN) not found. Install it with:" >&2; \
		echo "  uv tool install haybarn-unittest" >&2; \
		echo "  (then ensure ~/.local/bin is on PATH)" >&2; \
		exit 1; \
	}
	VGI_GOOGLE_WORKER="$(WORKER_STDIO)" \
	HAYBARN="$(HAYBARN)" TEST_DIR="$(TEST_DIR)" TEST_PATTERN="$(TEST_PATTERN)" \
		uv run --no-sync python scripts/run_sql_e2e.py

lint:
	uv run --no-sync ruff check .

typecheck:
	uv run --no-sync mypy vgi_google

# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python",
#     "google-api-python-client>=2.100",
#     "google-auth>=2.20",
# ]
#
# [tool.uv.sources]
# vgi-python = { path = "../vgi-python" }
# ///
"""VGI worker exposing Google APIs to DuckDB/SQL.

Assembles the table functions in ``vgi_google`` into a single ``google`` catalog
and runs the worker over stdio (a DuckDB subprocess) or HTTP (serve.py).

It is a discovery-driven Google connector: curated READ adapters for the
high-demand APIs (Sheets / Drive / Calendar / YouTube), a generic ``google_call``
escape hatch for any other Google REST API, and ``google_apis`` /
``google_methods`` to discover what is reachable. Auth is service-account-first
(API key for public APIs), resolved via the VGI secret provider — never inline.

This is an egress / commodity connector: the data and its quotas/billing live in
Google's APIs, not here. See the README for the honest framing, per-adapter OAuth
scopes, and the note that quotas / billing / ToS are the operator's
responsibility.

Usage:
    uv run google_worker.py              # serve over stdio (DuckDB subprocess)
    python serve.py --port 8000          # serve over HTTP

    INSTALL vgi FROM community; LOAD vgi;
    ATTACH 'google' (TYPE vgi, LOCATION 'uv run google_worker.py');

    SELECT * FROM google.google_sheet('1AbC...', 'Sheet1!A1:Z', header := true);
    SELECT * FROM google.google_apis() WHERE name LIKE '%calendar%';
"""

from __future__ import annotations

import os

from vgi import Worker
from vgi.catalog import Catalog, Schema

from vgi_google.tables import TABLE_FUNCTIONS


def _maybe_install_mock() -> None:
    """Install the canned test transport when ``VGI_GOOGLE_MOCK=1``.

    This is the seam the haybarn SQL E2E uses to drive the REAL worker subprocess
    against deterministic, paginated responses — no live Google, no credentials.
    Production never sets this; the import is lazy so the test-only module is not
    a runtime dependency.
    """
    if os.environ.get("VGI_GOOGLE_MOCK") != "1":
        return
    from tests.mock_google import http_factory  # test-only import
    from vgi_google import client

    client.set_http_factory(http_factory)

_GOOGLE_CATALOG = Catalog(
    name="google",
    default_schema="main",
    schemas=[
        Schema(
            name="main",
            comment="Query Google APIs from SQL: Sheets / Drive / Calendar / YouTube + a generic hatch",
            functions=list(TABLE_FUNCTIONS),
        ),
    ],
)


class GoogleWorker(Worker):
    """Worker process hosting the ``google`` catalog."""

    catalog = _GOOGLE_CATALOG


def main() -> None:
    """Run the google worker process (stdio or, via flags, HTTP)."""
    _maybe_install_mock()
    GoogleWorker.main()


if __name__ == "__main__":
    main()

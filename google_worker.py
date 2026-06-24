# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.8.4",
#     "google-api-python-client>=2.100",
#     "google-auth>=2.20",
# ]
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


_CATALOG_DESCRIPTION_LLM = (
    "Query Google's REST APIs from SQL as table functions. Curated READ adapters cover the "
    "high-demand surfaces: read a Google Sheets range as rows (google_sheet), list/search Google "
    "Drive files (google_drive), list Google Calendar events (google_calendar), and search YouTube "
    "videos with view/like/comment counts (google_youtube). A generic escape hatch (google_call) "
    "invokes any Google API method and returns its JSON rows, while google_apis and google_methods "
    "let you discover which APIs and methods are reachable. Use it to pull spreadsheet data, file "
    "metadata, calendar events, and video statistics into SQL, or to reach any other Google REST "
    "API. Auth is service-account-first (API key for public APIs) via the VGI secret provider; "
    "the worker is READ-only and quotas/billing live in Google's APIs."
)

_CATALOG_DESCRIPTION_MD = (
    "# google\n\n"
    "Query Google APIs from DuckDB/SQL as VGI table functions.\n\n"
    "Curated READ adapters: `google_sheet` (Sheets ranges), `google_drive` (Drive files), "
    "`google_calendar` (Calendar events), `google_youtube` (YouTube search). Generic hatch: "
    "`google_call` (any Google API method, JSON rows). Discovery: `google_apis`, `google_methods`.\n\n"
    "Auth is service-account-first (API key for public APIs) via the VGI secret provider. The "
    "connector is READ-only; quotas, billing and Google's ToS are the operator's responsibility."
)

_SCHEMA_DESCRIPTION_LLM = (
    "Google API table functions: read Sheets ranges (google_sheet), list/search Drive files "
    "(google_drive), list Calendar events (google_calendar), search YouTube videos (google_youtube), "
    "call any Google API method (google_call), and discover reachable APIs/methods (google_apis, "
    "google_methods)."
)

_SCHEMA_DESCRIPTION_MD = (
    "Google API table functions over the Discovery Service: curated Sheets/Drive/Calendar/YouTube "
    "READ adapters, a generic `google_call` hatch, and `google_apis` / `google_methods` discovery."
)

_GOOGLE_CATALOG = Catalog(
    name="google",
    default_schema="main",
    comment="Query Google APIs from SQL: Sheets / Drive / Calendar / YouTube + a generic hatch.",
    source_url="https://github.com/Query-farm/vgi-google",
    tags={
        "vgi.title": "Google APIs for SQL",
        "vgi.keywords": (
            "google, google apis, sheets, drive, calendar, youtube, gmail, discovery, rest api, egress, connector"
        ),
        "vgi.doc_llm": _CATALOG_DESCRIPTION_LLM,
        "vgi.doc_md": _CATALOG_DESCRIPTION_MD,
        "vgi.author": "Query.Farm",
        "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
        "vgi.license": "MIT",
        "vgi.support_contact": "https://github.com/Query-farm/vgi-google/issues",
        "vgi.support_policy_url": "https://github.com/Query-farm/vgi-google/blob/main/README.md",
    },
    schemas=[
        Schema(
            name="main",
            comment="Query Google APIs from SQL: Sheets / Drive / Calendar / YouTube + a generic hatch",
            tags={
                "vgi.title": "Google API Table Functions",
                "vgi.keywords": (
                    "google, sheets, drive, calendar, youtube, google_call, "
                    "google_apis, google_methods, discovery, rest api"
                ),
                "vgi.source_url": ("https://github.com/Query-farm/vgi-google/blob/main/google_worker.py"),
                "vgi.doc_llm": _SCHEMA_DESCRIPTION_LLM,
                "vgi.doc_md": _SCHEMA_DESCRIPTION_MD,
                # VGI123 classifying tags use BARE keys (not vgi.-namespaced).
                "domain": "productivity",
                "category": "api-integration",
                "topic": "google-apis",
                # VGI506 representative example queries for the schema. The two
                # discovery functions are public (no credentials), so they run
                # as written; the curated adapters need a configured secret.
                "vgi.example_queries": (
                    "SELECT name, version, title FROM google.main.google_apis() "
                    "WHERE name LIKE '%sheets%' ORDER BY name LIMIT 5;\n"
                    "SELECT method, http_method FROM google.main.google_methods('drive', 'v3') "
                    "ORDER BY method LIMIT 5;\n"
                    "SELECT * FROM google.main.google_sheet('1AbC...', 'Sheet1!A1:Z', header := true);\n"
                    "SELECT id, name FROM google.main.google_drive("
                    "query := \"mimeType='application/pdf'\", count := 100);\n"
                    "SELECT video_id, title, view_count "
                    "FROM google.main.google_youtube('duckdb', count := 25);"
                ),
            },
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

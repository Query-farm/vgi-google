# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.16.0",
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

import json
import os

from vgi import Worker
from vgi.catalog import Catalog, Schema, View

from vgi_google.tables import TABLE_FUNCTIONS

# A genuinely browsable DATA relation: the Google API directory, so an agent can
# `SELECT * FROM google.main.google_api_directory` — no arguments, no secret — to
# see which Google services are reachable and the exact `api`/`version` strings to
# pass to google_call / google_methods. It surfaces upstream DATA (Google's public
# discovery directory), NOT a catalogue of this worker's own functions, so it gives
# an agent a zero-argument entry point (VGI146) without duplicating
# duckdb_functions() (VGI327). The projection is a stable subset of google_apis().
_API_DIRECTORY_VIEW = View(
    name="google_api_directory",
    definition=(
        "SELECT name, version, title, preferred, discovery_url FROM google.main.google_apis() ORDER BY name, version"
    ),
    comment="Browsable directory of the Google APIs reachable through the public Discovery Service.",
    column_comments={
        "name": "Google API name to pass as the first argument to google_call / google_methods.",
        "version": "API version to pass alongside the name (e.g. 'v3').",
        "title": "Human-readable API title.",
        "preferred": "TRUE when this is Google's preferred version of the API.",
        "discovery_url": "URL of the API's discovery document.",
    },
    tags={
        "vgi.title": "Google API Directory (browsable)",
        "vgi.category": "API discovery",
        # VGI123 classifying tags use BARE keys (not vgi.-namespaced).
        "domain": "productivity",
        "category": "api-integration",
        "topic": "google-apis",
        "vgi.doc_llm": (
            "A ready-to-scan **directory of the Google APIs** reachable through the public Discovery "
            "Service, so an agent can browse the surface with a plain `SELECT` (no arguments, no "
            "credential) before choosing what to call. Each row gives the `name` and `version` to pass "
            "to `google_call` and `google_methods`, the human-readable `title`, whether it is Google's "
            "`preferred` version, and the `discovery_url`. This is a thin, ordered projection of "
            "`google_apis()`; it reads Google's public API directory, so no secret is required. Filter "
            "by `name LIKE '%sheets%'` or `preferred` to narrow it."
        ),
        "vgi.doc_md": (
            "## google_api_directory (view)\n\n"
            "A browsable directory of every Google API reachable through the public Discovery Service: "
            "the `name` and `version` to pass to `google_call` / `google_methods`, the API `title`, "
            "whether it is the `preferred` version, and the discovery-document URL. Browse it directly "
            "to discover what is reachable — no arguments and no credential needed. The attached example "
            "queries show how to find a service and its preferred version."
        ),
        "vgi.keywords": json.dumps(
            [
                "google apis",
                "api directory",
                "discovery",
                "reachable apis",
                "browsable",
                "services",
                "versions",
                "google",
                "catalog",
            ]
        ),
        "vgi.example_queries": json.dumps(
            [
                {
                    "description": "Find the Sheets APIs in the directory and their versions.",
                    "sql": (
                        "SELECT name, version, title FROM google.main.google_api_directory "
                        "WHERE name LIKE '%sheets%' ORDER BY name, version"
                    ),
                },
                {
                    "description": "List the preferred version of every reachable Google API.",
                    "sql": ("SELECT name, version FROM google.main.google_api_directory WHERE preferred ORDER BY name"),
                },
            ]
        ),
    },
)


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
    "# Google APIs for SQL\n\n"
    "![Google logo](https://upload.wikimedia.org/wikipedia/commons/thumb/2/2f/Google_2015_logo.svg/250px-Google_2015_logo.svg.png)\n\n"
    "**Query the entire Google REST API surface directly from DuckDB SQL** — read Google "
    "Sheets ranges, list Google Drive files, pull Google Calendar events, search YouTube "
    "videos, and reach any other Google API as ordinary SQL table functions, no glue code "
    "required.\n\n"
    "This extension turns Google's APIs into a relational data source. It is built on the "
    "official [google-api-python-client](https://github.com/googleapis/google-api-python-client) "
    "library and Google's [API Discovery Service](https://developers.google.com/discovery), so "
    "any service Google publishes a discovery document for is reachable — not just a hand-picked "
    "few. Curated READ adapters give first-class, typed columns for the high-demand surfaces "
    "(Sheets, Drive, Calendar, YouTube), while a generic escape hatch covers everything else. "
    "Authentication is service-account-first (or an API key for public data), resolved securely "
    "through the VGI secret provider — credentials are never inlined in SQL. See the "
    "[client library documentation](https://googleapis.github.io/google-api-python-client/docs/) "
    "for the full API catalog and auth model.\n\n"
    "It is built for data engineers and analysts who want spreadsheet data, file metadata, "
    "calendar schedules, and video statistics in their warehouse without standing up a separate "
    "ETL service. Three kinds of access work together: **curated data adapters** provide typed "
    "columns for the high-demand surfaces (Sheets, Drive, Calendar, YouTube), **generic API "
    "access** reaches any other Google REST method by name and returns its JSON response as rows, "
    "and **API discovery** lets you introspect which APIs and methods are reachable — all from "
    "inside SQL.\n\n"
    "A typical query loads a spreadsheet range straight into SQL by naming a spreadsheet ID and "
    "an A1 range; the per-function example queries show the exact call shapes for Sheets, Drive, "
    "Calendar, YouTube, and the generic hatch. "
    "Pagination (Google's `nextPageToken`) is handled automatically and a `count` argument caps "
    "total rows. The connector is **READ-only**; data, quotas, billing and compliance with "
    "Google's Terms of Service remain the responsibility of the operator whose credentials are "
    "used."
)

_SCHEMA_DESCRIPTION_LLM = (
    "Google API table functions: read Sheets ranges (google_sheet), list/search Drive files "
    "(google_drive), list Calendar events (google_calendar), search YouTube videos (google_youtube), "
    "call any Google API method (google_call), and discover reachable APIs/methods (google_apis, "
    "google_methods)."
)

_SCHEMA_DESCRIPTION_MD = (
    "## Google API table functions\n\n"
    "The `main` schema exposes Google's REST surface as SQL table functions, grouped into three "
    "kinds of access.\n\n"
    "- **Curated data adapters** give first-class, typed columns for the high-demand surfaces — "
    "Sheets ranges, Drive file metadata, Calendar events, and YouTube video statistics.\n"
    "- **Generic API access** reaches any other Google REST method by name and returns its JSON "
    "response as rows, so the breadth is not limited to the curated few.\n"
    "- **API discovery** lists which APIs and methods are reachable through Google's public "
    "Discovery Service, so you can find the right method and parameters before you call it.\n\n"
    "Authentication is service-account-first (an API key suffices for public data), resolved "
    "through the VGI secret provider — credentials are never inlined in SQL. Pagination is handled "
    "automatically and a `count` argument caps total rows. Every function is READ-only."
)

_GOOGLE_CATALOG = Catalog(
    name="google",
    default_schema="main",
    comment="Query Google APIs from SQL: Sheets / Drive / Calendar / YouTube + a generic hatch.",
    source_url="https://github.com/Query-farm/vgi-google",
    tags={
        "vgi.title": "Google APIs for SQL",
        "vgi.keywords": json.dumps(
            [
                "google",
                "google apis",
                "sheets",
                "drive",
                "calendar",
                "youtube",
                "gmail",
                "discovery",
                "rest api",
                "egress",
                "connector",
            ]
        ),
        "vgi.doc_llm": _CATALOG_DESCRIPTION_LLM,
        "vgi.doc_md": _CATALOG_DESCRIPTION_MD,
        "vgi.author": "Query.Farm",
        "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
        "vgi.license": "MIT",
        "vgi.support_contact": "https://github.com/Query-farm/vgi-google/issues",
        "vgi.support_policy_url": "https://github.com/Query-farm/vgi-google/blob/main/README.md",
        # VGI152 analyst task suite for `vgi-lint simulate` / the agent-check.
        # Tasks bias toward the public Discovery Service functions (no
        # credentials); the curated-adapter tasks show the expected shape even
        # though executing them needs a configured Google secret.
        "vgi.agent_test_tasks": json.dumps(
            [
                {
                    # These tasks exercise the credential-free Discovery surface
                    # (the worker's distinguishing breadth): finding the right
                    # API/version/method to call. They resolve against Google's
                    # public API directory, so they run and are reference-graded
                    # without any configured secret. Each prompt pins the exact
                    # columns and filter so the analyst's SQL reproduces the
                    # reference result. Invoking the curated adapters or
                    # google_call additionally needs a Google credential — see the
                    # README and the per-function docs.
                    "name": "discover_sheets_api",
                    "prompt": (
                        "Using the google_apis discovery function, find the reachable Google API "
                        "whose name is exactly 'sheets'. Return only its name, version and title "
                        "columns."
                    ),
                    "reference_sql": (
                        "SELECT name, version, title FROM google.main.google_apis() WHERE name = 'sheets';"
                    ),
                },
                {
                    "name": "sheets_preferred_version",
                    "prompt": (
                        "Using the google_apis discovery function, what is the preferred version of "
                        "the Google API whose name is exactly 'sheets'? Return only the version "
                        "column for the row where name = 'sheets' and preferred is true."
                    ),
                    "reference_sql": (
                        "SELECT version FROM google.main.google_apis() WHERE name = 'sheets' AND preferred;"
                    ),
                },
                {
                    "name": "drive_files_list_verb",
                    "prompt": (
                        "Using the google_methods discovery function for the api 'drive' version "
                        "'v3', return the method and http_method columns for the method whose "
                        "dotted name is exactly 'files.list'."
                    ),
                    "reference_sql": (
                        "SELECT method, http_method FROM google.main.google_methods('drive', 'v3') "
                        "WHERE method = 'files.list';"
                    ),
                },
                {
                    "name": "calendar_api_reachable",
                    "prompt": (
                        "Using the google_apis discovery function, confirm the Google Calendar API "
                        "is reachable. Return only the name and version columns for the API whose "
                        "name is exactly 'calendar'."
                    ),
                    "reference_sql": ("SELECT name, version FROM google.main.google_apis() WHERE name = 'calendar';"),
                },
                {
                    # Credential-free and reference-graded: google_api_directory is
                    # a browsable view over the public Discovery directory, so it
                    # resolves with no secret.
                    "name": "browse_api_directory_for_calendar",
                    "prompt": (
                        "Browse the worker's Google API directory view and return the name and "
                        "version columns for the API whose name is exactly 'calendar'."
                    ),
                    "reference_sql": (
                        "SELECT name, version FROM google.main.google_api_directory WHERE name = 'calendar';"
                    ),
                },
                {
                    "name": "sheets_values_get_verb",
                    "prompt": (
                        "Using the google_methods discovery function for the api 'sheets' version "
                        "'v4', return only the http_method column for the method whose dotted name "
                        "is exactly 'spreadsheets.values.get'."
                    ),
                    "reference_sql": (
                        "SELECT http_method FROM google.main.google_methods('sheets', 'v4') "
                        "WHERE method = 'spreadsheets.values.get';"
                    ),
                },
                # The remaining tasks exercise the curated adapters and the
                # generic hatch (VGI520 coverage). Executing them needs a
                # configured Google secret (google_service_account or
                # google_api_key), so under `--execute`/agent-simulation they are
                # credential-gated and cannot be reference-graded here; they
                # document the expected call shape for an operator who has
                # configured credentials. See the README auth section.
                {
                    "name": "sheet_read_range",
                    "prompt": (
                        "Read the first 50 rows of the A1 range 'Sheet1!A1:Z' from the Google "
                        "spreadsheet whose ID is '1AbC', treating the first row as headers, and "
                        "return the row_number column. (Requires a configured Google secret.)"
                    ),
                    "reference_sql": (
                        "SELECT row_number FROM google.main.google_sheet('1AbC', 'Sheet1!A1:Z', header := true) "
                        "WHERE row_number <= 50 ORDER BY row_number;"
                    ),
                },
                {
                    "name": "drive_list_pdfs",
                    "prompt": (
                        "List up to 25 Google Drive files whose MIME type is 'application/pdf', "
                        "returning their id and name. (Requires a configured Google secret.)"
                    ),
                    "reference_sql": (
                        "SELECT id, name FROM google.main.google_drive("
                        "query := \"mimeType='application/pdf'\", count := 25) ORDER BY name;"
                    ),
                },
                {
                    "name": "calendar_list_events",
                    "prompt": (
                        "List up to 20 events from the primary Google Calendar, returning the id, "
                        "summary and start_time columns. (Requires a configured Google secret.)"
                    ),
                    "reference_sql": (
                        "SELECT id, summary, start_time FROM google.main.google_calendar("
                        "calendar_id := 'primary', count := 20) ORDER BY start_time;"
                    ),
                },
                {
                    "name": "youtube_search_by_views",
                    "prompt": (
                        "Search YouTube for videos matching 'duckdb', ordered by view count, and "
                        "return the title and view_count of the top 10. (Requires a configured "
                        "Google secret.)"
                    ),
                    "reference_sql": (
                        "SELECT title, view_count FROM google.main.google_youtube("
                        "'duckdb', count := 10, order_by := 'viewCount') "
                        "ORDER BY view_count DESC;"
                    ),
                },
                {
                    "name": "call_gmail_labels",
                    "prompt": (
                        "Using the generic google_call hatch, list the user-created Gmail label "
                        "names for the authenticated user (api 'gmail', version 'v1', method "
                        "'users.labels.list', userId 'me'). (Requires a configured Google secret.)"
                    ),
                    "reference_sql": (
                        "SELECT result ->> '$.name' AS label "
                        "FROM google.main.google_call('gmail', 'v1', 'users.labels.list', '{\"userId\":\"me\"}') "
                        "WHERE result ->> '$.type' = 'user' ORDER BY label;"
                    ),
                },
            ]
        ),
    },
    schemas=[
        Schema(
            name="main",
            comment="Query Google APIs from SQL: Sheets / Drive / Calendar / YouTube + a generic hatch",
            tags={
                "vgi.title": "Google API Table Functions",
                "vgi.keywords": json.dumps(
                    [
                        "google",
                        "sheets",
                        "drive",
                        "calendar",
                        "youtube",
                        "google_call",
                        "google_apis",
                        "google_methods",
                        "discovery",
                        "rest api",
                    ]
                ),
                "vgi.doc_llm": _SCHEMA_DESCRIPTION_LLM,
                "vgi.doc_md": _SCHEMA_DESCRIPTION_MD,
                # VGI413 category registry: each function declares a `vgi.category`
                # naming one of these; categories drive navigation/listing sections.
                "vgi.categories": json.dumps(
                    [
                        {
                            "name": "Curated data adapters",
                            "description": (
                                "Typed READ adapters for the high-demand Google surfaces — "
                                "Sheets ranges, Drive file metadata, Calendar events, and "
                                "YouTube video statistics — with first-class columns."
                            ),
                        },
                        {
                            "name": "Generic API access",
                            "description": (
                                "An escape hatch that invokes any Google REST API method by "
                                "name and returns its JSON response as rows, covering services "
                                "beyond the curated adapters."
                            ),
                        },
                        {
                            "name": "API discovery",
                            "description": (
                                "Introspection over Google's public Discovery Service: list "
                                "reachable APIs and the callable methods/parameters of each, so "
                                "you can find what to call before calling it."
                            ),
                        },
                    ]
                ),
                # VGI123 classifying tags use BARE keys (not vgi.-namespaced).
                "domain": "productivity",
                "category": "api-integration",
                "topic": "google-apis",
                # VGI515 described example queries for the schema. The two
                # discovery functions are public (no credentials), so they run
                # as written; the curated adapters need a configured secret. Each
                # example projects explicit columns (never a bare SELECT *).
                "vgi.example_queries": json.dumps(
                    [
                        {
                            "description": "Discover the Sheets API's reachable versions via the Discovery Service.",
                            "sql": (
                                "SELECT name, version, title FROM google.main.google_apis() "
                                "WHERE name LIKE '%sheets%' ORDER BY name LIMIT 5"
                            ),
                        },
                        {
                            "description": "Browse the Drive v3 methods you can invoke through google_call.",
                            "sql": (
                                "SELECT method, http_method FROM google.main.google_methods('drive', 'v3') "
                                "ORDER BY method LIMIT 5"
                            ),
                        },
                        {
                            "description": "Read a header sheet range, projecting a named column from each row.",
                            "sql": (
                                "SELECT row_number, record ->> '$.name' AS name "
                                "FROM google.main.google_sheet('1AbC...', 'Sheet1!A1:Z', header := true) "
                                "WHERE row_number <= 100 ORDER BY row_number"
                            ),
                        },
                        {
                            "description": "List up to 100 PDF files in Google Drive by MIME type.",
                            "sql": (
                                "SELECT id, name FROM google.main.google_drive("
                                "query := \"mimeType='application/pdf'\", count := 100)"
                            ),
                        },
                        {
                            "description": "Search YouTube for 'duckdb' videos and project their view counts.",
                            "sql": (
                                "SELECT video_id, title, view_count "
                                "FROM google.main.google_youtube('duckdb', count := 25)"
                            ),
                        },
                    ]
                ),
            },
            functions=list(TABLE_FUNCTIONS),
            views=[_API_DIRECTORY_VIEW],
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

"""VGI worker exposing Google APIs to DuckDB/SQL.

Assembles the table functions in ``vgi_google`` into a single ``google`` catalog
and runs the worker over stdio (a DuckDB subprocess) or HTTP.

It is a discovery-driven Google connector: curated READ adapters for the
high-demand APIs (Sheets / Drive / Calendar / YouTube), a generic ``google_call``
escape hatch for any other Google REST API, and ``google_apis`` /
``google_methods`` to discover what is reachable. Auth is service-account-first
(API key for public APIs), resolved via the VGI secret provider — never inline.

This is an egress / commodity connector: the data and its quotas/billing live in
Google's APIs, not here. See the README for the honest framing, per-adapter OAuth
scopes, and the note that quotas / billing / ToS are the operator's
responsibility.

The importable home of the worker is this module (``vgi_google.worker``): the
``vgi-google-worker`` console script points at :func:`main`, the Docker image's
HTTP mode serves ``vgi_google.worker:GoogleWorker`` via ``vgi-serve``, and the
repo-root ``google_worker.py`` is a thin PEP-723 shim re-exporting these so
``uv run google_worker.py`` keeps working unchanged.

Usage:
    vgi-google-worker                    # serve over stdio (DuckDB subprocess)
    uv run google_worker.py              # serve over stdio (PEP-723 shim)
    vgi-serve vgi_google.worker:GoogleWorker --http --port 8000   # serve over HTTP

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

from vgi_google.auth import SECRET_TYPES
from vgi_google.tables import COMPOSED_EXAMPLES, TABLE_FUNCTIONS
from vgi_google.workspace_tables import COMPOSED_EXAMPLES as WORKSPACE_COMPOSED_EXAMPLES
from vgi_google.workspace_tables import WORKSPACE_CATEGORIES, WORKSPACE_FUNCTIONS

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
        "vgi.title": "Discoverable Google APIs",
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
    "Query Google's REST APIs from SQL as table functions. Curated READ adapters give typed columns "
    "for the high-demand surfaces (Sheets ranges, Drive file metadata, Calendar events, YouTube "
    "video statistics); a generic escape hatch invokes any Google API method by name and returns "
    "its JSON response as rows; and discovery functions, backed by Google's public API Discovery "
    "Service, list which APIs and methods are reachable so you can find a method and its "
    "parameters before calling it. Use it to pull spreadsheet data, file metadata, calendar events "
    "and video statistics into SQL, or to reach any other Google REST API. Auth is "
    "service-account-first (API key for public APIs) via the VGI secret provider; discovery needs "
    "no credential. Pagination is automatic and a count argument caps total rows. The worker is "
    "READ-only and quotas/billing live in Google's APIs."
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
    "Google API table functions in three kinds: curated typed adapters for Sheets, Drive, Calendar "
    "and YouTube; a generic call that reaches any Google REST method; and discovery of the "
    "reachable APIs and their methods (also browsable as a directory view). Start from discovery "
    "when the curated adapters do not cover the API you need."
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

_WORKSPACE_DESCRIPTION_LLM = (
    "Google Workspace admin data as typed SQL table functions: Admin SDK Directory objects (users, "
    "groups, group members, org units, domains, ChromeOS and mobile devices, admin roles and role "
    "assignments), the Admin SDK Reports audit log (one row per login/admin/drive/token/saml/groups "
    "event) and daily usage metrics, Alert Center alerts, and per-group Groups Settings. WHERE "
    "predicates on time ranges, event names, actors, e-mails, org units, roles and admin flags are "
    "pushed into each API's own filters. Needs a google_service_account secret with domain-wide "
    "delegation whose subject is an administrator."
)

_WORKSPACE_DESCRIPTION_MD = (
    "## Google Workspace admin data\n\n"
    "The `workspace` schema reads a Google Workspace tenant's administrative data as SQL tables, "
    "READ-only:\n\n"
    "- **Directory** — users, groups and members, organizational units, domains, ChromeOS and "
    "mobile devices, admin roles and role assignments (Admin SDK Directory API).\n"
    "- **Audit and usage reports** — the audit log flattened to one row per event, and daily "
    "tenant or per-user usage metrics (Admin SDK Reports API).\n"
    "- **Alerts and group settings** — Alert Center alerts and Groups Settings.\n\n"
    "SQL filters on times, event names, actors, e-mails, org units and flags are translated into "
    "the APIs' own server-side filters; the worker re-applies the exact predicate. Authenticate "
    "with a `google_service_account` secret using domain-wide delegation (`subject` = an admin's "
    "e-mail) and authorize each function's read-only scope (see the README)."
)

_WORKSPACE_SCHEMA = Schema(
    path=["workspace"],
    comment="Google Workspace admin data: Directory, Reports audit log and usage, Alert Center, Groups Settings",
    tags={
        "vgi.title": "Google Workspace Admin Data",
        "vgi.keywords": json.dumps(
            [
                "google workspace",
                "admin sdk",
                "directory",
                "audit log",
                "reports api",
                "alert center",
                "groups settings",
                "users",
                "devices",
                "domain-wide delegation",
            ]
        ),
        "vgi.doc_llm": _WORKSPACE_DESCRIPTION_LLM,
        "vgi.doc_md": _WORKSPACE_DESCRIPTION_MD,
        "vgi.categories": json.dumps(WORKSPACE_CATEGORIES),
        "domain": "it-administration",
        "category": "api-integration",
        "topic": "google-workspace",
        "vgi.example_queries": json.dumps(
            [
                {
                    "description": "Super administrators who are not enrolled in 2-step verification.",
                    "sql": (
                        "SELECT primary_email, last_login_time FROM google.workspace.users() "
                        "WHERE is_admin AND NOT is_enrolled_in_2sv ORDER BY primary_email"
                    ),
                },
                {
                    "description": "Failed sign-ins during the first week of November 2026.",
                    "sql": (
                        "SELECT time, actor_email, ip_address FROM google.workspace.activities('login') "
                        "WHERE event_name = 'login_failure' AND time >= TIMESTAMPTZ '2026-11-01 00:00:00+00' "
                        "AND time < TIMESTAMPTZ '2026-11-08 00:00:00+00' ORDER BY time"
                    ),
                },
                {
                    "description": "Tenant user count on one day from the usage reports.",
                    "sql": (
                        "SELECT int_value AS users FROM google.workspace.usage_reports() "
                        "WHERE date = DATE '2026-11-10' AND parameter = 'accounts:num_users'"
                    ),
                },
                {
                    "description": "High-severity Alert Center alerts created since November 2026.",
                    "sql": (
                        "SELECT create_time, type, status FROM google.workspace.alerts() "
                        "WHERE create_time >= TIMESTAMPTZ '2026-11-01 00:00:00+00' AND severity = 'HIGH' "
                        "ORDER BY create_time DESC"
                    ),
                },
                *WORKSPACE_COMPOSED_EXAMPLES,
            ]
        ),
    },
    functions=list(WORKSPACE_FUNCTIONS),
)

_GOOGLE_CATALOG = Catalog(
    name="google",
    default_schema="main",
    comment="Query Google APIs from SQL: Sheets / Drive / Calendar / YouTube + a generic hatch.",
    source_url="https://github.com/Query-farm/vgi-google",
    tags={
        "vgi.title": "Google APIs",
        "vgi.subtitle": (
            "Read Google Sheets, Drive, Calendar, YouTube and any other Google REST API as SQL table functions."
        ),
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
        # Only each task's name and prompt are published here (VGI416); the
        # private graders (reference_sql) live in vgi-agent-tests.yaml at the
        # repo root, merged by task name.
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
                },
                {
                    "name": "sheets_preferred_version",
                    "prompt": (
                        "Using the google_apis discovery function, what is the preferred version of "
                        "the Google API whose name is exactly 'sheets'? Return only the version "
                        "column for the row where name = 'sheets' and preferred is true."
                    ),
                },
                {
                    "name": "drive_files_list_verb",
                    "prompt": (
                        "Using the google_methods discovery function for the api 'drive' version "
                        "'v3', return the method and http_method columns for the method whose "
                        "dotted name is exactly 'files.list'."
                    ),
                },
                {
                    "name": "calendar_api_reachable",
                    "prompt": (
                        "Using the google_apis discovery function, confirm the Google Calendar API "
                        "is reachable. Return only the name and version columns for the API whose "
                        "name is exactly 'calendar'."
                    ),
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
                },
                {
                    "name": "sheets_values_get_verb",
                    "prompt": (
                        "Using the google_methods discovery function for the api 'sheets' version "
                        "'v4', return only the http_method column for the method whose dotted name "
                        "is exactly 'spreadsheets.values.get'."
                    ),
                },
                # Composed, credential-free tasks: each needs two discovery
                # objects in combination (the directory view to pick the version,
                # or two google_methods listings joined), and is reference-graded
                # live against Google's public discovery documents.
                {
                    "name": "sheets_preferred_post_methods",
                    "prompt": (
                        "Look up the preferred version of the Google API named exactly 'sheets' in "
                        "the worker's Google API directory view, then use google_methods for that "
                        "api and version to list the methods whose http_method is 'POST'. Return "
                        "only the method column, ordered by method."
                    ),
                },
                {
                    "name": "drive_methods_kept_in_v3_by_verb",
                    "prompt": (
                        "Compare the Drive API versions 'v2' and 'v3' with google_methods: count the "
                        "dotted method names that exist in BOTH versions, grouped by the v3 "
                        "http_method. Return two columns, http_method and n (the BIGINT count), "
                        "ordered by http_method."
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
                },
                {
                    "name": "drive_list_pdfs",
                    "prompt": (
                        "List up to 25 Google Drive files whose MIME type is 'application/pdf', "
                        "returning their id and name. (Requires a configured Google secret.)"
                    ),
                },
                {
                    "name": "calendar_list_events",
                    "prompt": (
                        "List up to 20 events from the primary Google Calendar, returning the id, "
                        "summary and start_time columns. (Requires a configured Google secret.)"
                    ),
                },
                {
                    "name": "youtube_search_by_views",
                    "prompt": (
                        "Search YouTube for videos matching 'duckdb', ordered by view count, and "
                        "return the title and view_count of the top 10. (Requires a configured "
                        "Google secret.)"
                    ),
                },
                {
                    "name": "call_gmail_labels",
                    "prompt": (
                        "Using the generic google_call hatch, list the user-created Gmail label "
                        "names for the authenticated user (api 'gmail', version 'v1', method "
                        "'users.labels.list', userId 'me'). (Requires a configured Google secret.)"
                    ),
                },
                # google.workspace tasks: credential-gated (a Workspace admin service
                # account with domain-wide delegation); graders in vgi-agent-tests.yaml.
                {
                    "name": "workspace_admins_without_2sv",
                    "prompt": "List the primary_email of Google Workspace super administrators who are not enrolled in 2-step verification, ordered by primary_email. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_largest_groups",
                    "prompt": "Return the email and direct_members_count of the 5 Google Workspace groups with the most direct members, largest first (ties by email). (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_group_owners",
                    "prompt": "List the email of every OWNER of the Google group 'eng@example.com', ordered by email. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_org_unit_tree",
                    "prompt": "List every organizational unit's org_unit_path in the Workspace tenant, ordered by org_unit_path. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_unverified_domains",
                    "prompt": "Which of the Workspace tenant's domains are not verified? Return domain_name ordered by domain_name. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_custom_roles",
                    "prompt": "List the role_name of the Workspace admin roles that are not system roles, ordered by role_name. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_role_holders",
                    "prompt": "For every Workspace admin role assignment, return the role_name and the assignee's primary_email (join role assignments to roles on role_id and to users on users.id = assigned_to), ordered by role_name then primary_email. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_chromeos_expired",
                    "prompt": "List the serial_number of managed ChromeOS devices whose auto_update_through is before 2026-11-15 00:00 UTC, ordered by serial_number. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_mobile_stale",
                    "prompt": "List the resource_id of managed mobile devices whose last_sync is before 2026-10-01 00:00 UTC, ordered by resource_id. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_failed_logins_week",
                    "prompt": "From the Workspace 'login' audit log, return time and actor_email of every login_failure event between 2026-11-01 00:00 UTC (inclusive) and 2026-11-08 00:00 UTC (exclusive), ordered by time. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_user_count_on_day",
                    "prompt": "Using the Workspace customer usage report for 2026-11-10, return the int_value of the 'accounts:num_users' metric. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_high_alerts",
                    "prompt": "List the alert_id and type of Alert Center alerts with severity 'HIGH' created on or after 2026-11-01 00:00 UTC, ordered by alert_id. (Requires a Workspace admin credential.)",
                },
                {
                    "name": "workspace_group_allows_outsiders",
                    "prompt": "Does the Google group 'eng@example.com' allow external members? Return who_can_join and allow_external_members from its group settings. (Requires a Workspace admin credential.)",
                },
            ]
        ),
    },
    schemas=[
        Schema(
            path=["main"],
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
                        # Composed examples (two of this worker's functions in one
                        # query), shared with the driving function's example list.
                        *COMPOSED_EXAMPLES,
                    ]
                ),
            },
            functions=list(TABLE_FUNCTIONS),
            views=[_API_DIRECTORY_VIEW],
        ),
        _WORKSPACE_SCHEMA,
    ],
)


class GoogleWorker(Worker):
    """Worker process hosting the ``google`` catalog."""

    catalog = _GOOGLE_CATALOG
    secret_types = SECRET_TYPES


def main() -> None:
    """Run the google worker process (stdio or, via flags, HTTP)."""
    _maybe_install_mock()
    GoogleWorker.main()


if __name__ == "__main__":
    main()

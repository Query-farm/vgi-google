"""Workspace admin functions: mapping, pagination, pushdown translation, auth hints.

Every request is built from Google's REAL discovery documents (trimmed fixtures)
and served by :class:`tests.mock_workspace.FakeWorkspaceHttp`, which records the
request URIs so pushdown and paging can be asserted on what reached the "API".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyarrow as pa
import pytest
from httplib2 import Response

from tests.harness import invoke_table_function
from tests.mock_google import http_factory
from tests.mock_workspace import WORKSPACE_DISCOVERY_DOCS, FakeWorkspaceHttp
from vgi_google import client, discovery
from vgi_google import workspace as ws
from vgi_google import workspace_tables as wt
from vgi_google.tables import GoogleCallFunction


@pytest.fixture()
def mock_google() -> Iterator[list[str]]:
    client.set_http_factory(http_factory)
    FakeWorkspaceHttp.REQUESTS.clear()
    try:
        yield FakeWorkspaceHttp.REQUESTS
    finally:
        client.set_http_factory(None)


def _queries(requests: list[str]) -> list[dict[str, str]]:
    return [{k: v[0] for k, v in parse_qs(urlparse(u).query).items()} for u in requests]


class FakeFilters:
    """Duck-typed stand-in for ``PushdownFilters`` (the helpers the adapters call)."""

    def __init__(self, values: dict[str, list[Any]] | None = None, bounds: dict[str, tuple[Any, Any]] | None = None):
        self.values = values or {}
        self.bounds = bounds or {}

    def get_column_values(self, column: str) -> pa.Array | None:
        return pa.array(self.values[column]) if column in self.values else None

    def get_column_bounds(self, column: str) -> Any:
        if column not in self.bounds:
            return None
        low, high = self.bounds[column]

        class _B:
            min_value = pa.scalar(low) if low is not None else None
            max_value = pa.scalar(high) if high is not None else None

        return _B()


# ---------------------------------------------------------------------------
# Real discovery docs: the fixtures are Google's documents
# ---------------------------------------------------------------------------


def test_fixture_docs_are_real_discovery_documents() -> None:
    reports = WORKSPACE_DISCOVERY_DOCS[("admin", "reports_v1")]
    assert reports["kind"] == "discovery#restDescription"
    params = reports["resources"]["activities"]["methods"]["list"]["parameters"]
    assert "pattern" in params["startTime"] and "login" in params["applicationName"]["pattern"]
    assert set(WORKSPACE_DISCOVERY_DOCS[("admin", "directory_v1")]["resources"]) >= {"users", "groups", "roles"}


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------


def test_users_typed_columns_and_never_logged_in(mock_google: list[str]) -> None:
    rows = {r["primary_email"]: r for r in invoke_table_function(wt.WorkspaceUsersFunction).to_pylist()}
    ada, bob = rows["ada@example.com"], rows["bob@example.com"]
    assert ada["is_admin"] is True and ada["aliases"] == ["countess@example.com"]
    assert ada["last_login_time"] == datetime(2026, 11, 10, 8, tzinfo=UTC)
    assert bob["last_login_time"] is None  # Directory's 1970 "never" sentinel
    assert json.loads(ada["extra"])["phones"][0]["type"] == "work"
    assert _queries(mock_google)[0] == {
        "customer": "my_customer",
        "maxResults": "500",
        "projection": "basic",
        "viewType": "admin_view",
        "alt": "json",
    }


def test_users_page_token_round_trip(mock_google: list[str]) -> None:
    table = invoke_table_function(wt.WorkspaceUsersFunction, named={"page_size": 1})
    assert table.column("id").to_pylist() == [f"10000000000000000000{i}" for i in (1, 2, 3)]
    assert [q.get("pageToken") for q in _queries(mock_google)] == [None, "1", "2"]


def test_users_count_caps_requests(mock_google: list[str]) -> None:
    table = invoke_table_function(wt.WorkspaceUsersFunction, named={"page_size": 1, "count": 2})
    assert table.num_rows == 2 and len(mock_google) == 2


def test_users_pushdown_builds_directory_query() -> None:
    pushed = ws.UsersAdapter().pushdown(
        FakeFilters(
            {
                "primary_email": ["o'neil@example.com"],
                "is_admin": [True],
                "suspended": [False],
                "org_unit_path": ["/Sales Team"],
            }
        )  # fmt: skip
    )
    assert pushed == {"query": "email='o\\'neil@example.com' orgUnitPath='/Sales Team' isAdmin=true isSuspended=false"}
    # A multi-valued predicate is not pushable as one clause: left to the worker.
    assert ws.UsersAdapter().pushdown(FakeFilters({"primary_email": ["a@x", "b@x"]})) == {}


def test_users_query_arg_and_pushdown_are_anded(mock_google: list[str]) -> None:
    args = wt._UsersArgs("my_customer", "", "isAdmin=true", False, "", "basic", 10, 0)
    service = client.build_client("admin", "directory_v1", secrets=None)
    page = ws.UsersAdapter().fetch_page(service, args, None, {"query": "isEnrolledIn2Sv=false"})
    assert [r["primary_email"] for r in page.rows] == ["ada@example.com"]
    assert _queries(mock_google)[0]["query"] == "isAdmin=true isEnrolledIn2Sv=false"


def test_users_domain_replaces_customer(mock_google: list[str]) -> None:
    invoke_table_function(wt.WorkspaceUsersFunction, named={"domain": "example.com"})
    q = _queries(mock_google)[0]
    assert q["domain"] == "example.com" and "customer" not in q


def test_groups_and_user_key(mock_google: list[str]) -> None:
    rows = invoke_table_function(wt.WorkspaceGroupsFunction).to_pylist()
    assert rows[0]["direct_members_count"] == 3 and rows[0]["aliases"] == ["engineering@example.com"]
    mine = invoke_table_function(wt.WorkspaceGroupsFunction, named={"user_key": "partner@other.org"})
    assert mine.column("email").to_pylist() == ["eng@example.com"]
    assert ws.GroupsAdapter().pushdown(FakeFilters({"email": ["eng@example.com"]})) == {
        "query": "email='eng@example.com'"
    }


def test_members_roles_pushdown(mock_google: list[str]) -> None:
    assert ws.MembersAdapter().pushdown(FakeFilters({"role": ["OWNER", "MANAGER"]})) == {"roles": "MANAGER,OWNER"}
    assert ws.MembersAdapter().pushdown(FakeFilters({"role": ["owner"]})) == {}  # not a valid role value
    service = client.build_client("admin", "directory_v1", secrets=None)
    args = wt._MembersArgs("eng@example.com", "", False, 10, 0)
    page = ws.MembersAdapter().fetch_page(service, args, None, {"roles": "OWNER"})
    assert [r["email"] for r in page.rows] == ["ada@example.com"]
    assert page.rows[0]["group_key"] == "eng@example.com"


def test_org_units_domains_roles(mock_google: list[str]) -> None:
    ous = invoke_table_function(wt.WorkspaceOrgUnitsFunction).column("org_unit_path").to_pylist()
    assert ous == ["/Engineering", "/Sales", "/Sales/EMEA"]
    domains = {r["domain_name"]: r for r in invoke_table_function(wt.WorkspaceDomainsFunction).to_pylist()}
    assert domains["example.com"]["creation_time"] == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert domains["example.com"]["domain_aliases"] == ["example.net"]
    roles = invoke_table_function(wt.WorkspaceRolesFunction)
    assert pa.types.is_struct(roles.schema.field("privileges").type.value_type)
    helpdesk = roles.to_pylist()[1]
    assert [p["privilege_name"] for p in helpdesk["privileges"]] == ["USERS_RETRIEVE", "USERS_UPDATE"]


def test_role_assignments_pushdown_prefers_role_id(mock_google: list[str]) -> None:
    pushed = ws.RoleAssignmentsAdapter().pushdown(
        FakeFilters({"role_id": ["9170516996784130"], "assigned_to": ["100000000000000000002"]})
    )
    assert pushed == {"roleId": "9170516996784130", "userKey": "100000000000000000002"}
    service = client.build_client("admin", "directory_v1", secrets=None)
    args = wt._RoleAssignmentsArgs("my_customer", "", "", 10, 0)
    page = ws.RoleAssignmentsAdapter().fetch_page(service, args, None, pushed)
    assert [r["role_assignment_id"] for r in page.rows] == ["91000002"]
    assert page.rows[0]["expire_time"] == datetime(2027, 1, 1, tzinfo=UTC)
    assert "userKey" not in _queries(mock_google)[0]  # the API rejects both at once


def test_devices(mock_google: list[str]) -> None:
    chrome = invoke_table_function(wt.WorkspaceChromeOsDevicesFunction).to_pylist()
    assert chrome[0]["recent_users"] == ["ada@example.com"]
    assert json.loads(chrome[0]["extra"])["cpuInfo"][0]["model"] == "Intel Celeron N4500"
    assert _queries(mock_google)[0]["projection"] == "FULL"
    assert ws.ChromeOsDevicesAdapter().pushdown(FakeFilters({"org_unit_path": ["/Classrooms"]})) == {
        "orgUnitPath": "/Classrooms"
    }
    mobile = invoke_table_function(wt.WorkspaceMobileDevicesFunction).to_pylist()[0]
    assert mobile["security_patch_level"] == datetime(2024, 10, 1, tzinfo=UTC)
    assert mobile["email"] == ["bob@example.com"]


# ---------------------------------------------------------------------------
# Reports: activities
# ---------------------------------------------------------------------------


def test_activities_flatten_events_and_parameters(mock_google: list[str]) -> None:
    rows = invoke_table_function(wt.WorkspaceActivitiesFunction, positional=("login",)).to_pylist()
    # 4 activities, one of which carries 2 events.
    assert len(rows) == 5
    challenge = next(r for r in rows if r["event_name"] == "login_challenge")
    assert challenge["event_index"] == 1 and challenge["actor_email"] == "cy@example.com"
    assert json.loads(challenge["parameters"]) == {"login_challenge_method": ["password"]}
    token = invoke_table_function(wt.WorkspaceActivitiesFunction, positional=("token",)).to_pylist()[0]
    assert json.loads(token["parameters"])["scope_data"] == [{"product_bucket": ["DRIVE"], "scope_name": "drive"}]
    schema = invoke_table_function(wt.WorkspaceActivitiesFunction, positional=("login",)).schema
    assert schema.field("time").type == pa.timestamp("us", tz="UTC") and not schema.field("time").nullable


def test_activities_pushdown_translation() -> None:
    pushed = ws.ActivitiesAdapter().pushdown(
        FakeFilters(
            {"event_name": ["login_failure"], "actor_email": ["cy@example.com"], "ip_address": ["198.51.100.7"]},
            {"time": (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 8, tzinfo=UTC))},
        )
    )
    assert pushed == {
        "startTime": "2026-10-31T23:59:59.000Z",
        "endTime": "2026-11-08T00:00:01.000Z",
        "eventName": "login_failure",
        "userKey": "cy@example.com",
        "actorIpAddress": "198.51.100.7",
    }


def test_activities_request_uses_pushed_filters(mock_google: list[str]) -> None:
    service = client.build_client("admin", "reports_v1", secrets=None)
    args = wt._ActivitiesArgs("login", "all", "", "", "", "", "", "", "", 100, 0)
    pushed = {"startTime": "2026-11-01T00:00:00.000Z", "eventName": "login_failure", "userKey": "cy@example.com"}
    page = ws.ActivitiesAdapter().fetch_page(service, args, None, pushed)
    assert {r["time"].day for r in page.rows} == {2, 5}
    assert "/users/cy@example.com/applications/login" in mock_google[0].replace("%40", "@")


def test_activities_date_args_are_normalized_to_rfc3339(mock_google: list[str]) -> None:
    invoke_table_function(
        wt.WorkspaceActivitiesFunction,
        positional=("login",),
        named={"start_time": "2026-11-03", "end_time": "2026-11-10"},
    )
    q = _queries(mock_google)[0]
    assert q["startTime"] == "2026-11-03T00:00:00.000Z" and q["endTime"] == "2026-11-10T00:00:00.000Z"


def test_activities_invalid_application_is_rejected_at_bind(mock_google: list[str]) -> None:
    # `application` declares the Reports applicationName values as choices.
    from vgi.arguments import ArgumentValidationError

    with pytest.raises(ArgumentValidationError, match="invalid value"):
        invoke_table_function(wt.WorkspaceActivitiesFunction, positional=("not_an_app",))
    assert mock_google == []
    assert set(wt.AUDIT_APPLICATIONS) >= {"login", "admin", "drive", "token", "saml", "groups"}


# ---------------------------------------------------------------------------
# Reports: usage
# ---------------------------------------------------------------------------


def test_usage_customer_report_typed_values(mock_google: list[str]) -> None:
    rows = {r["parameter"]: r for r in invoke_table_function(
        wt.WorkspaceUsageReportsFunction, named={"date": "2026-11-10"}).to_pylist()}  # fmt: skip
    assert rows["accounts:num_users"]["int_value"] == 3
    assert rows["accounts:apps_admin_enabled"]["bool_value"] is True
    assert rows["gmail:last_interaction_time"]["datetime_value"] == datetime(2026, 11, 10, 22, tzinfo=UTC)
    assert rows["accounts:num_users"]["date"] == date(2026, 11, 10)


def test_usage_day_range_with_composite_cursor(mock_google: list[str]) -> None:
    pushed = ws.UsageReportsAdapter().pushdown(
        FakeFilters({"parameter": ["gmail:num_emails_sent"]}, {"date": (date(2026, 11, 1), date(2026, 11, 3))})
    )
    assert pushed == {"date_min": "2026-11-01", "date_max": "2026-11-03", "parameters": "gmail:num_emails_sent"}
    service = client.build_client("admin", "reports_v1", secrets=None)
    args = wt._UsageArgs("user", "", "", "all", "", "", "", 100, 1)
    rows, cursor, cursors = [], None, []
    while True:
        page = ws.UsageReportsAdapter().fetch_page(service, args, cursor, pushed)
        rows.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break
        cursors.append(cursor)
    # 3 days x 2 users, one user per page: day/page cursors interleave.
    assert cursors == ["0|1", "1|", "1|1", "2|", "2|1"]
    assert [(r["date"].day, r["user_email"], r["int_value"]) for r in rows][:3] == [
        (1, "ada@example.com", 4),
        (1, "bob@example.com", 8),
        (2, "ada@example.com", 5),
    ]
    assert all(r["parameter"] == "gmail:num_emails_sent" for r in rows)


def test_usage_requires_a_day_and_caps_ranges() -> None:
    args = wt._UsageArgs("customer", "", "", "all", "", "", "", 100, 0)
    with pytest.raises(ValueError, match="needs a report day"):
        ws.UsageReportsAdapter.dates(args, {})
    with pytest.raises(ValueError, match="at most 92"):
        ws.UsageReportsAdapter.dates(args, {"date_min": "2026-01-01", "date_max": "2026-12-31"})
    assert ws.UsageReportsAdapter.dates(args, {"date_min": "2026-11-14"}, today=date(2026, 11, 15)) == [
        "2026-11-14",
        "2026-11-15",
    ]


def test_usage_empty_day_with_warning_is_empty(mock_google: list[str]) -> None:
    assert invoke_table_function(wt.WorkspaceUsageReportsFunction, named={"date": "2026-11-11"}).num_rows == 0


def test_usage_empty_days_inside_range_are_skipped(mock_google: list[str]) -> None:
    # Only 2026-11-10 has customer data; the tick must skip the empty days around it.
    state = wt._WorkspaceState(pushed_json=json.dumps({"date_min": "2026-11-08", "date_max": "2026-11-11"}))

    class _Out:
        def __init__(self) -> None:
            self.batches: list[pa.RecordBatch] = []
            self.finished = False

        def emit(self, batch: pa.RecordBatch) -> None:
            self.batches.append(batch)

        def finish(self) -> None:
            self.finished = True

    class _Params:
        args = wt._UsageArgs("customer", "", "", "all", "", "", "", 100, 0)
        secrets = None
        output_schema = ws.USAGE_SCHEMA

    out = _Out()
    while not out.finished:
        wt._run_tick(wt.WorkspaceUsageReportsFunction._adapter, _Params(), state, out)  # type: ignore[arg-type]
    assert sum(b.num_rows for b in out.batches) == 4


# ---------------------------------------------------------------------------
# Alert Center / Groups Settings
# ---------------------------------------------------------------------------


def test_alerts_filter_pushdown(mock_google: list[str]) -> None:
    pushed = ws.AlertsAdapter().pushdown(
        FakeFilters(
            {"type": ["Suspicious login", "Device compromised"]},
            {"create_time": (datetime(2026, 11, 1, tzinfo=UTC), None)},
        )
    )
    assert pushed == {
        "filter": 'createTime >= "2026-10-31T23:59:59.000Z" AND (type="Suspicious login" OR type="Device compromised")'
    }
    service = client.build_client("alertcenter", "v1beta1", secrets=None)
    args = wt._AlertsArgs("", "", "", 100, 0)
    page = ws.AlertsAdapter().fetch_page(service, args, None, pushed)
    assert sorted(r["alert_id"] for r in page.rows) == ["a-1", "a-3"]
    assert page.rows[0]["severity"] in ("HIGH", "LOW")


def test_alerts_user_filter_is_anded(mock_google: list[str]) -> None:
    invoke_table_function(wt.WorkspaceAlertsFunction, named={"filter": 'source="Gmail phishing"'})
    assert _queries(mock_google)[0]["filter"] == '(source="Gmail phishing")'


def test_group_settings_booleans_and_alt_json(mock_google: list[str]) -> None:
    row = invoke_table_function(wt.WorkspaceGroupSettingsFunction, positional=("eng@example.com",)).to_pylist()[0]
    assert row["allow_external_members"] is True and row["members_can_post_as_the_group"] is False
    assert row["who_can_join"] == "INVITED_CAN_JOIN"
    assert json.loads(row["settings"])["whoCanAddReferences"] == "NONE"
    assert _queries(mock_google)[0]["alt"] == "json"


# ---------------------------------------------------------------------------
# Auth / errors / metadata
# ---------------------------------------------------------------------------


def test_forbidden_carries_domain_wide_delegation_hint(mock_google: list[str]) -> None:
    with pytest.raises(RuntimeError, match=r"HTTP 403.*domain-wide delegation.*admin.directory.device.mobile.readonly"):
        invoke_table_function(wt.WorkspaceMobileDevicesFunction, named={"customer": "C0forbidden"})


def test_missing_credentials_is_a_clean_error() -> None:
    with pytest.raises(RuntimeError, match="no Google credential"):
        invoke_table_function(wt.WorkspaceUsersFunction)


def test_every_function_declares_pushdown_and_result_columns() -> None:
    for fn in wt.WORKSPACE_FUNCTIONS:
        assert fn.Meta.filter_pushdown and fn.Meta.auto_apply_filters, fn.Meta.name
        cols = json.loads(fn.Meta.tags["vgi.result_columns_schema"])
        assert [c["name"] for c in cols] == fn.FIXED_SCHEMA.names
    roles = json.loads(wt.WorkspaceRolesFunction.Meta.tags["vgi.result_columns_schema"])
    assert roles[-1]["type"] == "STRUCT(privilege_name VARCHAR, service_id VARCHAR)[]"


# ---------------------------------------------------------------------------
# Generic-hatch / transport fixes found while probing the Workspace APIs
# ---------------------------------------------------------------------------


def test_google_call_expands_usage_reports_despite_warnings(mock_google: list[str]) -> None:
    table = invoke_table_function(
        GoogleCallFunction,
        positional=("admin", "reports_v1", "userUsageReport.get", '{"userKey":"all","date":"2026-11-01"}'),
    )
    assert table.num_rows == 2  # one row per usageReports element, not one blob


def test_google_call_parameter_validation_is_clean(mock_google: list[str]) -> None:
    with pytest.raises(RuntimeError, match="startTime"):
        invoke_table_function(
            GoogleCallFunction,
            positional=(
                "admin",
                "reports_v1",
                "activities.list",
                '{"userKey":"all","applicationName":"login","startTime":"2026-11-01"}',
            ),
        )


def test_execute_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    from googleapiclient.errors import HttpError

    sleeps: list[float] = []
    monkeypatch.setattr(discovery, "_sleep", sleeps.append)

    class _Req:
        calls = 0

        def execute(self, num_retries: int = 0) -> dict[str, Any]:
            _Req.calls += 1
            if _Req.calls == 1:
                raise HttpError(Response({"status": "429", "retry-after": "7"}), b"{}")
            if _Req.calls == 2:
                raise HttpError(Response({"status": "429", "retry-after": "3600"}), b"{}")
            return {"ok": True}

    assert discovery.execute(_Req()) == {"ok": True}
    assert sleeps == [7.0, discovery.MAX_RETRY_AFTER]

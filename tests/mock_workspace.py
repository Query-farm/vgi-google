# ruff: noqa: E501 - fixture data mirrors the documented API payloads one object per line
"""Canned Google Workspace admin API responses, replayed against REAL discovery docs.

The discovery documents in ``tests/fixtures/discovery`` are Google's published
documents (trimmed to the methods used by ``scripts/trim_discovery.py``), so the
client built from them validates requests exactly as against live Google —
required path parameters, enum values, integer ranges and regex patterns (e.g.
the Reports ``startTime`` RFC-3339 pattern and the ``applicationName`` list).

:class:`FakeWorkspaceHttp` then plays the server: it routes by URL path, applies
the server-side semantics the adapters push down to (Directory search clauses,
Reports ``startTime``/``endTime``/``eventName``/``actorIpAddress``/``userKey``,
member ``roles``, role-assignment ``roleId``, usage ``parameters``, Alert Center
``filter``), pages with ``maxResults``/``pageToken``, and answers in the documented
response shapes (``users``, ``groups``, ``members``, ``organizationUnits``,
``chromeosdevices``, ``mobiledevices``, ``domains``, ``items``, ``usageReports``
+ ``warnings``, ``alerts``, Groups Settings resource). It rejects requests the
real API rejects (no customer/domain on ``users.list``, ``endTime`` before
``startTime``, a missing ``alt=json`` on Groups Settings) with HTTP 400.

Observation channel (tests only): every response object's ``etag`` carries the
request's query string, so the SQL suite can assert WHICH filters reached the
API — the worker re-applies the SQL predicate, so row results alone cannot
distinguish a pushed filter from a local one.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httplib2

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discovery"

WORKSPACE_DISCOVERY_DOCS: dict[tuple[str, str], dict[str, Any]] = {
    (api, version): json.loads((FIXTURES / f"{api}.{version}.json").read_text())
    for api, version in (
        ("admin", "directory_v1"),
        ("admin", "reports_v1"),
        ("alertcenter", "v1beta1"),
        ("groupssettings", "v1"),
    )
}

# ---------------------------------------------------------------------------
# Tenant fixture data (documented response shapes)
# ---------------------------------------------------------------------------

USERS: list[dict[str, Any]] = [
    {
        "kind": "admin#directory#user",
        "id": "100000000000000000001",
        "primaryEmail": "ada@example.com",
        "name": {"givenName": "Ada", "familyName": "Lovelace", "fullName": "Ada Lovelace"},
        "isAdmin": True,
        "isDelegatedAdmin": False,
        "suspended": False,
        "archived": False,
        "orgUnitPath": "/Engineering",
        "isEnrolledIn2Sv": False,
        "isEnforcedIn2Sv": False,
        "creationTime": "2020-01-02T03:04:05.000Z",
        "lastLoginTime": "2026-11-10T08:00:00.000Z",
        "isMailboxSetup": True,
        "agreedToTerms": True,
        "changePasswordAtNextLogin": False,
        "includeInGlobalAddressList": True,
        "customerId": "C01abc234",
        "aliases": ["countess@example.com"],
        "emails": [{"address": "ada@example.com", "primary": True}],
        "phones": [{"value": "+1 555 0100", "type": "work"}],
    },
    {
        "kind": "admin#directory#user",
        "id": "100000000000000000002",
        "primaryEmail": "bob@example.com",
        "name": {"givenName": "Bob", "familyName": "Builder", "fullName": "Bob Builder"},
        "isAdmin": False,
        "isDelegatedAdmin": True,
        "suspended": False,
        "archived": False,
        "orgUnitPath": "/Sales",
        "isEnrolledIn2Sv": True,
        "isEnforcedIn2Sv": True,
        "creationTime": "2021-05-06T07:08:09.000Z",
        "lastLoginTime": "1970-01-01T00:00:00.000Z",
        "customerId": "C01abc234",
    },
    {
        "kind": "admin#directory#user",
        "id": "100000000000000000003",
        "primaryEmail": "cy@example.com",
        "name": {"givenName": "Cy", "familyName": "Press", "fullName": "Cy Press"},
        "isAdmin": False,
        "isDelegatedAdmin": False,
        "suspended": True,
        "suspensionReason": "ADMIN",
        "archived": False,
        "orgUnitPath": "/Sales/EMEA",
        "isEnrolledIn2Sv": False,
        "isEnforcedIn2Sv": False,
        "creationTime": "2022-09-10T11:12:13.000Z",
        "lastLoginTime": "2026-03-01T09:30:00.000Z",
        "customerId": "C01abc234",
    },
]

GROUPS: list[dict[str, Any]] = [
    {
        "kind": "admin#directory#group",
        "id": "g1",
        "email": "eng@example.com",
        "name": "Engineering",
        "description": "All engineers",
        "directMembersCount": "3",
        "adminCreated": True,
        "aliases": ["engineering@example.com"],
    },
    {
        "kind": "admin#directory#group",
        "id": "g2",
        "email": "sales@example.com",
        "name": "Sales",
        "description": "Sales team",
        "directMembersCount": "12",
        "adminCreated": False,
    },
]

MEMBERS: dict[str, list[dict[str, Any]]] = {
    "eng@example.com": [
        {"kind": "admin#directory#member", "id": "100000000000000000001", "email": "ada@example.com",
         "role": "OWNER", "type": "USER", "status": "ACTIVE", "delivery_settings": "ALL_MAIL"},
        {"kind": "admin#directory#member", "id": "100000000000000000002", "email": "bob@example.com",
         "role": "MANAGER", "type": "USER", "status": "ACTIVE", "delivery_settings": "DIGEST"},
        {"kind": "admin#directory#member", "id": "x9", "email": "partner@other.org",
         "role": "MEMBER", "type": "EXTERNAL", "status": "ACTIVE", "delivery_settings": "ALL_MAIL"},
    ]
}  # fmt: skip

ORG_UNITS = [
    {"kind": "admin#directory#orgUnit", "orgUnitId": "id:03ph8a2z1", "orgUnitPath": "/Engineering",
     "name": "Engineering", "description": "Eng", "parentOrgUnitId": "id:03ph8a2z0", "parentOrgUnitPath": "/"},
    {"kind": "admin#directory#orgUnit", "orgUnitId": "id:03ph8a2z2", "orgUnitPath": "/Sales",
     "name": "Sales", "parentOrgUnitId": "id:03ph8a2z0", "parentOrgUnitPath": "/"},
    {"kind": "admin#directory#orgUnit", "orgUnitId": "id:03ph8a2z3", "orgUnitPath": "/Sales/EMEA",
     "name": "EMEA", "parentOrgUnitId": "id:03ph8a2z2", "parentOrgUnitPath": "/Sales"},
]  # fmt: skip

DOMAINS = [
    {"kind": "admin#directory#domain", "domainName": "example.com", "isPrimary": True, "verified": True,
     "creationTime": "1577934245000", "domainAliases": [{"domainAliasName": "example.net", "verified": True}]},
    {"kind": "admin#directory#domain", "domainName": "example.org", "isPrimary": False, "verified": False,
     "creationTime": "1704067200000"},
]  # fmt: skip

ROLES = [
    {"kind": "admin#directory#role", "roleId": "9170516996784129", "roleName": "_SEED_ADMIN_ROLE",
     "roleDescription": "Super Admin", "isSystemRole": True, "isSuperAdminRole": True,
     "rolePrivileges": [{"privilegeName": "SUPER_ADMIN", "serviceId": "01ci93xb3tmzyin"}]},
    {"kind": "admin#directory#role", "roleId": "9170516996784130", "roleName": "Helpdesk",
     "roleDescription": "Password resets", "isSystemRole": False, "isSuperAdminRole": False,
     "rolePrivileges": [{"privilegeName": "USERS_RETRIEVE", "serviceId": "00haapch16h1ysv"},
                        {"privilegeName": "USERS_UPDATE", "serviceId": "00haapch16h1ysv"}]},
]  # fmt: skip

ROLE_ASSIGNMENTS = [
    {"kind": "admin#directory#roleAssignment", "roleAssignmentId": "91000001", "roleId": "9170516996784129",
     "assignedTo": "100000000000000000001", "assigneeType": "user", "scopeType": "CUSTOMER"},
    {"kind": "admin#directory#roleAssignment", "roleAssignmentId": "91000002", "roleId": "9170516996784130",
     "assignedTo": "100000000000000000002", "assigneeType": "user", "scopeType": "ORG_UNIT",
     "orgUnitId": "03ph8a2z2", "expirationDetails": {"expireTime": "2027-01-01T00:00:00Z"}},
]  # fmt: skip

CHROMEOS = [
    {"kind": "admin#directory#chromeosdevice", "deviceId": "dev-1", "serialNumber": "5CD1", "status": "ACTIVE",
     "model": "Acer Chromebook 314", "osVersion": "126.0.6478.132", "orgUnitPath": "/Classrooms",
     "lastSync": "2026-11-12T10:00:00.000Z", "autoUpdateThrough": "2026-06-01T00:00:00Z",
     "recentUsers": [{"type": "USER_TYPE_MANAGED", "email": "ada@example.com"}],
     "cpuInfo": [{"model": "Intel Celeron N4500"}]},
    {"kind": "admin#directory#chromeosdevice", "deviceId": "dev-2", "serialNumber": "5CD2", "status": "ACTIVE",
     "model": "HP Chromebook 11", "orgUnitPath": "/Engineering",
     "lastSync": "2026-11-13T10:00:00.000Z", "autoUpdateThrough": "2030-06-01T00:00:00Z"},
]  # fmt: skip

MOBILE = [
    {"kind": "admin#directory#mobiledevice", "resourceId": "AFiQxQ8", "deviceId": "android-1",
     "email": ["bob@example.com"], "name": ["Bob Builder"], "model": "Pixel 8", "os": "Android 15",
     "type": "ANDROID", "status": "APPROVED", "firstSync": "2025-01-01T00:00:00.000Z",
     "lastSync": "2026-09-01T00:00:00.000Z", "deviceCompromisedStatus": "No compromise detected",
     "encryptionStatus": "Encrypted", "securityPatchLevel": "1727740800000",
     "applications": [{"packageName": "com.example"}]},
]  # fmt: skip


def _activity(i: int, app: str, when: str, actor: str, ip: str, events: list[tuple[str, str, list[dict]]]) -> dict:
    return {
        "kind": "admin#reports#activity",
        "id": {
            "time": when,
            "uniqueQualifier": str(-1234567890 - i),
            "applicationName": app,
            "customerId": "C01abc234",
        },
        "actor": {"callerType": "USER", "email": actor, "profileId": f"1000000000000000000{i:02d}"},
        "ipAddress": ip,
        "ownerDomain": "example.com",
        "events": [{"type": t, "name": n, "parameters": p} for t, n, p in events],
    }


ACTIVITIES: dict[str, list[dict[str, Any]]] = {
    "login": [
        _activity(1, "login", "2026-11-01T09:00:00.000Z", "ada@example.com", "203.0.113.5",
                  [("login", "login_success", [{"name": "login_type", "value": "google_password"},
                                                {"name": "is_suspicious", "boolValue": False}])]),
        _activity(2, "login", "2026-11-02T10:30:00.000Z", "cy@example.com", "198.51.100.7",
                  [("login", "login_failure", [{"name": "login_type", "value": "google_password"},
                                                {"name": "login_failure_type", "value": "login_failure_invalid_password"}]),
                   ("login", "login_challenge", [{"name": "login_challenge_method", "multiValue": ["password"]}])]),
        _activity(3, "login", "2026-11-05T23:59:59.000Z", "cy@example.com", "198.51.100.7",
                  [("login", "login_failure", [{"name": "login_failure_type", "value": "login_failure_invalid_password"}])]),
        _activity(4, "login", "2026-11-09T12:00:00.000Z", "ada@example.com", "203.0.113.5",
                  [("login", "login_failure", [{"name": "login_failure_type", "value": "login_failure_invalid_second_factor"}])]),
    ],
    "admin": [
        _activity(5, "admin", "2026-11-03T15:00:00.000Z", "ada@example.com", "203.0.113.5",
                  [("USER_SETTINGS", "CHANGE_PASSWORD", [{"name": "USER_EMAIL", "value": "bob@example.com"}]),
                   ("USER_SETTINGS", "SUSPEND_USER", [{"name": "USER_EMAIL", "value": "cy@example.com"}])]),
    ],
    "token": [
        _activity(6, "token", "2026-11-04T08:00:00.000Z", "bob@example.com", "192.0.2.1",
                  [("auth", "authorize", [{"name": "app_name", "value": "Example CRM"},
                                          {"name": "scope", "multiValue": ["https://www.googleapis.com/auth/drive"]},
                                          {"name": "scope_data", "multiMessageValue": [
                                              {"parameter": [{"name": "scope_name", "value": "drive"},
                                                             {"name": "product_bucket", "multiValue": ["DRIVE"]}]}]}])]),
    ],
}  # fmt: skip

USAGE: dict[str, dict[str, list[dict[str, Any]]]] = {
    "customer": {
        "2026-11-10": [
            {"kind": "admin#reports#usageReport", "date": "2026-11-10",
             "entity": {"type": "CUSTOMER", "customerId": "C01abc234"},
             "parameters": [{"name": "accounts:num_users", "intValue": "3"},
                            {"name": "accounts:used_quota_in_mb", "intValue": "20480"},
                            {"name": "accounts:apps_admin_enabled", "boolValue": True},
                            {"name": "gmail:last_interaction_time", "datetimeValue": "2026-11-10T22:00:00.000Z"}]}
        ],
    },
    "user": {
        day: [
            {"kind": "admin#reports#usageReport", "date": day,
             "entity": {"type": "USER", "customerId": "C01abc234", "userEmail": email, "profileId": pid},
             "parameters": [{"name": "gmail:num_emails_sent", "intValue": str(sent)},
                            {"name": "accounts:is_2sv_enrolled", "boolValue": enrolled}]}
            for email, pid, sent, enrolled in (("ada@example.com", "p1", n, False), ("bob@example.com", "p2", n * 2, True))
        ]
        for day, n in (("2026-11-01", 4), ("2026-11-02", 5), ("2026-11-03", 6))
    },
}  # fmt: skip

ALERTS = [
    {"customerId": "C01abc234", "alertId": "a-1", "type": "Suspicious login", "source": "Google identity",
     "createTime": "2026-11-02T10:31:00Z", "startTime": "2026-11-02T10:30:00Z", "updateTime": "2026-11-02T11:00:00Z",
     "deleted": False, "metadata": {"status": "NOT_STARTED", "severity": "HIGH"},
     "data": {"@type": "type.googleapis.com/google.apps.alertcenter.type.SuspiciousActivity", "email": "cy@example.com"}},
    {"customerId": "C01abc234", "alertId": "a-2", "type": "User reported phishing", "source": "Gmail phishing",
     "createTime": "2026-10-20T09:00:00Z", "startTime": "2026-10-20T08:55:00Z", "deleted": False,
     "metadata": {"status": "CLOSED", "severity": "MEDIUM", "assignee": "ada@example.com"}},
    {"customerId": "C01abc234", "alertId": "a-3", "type": "Device compromised", "source": "Mobile device management",
     "createTime": "2026-11-12T07:00:00Z", "startTime": "2026-11-12T06:59:00Z", "deleted": False,
     "metadata": {"status": "IN_PROGRESS", "severity": "LOW"}},
]  # fmt: skip

GROUP_SETTINGS = {
    "eng@example.com": {
        "kind": "groupsSettings#groups", "email": "eng@example.com", "name": "Engineering",
        "whoCanJoin": "INVITED_CAN_JOIN", "whoCanViewMembership": "ALL_MEMBERS_CAN_VIEW",
        "whoCanViewGroup": "ALL_MEMBERS_CAN_VIEW", "whoCanPostMessage": "ALL_IN_DOMAIN_CAN_POST",
        "whoCanContactOwner": "ANYONE_CAN_CONTACT", "whoCanDiscoverGroup": "ALL_IN_DOMAIN_CAN_DISCOVER",
        "messageModerationLevel": "MODERATE_NONE", "spamModerationLevel": "MODERATE",
        "allowExternalMembers": "true", "allowWebPosting": "true", "membersCanPostAsTheGroup": "false",
        "includeInGlobalAddressList": "true", "isArchived": "false", "archiveOnly": "false",
        "whoCanAddReferences": "NONE", "maxMessageBytes": 26214400,
    }
}  # fmt: skip


# ---------------------------------------------------------------------------
# Server-side semantics
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """An HTTP error the fake server answers with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _q(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    return qs.get(key, [default])[0]


def _directory_query(items: list[dict], query: str, fields: dict[str, str]) -> list[dict]:
    """Apply Directory search clauses (``field=value``, implicit AND)."""
    for clause in re.findall(r"(\w+)=('(?:[^'\\]|\\.)*'|\S+)", query):
        name, raw = clause
        value = raw[1:-1].replace("\\'", "'") if raw.startswith("'") else raw
        key = fields.get(name)
        if key is None:
            raise ApiError(400, f"Invalid Input: unsupported query field {name}")
        if name == "orgUnitPath":
            items = [i for i in items if (i.get(key) or "") == value or (i.get(key) or "").startswith(value + "/")]
        elif name == "email":
            items = [i for i in items if value in [i.get(key), *(i.get("aliases") or [])]]
        elif value in ("true", "false"):
            items = [i for i in items if bool(i.get(key)) == (value == "true")]
        else:
            items = [i for i in items if i.get(key) == value]
    return items


def _paginate(
    items: list[Any], qs: dict[str, list[str]], size_key: str = "maxResults", default: int = 100
) -> tuple[list[Any], str | None]:
    start = int(_q(qs, "pageToken", "0") or 0)
    size = int(_q(qs, size_key, str(default)))
    page = items[start : start + size]
    return page, (str(start + size) if start + size < len(items) else None)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _alert_filter(alerts: list[dict], expr: str) -> list[dict]:
    """A small evaluator for the Alert Center filter subset the adapter emits."""
    if not expr:
        return alerts

    def one(alert: dict, clause: str) -> bool:
        clause = clause.strip()
        while clause.startswith("(") and clause.endswith(")"):
            clause = clause[1:-1].strip()
        if " OR " in clause:
            return any(one(alert, c) for c in clause.split(" OR "))
        m = re.fullmatch(r'(\w+)\s*(>=|<=|=|<|>)\s*"(.*)"', clause)
        if not m:
            raise ApiError(400, f"Invalid filter: {clause}")
        field, op, value = m.groups()
        actual = alert.get(field) if field not in ("severity", "status") else (alert.get("metadata") or {}).get(field)
        if actual is None:
            return False
        if field.endswith("Time"):
            a, v = _ts(actual), _ts(value)
            return {"=": a == v, ">=": a >= v, "<=": a <= v, "<": a < v, ">": a > v}[op]
        return op == "=" and actual == value

    return [a for a in alerts if all(one(a, c) for c in expr.split(" AND "))]


class FakeWorkspaceHttp:
    """An ``httplib2``-style transport serving the Workspace admin fixtures.

    ``REQUESTS`` records every request URI (in-process tests assert pushdown on it).
    """

    REQUESTS: ClassVar[list[str]] = []

    def request(  # noqa: D401 - transport contract
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        redirections: int = 1,
        connection_type: Any = None,
    ) -> tuple[httplib2.Response, bytes]:
        FakeWorkspaceHttp.REQUESTS.append(uri)
        parsed = urlparse(uri)
        qs = parse_qs(parsed.query)
        query = urlencode(sorted((k, v[0]) for k, v in qs.items() if k not in ("alt", "pageToken")))
        echo = f"{unquote(parsed.path).rsplit('/', 3)[-3] if '/activity/' in parsed.path else ''}?{query}"
        try:
            payload = self._route(unquote(parsed.path), qs)
        except ApiError as exc:
            err = {"error": {"code": exc.status, "message": str(exc), "errors": [{"reason": "invalid"}]}}
            return httplib2.Response({"status": str(exc.status)}), json.dumps(err).encode()
        for key in ("users", "groups", "members", "chromeosdevices", "mobiledevices", "items", "alerts"):
            for obj in payload.get(key, []):
                obj["etag"] = f"q:{echo}"
        resp = httplib2.Response({"status": "200", "content-type": "application/json"})
        return resp, json.dumps(payload).encode("utf-8")

    def _route(self, path: str, qs: dict[str, list[str]]) -> dict[str, Any]:
        d = "/admin/directory/v1/"
        if "C0forbidden" in path or _q(qs, "customer") == "C0forbidden":
            # What the Directory API answers a service account without delegation.
            raise ApiError(403, "Not Authorized to access this resource/api")
        if path == d + "users":
            if not (_q(qs, "customer") or _q(qs, "domain")):
                raise ApiError(400, "Bad Request: customer or domain is required")
            fields = {"email": "primaryEmail", "orgUnitPath": "orgUnitPath", "isAdmin": "isAdmin",
                      "isDelegatedAdmin": "isDelegatedAdmin", "isSuspended": "suspended", "isArchived": "archived",
                      "isEnrolledIn2Sv": "isEnrolledIn2Sv", "isEnforcedIn2Sv": "isEnforcedIn2Sv"}  # fmt: skip
            items = _directory_query([dict(u) for u in USERS], _q(qs, "query"), fields)
            page, nxt = _paginate(items, qs)
            return {"kind": "admin#directory#users", "users": page, **({"nextPageToken": nxt} if nxt else {})}
        if path == d + "groups":
            items = [dict(g) for g in GROUPS]
            if _q(qs, "userKey"):
                items = [g for g in items if any(m["email"] == _q(qs, "userKey") for m in MEMBERS.get(g["email"], []))]
            else:
                items = _directory_query(items, _q(qs, "query"), {"email": "email", "name": "name"})
            page, nxt = _paginate(items, qs, default=200)
            return {"kind": "admin#directory#groups", "groups": page, **({"nextPageToken": nxt} if nxt else {})}
        if m := re.fullmatch(d + r"groups/([^/]+)/members", path):
            items = [dict(x) for x in MEMBERS.get(m.group(1), [])]
            if not items and m.group(1) not in MEMBERS:
                raise ApiError(404, "Resource Not Found: groupKey")
            if _q(qs, "roles"):
                wanted = set(_q(qs, "roles").split(","))
                items = [x for x in items if x["role"] in wanted]
            page, nxt = _paginate(items, qs, default=200)
            return {"kind": "admin#directory#members", "members": page, **({"nextPageToken": nxt} if nxt else {})}
        if re.fullmatch(d + r"customer/[^/]+/orgunits", path):
            items = ORG_UNITS
            base = _q(qs, "orgUnitPath")
            if base:
                items = [o for o in items if o["parentOrgUnitPath"] == base]
            return {"kind": "admin#directory#org_units", "organizationUnits": items}
        if re.fullmatch(d + r"customer/[^/]+/domains", path):
            return {"kind": "admin#directory#domains", "domains": DOMAINS}
        if re.fullmatch(d + r"customer/[^/]+/roles", path):
            page, nxt = _paginate([dict(r) for r in ROLES], qs, default=100)
            return {"kind": "admin#directory#roles", "items": page, **({"nextPageToken": nxt} if nxt else {})}
        if re.fullmatch(d + r"customer/[^/]+/roleassignments", path):
            if _q(qs, "roleId") and _q(qs, "userKey"):
                raise ApiError(400, "Invalid Input: roleId and userKey cannot both be set")
            items = [dict(a) for a in ROLE_ASSIGNMENTS]
            if _q(qs, "roleId"):
                items = [a for a in items if a["roleId"] == _q(qs, "roleId")]
            if _q(qs, "userKey"):
                users = {u["primaryEmail"]: u["id"] for u in USERS}
                key = users.get(_q(qs, "userKey"), _q(qs, "userKey"))
                items = [a for a in items if a["assignedTo"] == key]
            page, nxt = _paginate(items, qs, default=200)
            return {"kind": "admin#directory#roleAssignments", "items": page, **({"nextPageToken": nxt} if nxt else {})}
        if re.fullmatch(d + r"customer/[^/]+/devices/chromeos", path):
            items = [dict(x) for x in CHROMEOS]
            if _q(qs, "orgUnitPath"):
                items = [x for x in items if x["orgUnitPath"] == _q(qs, "orgUnitPath")]
            page, nxt = _paginate(items, qs)
            return {"kind": "admin#directory#chromeosdevices", "chromeosdevices": page,
                    **({"nextPageToken": nxt} if nxt else {})}  # fmt: skip
        if re.fullmatch(d + r"customer/[^/]+/devices/mobile", path):
            page, nxt = _paginate([dict(x) for x in MOBILE], qs)
            return {"kind": "admin#directory#mobiledevices", "mobiledevices": page,
                    **({"nextPageToken": nxt} if nxt else {})}  # fmt: skip
        if m := re.fullmatch(r"/admin/reports/v1/activity/users/([^/]+)/applications/([^/]+)", path):
            return self._activities(m.group(1), m.group(2), qs)
        if m := re.fullmatch(r"/admin/reports/v1/usage/dates/([0-9-]+)", path):
            return self._usage("customer", "all", m.group(1), qs)
        if m := re.fullmatch(r"/admin/reports/v1/usage/users/([^/]+)/dates/([0-9-]+)", path):
            return self._usage("user", m.group(1), m.group(2), qs)
        if path == "/v1beta1/alerts":
            items = _alert_filter([dict(a) for a in ALERTS], _q(qs, "filter"))
            page, nxt = _paginate(items, qs, size_key="pageSize")
            return {"alerts": page, **({"nextPageToken": nxt} if nxt else {})}
        if m := re.fullmatch(r"/groups/v1/groups/([^/]+)", path):
            if _q(qs, "alt") != "json":
                raise ApiError(400, "this fake only serves alt=json (the API default is atom)")
            if m.group(1) not in GROUP_SETTINGS:
                raise ApiError(404, "Resource Not Found: groupUniqueId")
            return dict(GROUP_SETTINGS[m.group(1)])
        raise ApiError(404, f"no fake route for {path}")

    @staticmethod
    def _activities(user_key: str, app: str, qs: dict[str, list[str]]) -> dict[str, Any]:
        items = [json.loads(json.dumps(a)) for a in ACTIVITIES.get(app, [])]
        start, end = _q(qs, "startTime"), _q(qs, "endTime")
        if start and end and _ts(end) < _ts(start):
            raise ApiError(400, "Start time must be less than or equal to the end time")
        if start:
            items = [a for a in items if _ts(a["id"]["time"]) >= _ts(start)]
        if end:
            items = [a for a in items if _ts(a["id"]["time"]) <= _ts(end)]
        if user_key != "all":
            items = [a for a in items if a["actor"].get("email") == user_key]
        if _q(qs, "actorIpAddress"):
            items = [a for a in items if a.get("ipAddress") == _q(qs, "actorIpAddress")]
        if _q(qs, "eventName"):
            items = [a for a in items if any(e["name"] == _q(qs, "eventName") for e in a["events"])]
        items.sort(key=lambda a: a["id"]["time"], reverse=True)  # the API returns newest first
        page, nxt = _paginate(items, qs, default=1000)
        return {"kind": "admin#reports#activities", "items": page, **({"nextPageToken": nxt} if nxt else {})}

    @staticmethod
    def _usage(report: str, user_key: str, day: str, qs: dict[str, list[str]]) -> dict[str, Any]:
        reports = [json.loads(json.dumps(r)) for r in USAGE[report].get(day, [])]
        if user_key != "all":
            reports = [r for r in reports if r["entity"].get("userEmail") == user_key]
        if _q(qs, "parameters"):
            wanted = set(_q(qs, "parameters").split(","))
            for r in reports:
                r["parameters"] = [p for p in r["parameters"] if p["name"] in wanted]
        page, nxt = _paginate(reports, qs, default=1000) if report == "user" else (reports, None)
        body: dict[str, Any] = {"kind": "admin#reports#usageReports", "usageReports": page}
        if not reports:
            body["warnings"] = [{"code": "PARTIAL_DATA_AVAILABLE", "message": f"Data for {day} is not available"}]
        if nxt:
            body["nextPageToken"] = nxt
        return body

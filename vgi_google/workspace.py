"""Google Workspace admin adapters: Admin SDK Directory + Reports, Alert Center, Groups Settings.

Each class here is a curated READ adapter in the same shape as the Sheets /
Drive / Calendar / YouTube ones (``api`` / ``version`` / ``scopes`` / ``schema``
and ``fetch_page``), plus one Workspace-specific hook:

* ``pushdown(filters) -> dict`` translates the SQL ``WHERE`` predicates DuckDB
  pushes into the API's own server-side filters (Directory ``query`` clauses,
  Reports ``startTime`` / ``endTime`` / ``eventName`` / ``actorIpAddress`` /
  ``userKey`` / ``parameters``, Alert Center ``filter`` expressions, member
  ``roles``, role-assignment ``roleId`` / ``userKey``). The result is a plain
  JSON-able dict stored in the scan state, so it survives HTTP transport.

Pushdown is **always a superset**: every translated filter returns at least the
rows the SQL predicate selects (bounds are widened by a second, prefix/OU-chain
matches are broader than equality). The table functions set
``auto_apply_filters`` so the worker re-applies the exact SQL predicate to every
batch before DuckDB sees it — results are correct whether or not a predicate
was pushed.

Auth: these APIs read a Workspace tenant's admin data, so they need a service
account with **domain-wide delegation** impersonating an administrator (the
``subject`` field of the ``google_service_account`` secret), or a service
account that has itself been granted an admin role. Each adapter requests only
its read-only scope; those exact scopes must be authorized for the client ID in
the Admin console (Security > API controls > Domain-wide delegation).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa

from .adapters.base import Page
from .discovery import execute, resolve_method
from .schema_utils import LIST_VARCHAR, TIMESTAMPTZ, afield, json_dumps, json_field, parse_int, parse_timestamp

_SCOPE = "https://www.googleapis.com/auth/"

#: Reports ``startTime`` / ``endTime`` and Alert Center filters accept RFC-3339.
_RFC3339 = "%Y-%m-%dT%H:%M:%S.%fZ"

#: Widen pushed time bounds by this much on each side so an inclusive/exclusive
#: mismatch with the API can never drop a row (the SQL predicate is re-applied).
_TIME_SLACK = timedelta(seconds=1)

#: Usage reports are fetched one day per request; cap the days a range expands to.
MAX_USAGE_DAYS = 92


# ---------------------------------------------------------------------------
# Pushdown helpers (PushdownFilters -> plain Python values)
# ---------------------------------------------------------------------------


def _discrete(filters: Any, column: str) -> list[Any] | None:
    """The discrete values (``=`` / ``IN`` / OR-of-``=``) a column is pinned to, else None."""
    if filters is None:
        return None
    try:
        values = filters.get_column_values(column)
    except (AttributeError, TypeError, ValueError):
        return None
    if values is None:
        return None
    out = [v for v in values.to_pylist() if v is not None]
    return out or None


def _single(filters: Any, column: str) -> Any:
    """The one value a column is pinned to by ``=`` (or a one-element ``IN``), else None."""
    values = _discrete(filters, column)
    return values[0] if values is not None and len(values) == 1 else None


def _as_utc(value: Any) -> datetime | None:
    """Coerce a pushed timestamp/date constant to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return parse_timestamp(value) if isinstance(value, str) else None


def _time_bounds(
    filters: Any, column: str, *, slack: timedelta = _TIME_SLACK
) -> tuple[datetime | None, datetime | None]:
    """(lower, upper) bounds a timestamp/date column is constrained to, widened by ``slack``."""
    if filters is None:
        return None, None
    try:
        bounds = filters.get_column_bounds(column)
    except (AttributeError, TypeError, ValueError):
        return None, None
    if bounds is None:
        return None, None
    low = _as_utc(bounds.min_value.as_py()) if bounds.min_value is not None else None
    high = _as_utc(bounds.max_value.as_py()) if bounds.max_value is not None else None
    return (low - slack if low else None), (high + slack if high else None)


def _rfc3339(value: datetime) -> str:
    """Format an aware datetime as Google's RFC-3339 (millisecond, Z) form."""
    return value.astimezone(UTC).strftime(_RFC3339)[:-4] + "Z"


def _quote_directory(value: str) -> str:
    """Single-quote a Directory search value, escaping embedded quotes."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _quote_alert(value: str) -> str:
    """Double-quote an Alert Center filter value."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _join_query(*clauses: str) -> str:
    """Space-join non-empty Directory query clauses (implicit AND)."""
    return " ".join(c for c in clauses if c)


def _page_size(args: Any, default: int, maximum: int) -> int:
    """Resolve the per-request page size from ``page_size`` (0 = adapter default)."""
    requested = getattr(args, "page_size", 0) or default
    return max(1, min(int(requested), maximum))


def _bool(value: Any) -> bool | None:
    """Groups Settings sends booleans as the strings 'true' / 'false'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _epoch_ms(value: Any) -> datetime | None:
    """Parse an int64 epoch-milliseconds value (Directory domains / mobile devices)."""
    ms = parse_int(value)
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _real_time(value: Any) -> datetime | None:
    """Parse a timestamp, mapping Directory's 'never' sentinel (the Unix epoch) to NULL."""
    parsed = parse_timestamp(value)
    if parsed is None or parsed.year <= 1970:
        return None
    return parsed


def _extra(item: dict[str, Any], mapped: set[str]) -> str | None:
    """JSON of every response field not already mapped to a typed column."""
    skip = mapped | {"kind", "etag"}
    return json_dumps({k: v for k, v in item.items() if k not in skip})


def _customer(args: Any) -> str:
    return getattr(args, "customer", "") or "my_customer"


# ---------------------------------------------------------------------------
# Directory: users
# ---------------------------------------------------------------------------

#: Boolean user columns pushed as Directory ``field=true|false`` query clauses.
_USER_BOOL_QUERY = {
    "is_admin": "isAdmin",
    "is_delegated_admin": "isDelegatedAdmin",
    "suspended": "isSuspended",
    "archived": "isArchived",
    "is_enrolled_in_2sv": "isEnrolledIn2Sv",
    "is_enforced_in_2sv": "isEnforcedIn2Sv",
}

USERS_SCHEMA = pa.schema(
    [
        afield("id", pa.string(), "Immutable user ID.", nullable=False),
        afield("primary_email", pa.string(), "Primary e-mail address."),
        afield("full_name", pa.string(), "Full name."),
        afield("given_name", pa.string(), "Given (first) name."),
        afield("family_name", pa.string(), "Family (last) name."),
        afield("is_admin", pa.bool_(), "Whether the user is a super administrator."),
        afield("is_delegated_admin", pa.bool_(), "Whether the user is a delegated administrator."),
        afield("suspended", pa.bool_(), "Whether the account is suspended."),
        afield("suspension_reason", pa.string(), "Why the account was suspended, when suspended."),
        afield("archived", pa.bool_(), "Whether the account is archived."),
        afield("org_unit_path", pa.string(), "Organizational unit path, e.g. '/Sales'."),
        afield("is_enrolled_in_2sv", pa.bool_(), "Whether the user is enrolled in 2-step verification."),
        afield("is_enforced_in_2sv", pa.bool_(), "Whether 2-step verification is enforced for the user."),
        afield("creation_time", TIMESTAMPTZ, "When the account was created."),
        afield("last_login_time", TIMESTAMPTZ, "Last sign-in time; NULL when the user never signed in."),
        afield("deletion_time", TIMESTAMPTZ, "When the account was deleted (show_deleted scans only)."),
        afield("is_mailbox_setup", pa.bool_(), "Whether the Gmail mailbox has been created."),
        afield("agreed_to_terms", pa.bool_(), "Whether the user accepted the Terms of Service."),
        afield("change_password_at_next_login", pa.bool_(), "Whether a password change is forced at next sign-in."),
        afield("include_in_global_address_list", pa.bool_(), "Whether the user is listed in the global address list."),
        afield("is_guest_user", pa.bool_(), "Whether the account is a guest user."),
        afield("customer_id", pa.string(), "Workspace customer ID that owns the account."),
        afield("aliases", LIST_VARCHAR, "E-mail aliases."),
        afield("non_editable_aliases", LIST_VARCHAR, "Read-only aliases (e.g. from secondary domains)."),
        afield("recovery_email", pa.string(), "Recovery e-mail address."),
        afield("thumbnail_photo_url", pa.string(), "Profile photo URL."),
        json_field("extra", "Remaining user fields (emails, phones, organizations, relations, custom schemas), JSON."),
    ]
)

_USER_MAPPED = {
    "id",
    "primaryEmail",
    "name",
    "isAdmin",
    "isDelegatedAdmin",
    "suspended",
    "suspensionReason",
    "archived",
    "orgUnitPath",
    "isEnrolledIn2Sv",
    "isEnforcedIn2Sv",
    "creationTime",
    "lastLoginTime",
    "deletionTime",
    "isMailboxSetup",
    "agreedToTerms",
    "changePasswordAtNextLogin",
    "includeInGlobalAddressList",
    "isGuestUser",
    "customerId",
    "aliases",
    "nonEditableAliases",
    "recoveryEmail",
    "thumbnailPhotoUrl",
    "thumbnailPhotoEtag",
}


class UsersAdapter:
    """Directory ``users.list``: one row per user account."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.user.readonly"]
    schema = USERS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Translate SQL predicates into Directory user-search clauses."""
        clauses: list[str] = []
        email = _single(filters, "primary_email")
        if isinstance(email, str):
            # `email=` matches primary addresses AND aliases: a superset.
            clauses.append(f"email={_quote_directory(email)}")
        ou = _single(filters, "org_unit_path")
        if isinstance(ou, str):
            # `orgUnitPath=` matches the OU and every OU beneath it: a superset.
            clauses.append(f"orgUnitPath={_quote_directory(ou)}")
        for column, field in _USER_BOOL_QUERY.items():
            value = _single(filters, column)
            if isinstance(value, bool):
                clauses.append(f"{field}={'true' if value else 'false'}")
        return {"query": _join_query(*clauses)} if clauses else {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of users."""
        params: dict[str, Any] = {
            "maxResults": _page_size(args, 500, 500),
            "projection": args.projection or "basic",
            "viewType": "admin_view",
        }
        if args.domain:
            params["domain"] = args.domain
        else:
            params["customer"] = _customer(args)
        query = _join_query(args.query, pushed.get("query", ""))
        if query:
            params["query"] = query
        if args.show_deleted:
            params["showDeleted"] = "true"
        if args.order_by:
            params["orderBy"] = args.order_by
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "users.list")(**params))
        return Page(rows=[self._map(u) for u in payload.get("users", [])], next_cursor=payload.get("nextPageToken"))

    @staticmethod
    def _map(u: dict[str, Any]) -> dict[str, Any]:
        name = u.get("name") or {}
        return {
            "id": u.get("id"),
            "primary_email": u.get("primaryEmail"),
            "full_name": name.get("fullName"),
            "given_name": name.get("givenName"),
            "family_name": name.get("familyName"),
            "is_admin": u.get("isAdmin"),
            "is_delegated_admin": u.get("isDelegatedAdmin"),
            "suspended": u.get("suspended"),
            "suspension_reason": u.get("suspensionReason"),
            "archived": u.get("archived"),
            "org_unit_path": u.get("orgUnitPath"),
            "is_enrolled_in_2sv": u.get("isEnrolledIn2Sv"),
            "is_enforced_in_2sv": u.get("isEnforcedIn2Sv"),
            "creation_time": _real_time(u.get("creationTime")),
            "last_login_time": _real_time(u.get("lastLoginTime")),
            "deletion_time": _real_time(u.get("deletionTime")),
            "is_mailbox_setup": u.get("isMailboxSetup"),
            "agreed_to_terms": u.get("agreedToTerms"),
            "change_password_at_next_login": u.get("changePasswordAtNextLogin"),
            "include_in_global_address_list": u.get("includeInGlobalAddressList"),
            "is_guest_user": u.get("isGuestUser"),
            "customer_id": u.get("customerId"),
            "aliases": u.get("aliases"),
            "non_editable_aliases": u.get("nonEditableAliases"),
            "recovery_email": u.get("recoveryEmail"),
            "thumbnail_photo_url": u.get("thumbnailPhotoUrl"),
            "extra": _extra(u, _USER_MAPPED),
        }


# ---------------------------------------------------------------------------
# Directory: groups / members
# ---------------------------------------------------------------------------

GROUPS_SCHEMA = pa.schema(
    [
        afield("id", pa.string(), "Immutable group ID.", nullable=False),
        afield("email", pa.string(), "Group e-mail address."),
        afield("name", pa.string(), "Group display name."),
        afield("description", pa.string(), "Group description."),
        afield("direct_members_count", pa.int64(), "Number of direct members (users and groups)."),
        afield("admin_created", pa.bool_(), "Whether an administrator (not a user) created the group."),
        afield("aliases", LIST_VARCHAR, "E-mail aliases of the group."),
        afield("non_editable_aliases", LIST_VARCHAR, "Read-only aliases of the group."),
    ]
)


class GroupsAdapter:
    """Directory ``groups.list``: one row per group (optionally those a user belongs to)."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.group.readonly"]
    schema = GROUPS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Translate ``email`` / ``name`` equality into Directory group-search clauses."""
        clauses: list[str] = []
        for column in ("email", "name"):
            value = _single(filters, column)
            if isinstance(value, str):
                clauses.append(f"{column}={_quote_directory(value)}")
        return {"query": _join_query(*clauses)} if clauses else {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of groups."""
        params: dict[str, Any] = {"maxResults": _page_size(args, 200, 200)}
        if args.user_key:
            # userKey lists the groups a member belongs to; the search query is
            # not combinable with it, so pushed clauses are dropped (the SQL
            # predicate is still applied by the worker).
            params["userKey"] = args.user_key
        else:
            if args.domain:
                params["domain"] = args.domain
            else:
                params["customer"] = _customer(args)
            query = _join_query(args.query, pushed.get("query", ""))
            if query:
                params["query"] = query
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "groups.list")(**params))
        rows = [
            {
                "id": g.get("id"),
                "email": g.get("email"),
                "name": g.get("name"),
                "description": g.get("description"),
                "direct_members_count": parse_int(g.get("directMembersCount")),
                "admin_created": g.get("adminCreated"),
                "aliases": g.get("aliases"),
                "non_editable_aliases": g.get("nonEditableAliases"),
            }
            for g in payload.get("groups", [])
        ]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))


MEMBERS_SCHEMA = pa.schema(
    [
        afield("group_key", pa.string(), "The group that was listed (as passed in).", nullable=False),
        afield("id", pa.string(), "Member's immutable ID (user or group)."),
        afield("email", pa.string(), "Member's e-mail address."),
        afield("role", pa.string(), "OWNER, MANAGER or MEMBER."),
        afield("type", pa.string(), "USER, GROUP, CUSTOMER or EXTERNAL."),
        afield("status", pa.string(), "Membership status, e.g. ACTIVE or SUSPENDED."),
        afield("delivery_settings", pa.string(), "Mail delivery preference, e.g. ALL_MAIL or DIGEST."),
    ]
)

_MEMBER_ROLES = ("OWNER", "MANAGER", "MEMBER")


class MembersAdapter:
    """Directory ``members.list``: one row per member of one group."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.group.member.readonly"]
    schema = MEMBERS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Push ``role = ...`` / ``role IN (...)`` into the ``roles`` parameter."""
        roles = _discrete(filters, "role")
        if roles and all(isinstance(r, str) and r in _MEMBER_ROLES for r in roles):
            return {"roles": ",".join(sorted(set(roles)))}
        return {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of group members."""
        params: dict[str, Any] = {"groupKey": args.group_key, "maxResults": _page_size(args, 200, 200)}
        roles = args.roles or pushed.get("roles", "")
        if roles:
            params["roles"] = roles
        if args.include_derived_membership:
            params["includeDerivedMembership"] = True
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "members.list")(**params))
        rows = [
            {
                "group_key": args.group_key,
                "id": m.get("id"),
                "email": m.get("email"),
                "role": m.get("role"),
                "type": m.get("type"),
                "status": m.get("status"),
                "delivery_settings": m.get("delivery_settings"),
            }
            for m in payload.get("members", [])
        ]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))


# ---------------------------------------------------------------------------
# Directory: org units, domains, roles, role assignments
# ---------------------------------------------------------------------------

ORG_UNITS_SCHEMA = pa.schema(
    [
        afield("org_unit_id", pa.string(), "Immutable organizational-unit ID ('id:...').", nullable=False),
        afield("org_unit_path", pa.string(), "Full path, e.g. '/Engineering/Platform'."),
        afield("name", pa.string(), "Organizational-unit name."),
        afield("description", pa.string(), "Description."),
        afield("parent_org_unit_id", pa.string(), "Parent organizational-unit ID."),
        afield("parent_org_unit_path", pa.string(), "Parent organizational-unit path."),
        afield("block_inheritance", pa.bool_(), "Deprecated inheritance-blocking flag."),
    ]
)


class OrgUnitsAdapter:
    """Directory ``orgunits.list``: the organizational-unit tree (single response)."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.orgunit.readonly"]
    schema = ORG_UNITS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Org units are returned in one response; nothing to push."""
        del filters
        return {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch the org-unit tree."""
        del cursor, pushed
        params: dict[str, Any] = {"customerId": _customer(args), "type": args.type or "all"}
        if args.org_unit_path:
            params["orgUnitPath"] = args.org_unit_path
        payload = execute(resolve_method(service, "orgunits.list")(**params))
        rows = [
            {
                "org_unit_id": o.get("orgUnitId"),
                "org_unit_path": o.get("orgUnitPath"),
                "name": o.get("name"),
                "description": o.get("description"),
                "parent_org_unit_id": o.get("parentOrgUnitId"),
                "parent_org_unit_path": o.get("parentOrgUnitPath"),
                "block_inheritance": o.get("blockInheritance"),
            }
            for o in payload.get("organizationUnits", [])
        ]
        return Page(rows=rows, next_cursor=None)


DOMAINS_SCHEMA = pa.schema(
    [
        afield("domain_name", pa.string(), "Domain name.", nullable=False),
        afield("is_primary", pa.bool_(), "Whether this is the primary domain."),
        afield("verified", pa.bool_(), "Whether ownership of the domain is verified."),
        afield("creation_time", TIMESTAMPTZ, "When the domain was added."),
        afield("domain_aliases", LIST_VARCHAR, "Alias domain names of this domain."),
    ]
)


class DomainsAdapter:
    """Directory ``domains.list``: the tenant's domains (single response)."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.domain.readonly"]
    schema = DOMAINS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Domains are returned in one response; nothing to push."""
        del filters
        return {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch the domain list."""
        del cursor, pushed
        payload = execute(resolve_method(service, "domains.list")(customer=_customer(args)))
        rows = [
            {
                "domain_name": d.get("domainName"),
                "is_primary": d.get("isPrimary"),
                "verified": d.get("verified"),
                "creation_time": _epoch_ms(d.get("creationTime")),
                "domain_aliases": [a.get("domainAliasName") for a in d.get("domainAliases") or []] or None,
            }
            for d in payload.get("domains", [])
        ]
        return Page(rows=rows, next_cursor=None)


PRIVILEGE_TYPE = pa.list_(pa.struct([pa.field("privilege_name", pa.string()), pa.field("service_id", pa.string())]))

ROLES_SCHEMA = pa.schema(
    [
        afield("role_id", pa.string(), "Role ID.", nullable=False),
        afield("role_name", pa.string(), "Role name."),
        afield("role_description", pa.string(), "Role description."),
        afield("is_system_role", pa.bool_(), "Whether this is a pre-defined system role."),
        afield("is_super_admin_role", pa.bool_(), "Whether this is the super-admin role."),
        afield("privileges", PRIVILEGE_TYPE, "Granted privileges as LIST<STRUCT(privilege_name, service_id)>."),
    ]
)


class RolesAdapter:
    """Directory ``roles.list``: admin roles and their privileges."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.rolemanagement.readonly"]
    schema = ROLES_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """roles.list has no server-side filters."""
        del filters
        return {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of roles."""
        del pushed
        params: dict[str, Any] = {"customer": _customer(args), "maxResults": _page_size(args, 100, 100)}
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "roles.list")(**params))
        rows = [
            {
                "role_id": str(r["roleId"]) if r.get("roleId") is not None else None,
                "role_name": r.get("roleName"),
                "role_description": r.get("roleDescription"),
                "is_system_role": r.get("isSystemRole"),
                "is_super_admin_role": r.get("isSuperAdminRole"),
                "privileges": [
                    {"privilege_name": p.get("privilegeName"), "service_id": p.get("serviceId")}
                    for p in r.get("rolePrivileges") or []
                ]
                or None,
            }
            for r in payload.get("items", [])
        ]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))


ROLE_ASSIGNMENTS_SCHEMA = pa.schema(
    [
        afield("role_assignment_id", pa.string(), "Role-assignment ID.", nullable=False),
        afield("role_id", pa.string(), "Assigned role ID (join to roles.role_id)."),
        afield("assigned_to", pa.string(), "Unique ID of the assignee (user, group or service account)."),
        afield("assignee_type", pa.string(), "USER or GROUP."),
        afield("scope_type", pa.string(), "CUSTOMER (tenant-wide) or ORG_UNIT."),
        afield("org_unit_id", pa.string(), "Organizational unit the assignment is scoped to (ORG_UNIT scope)."),
        afield("condition", pa.string(), "Optional assignment condition expression."),
        afield("expire_time", TIMESTAMPTZ, "When a time-limited assignment expires."),
    ]
)


class RoleAssignmentsAdapter:
    """Directory ``roleAssignments.list``: who holds which admin role."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.rolemanagement.readonly"]
    schema = ROLE_ASSIGNMENTS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Push ``role_id =`` into ``roleId`` and ``assigned_to =`` into ``userKey``."""
        pushed: dict[str, Any] = {}
        role_id = _single(filters, "role_id")
        if isinstance(role_id, str):
            pushed["roleId"] = role_id
        assignee = _single(filters, "assigned_to")
        if isinstance(assignee, str):
            pushed["userKey"] = assignee
        return pushed

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of role assignments."""
        params: dict[str, Any] = {"customer": _customer(args), "maxResults": _page_size(args, 200, 200)}
        role_id = args.role_id or pushed.get("roleId", "")
        user_key = args.user_key or pushed.get("userKey", "")
        # The API accepts at most one of roleId / userKey per request.
        if role_id:
            params["roleId"] = role_id
        elif user_key:
            params["userKey"] = user_key
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "roleAssignments.list")(**params))
        rows = [
            {
                "role_assignment_id": str(a["roleAssignmentId"]) if a.get("roleAssignmentId") is not None else None,
                "role_id": str(a["roleId"]) if a.get("roleId") is not None else None,
                "assigned_to": a.get("assignedTo"),
                "assignee_type": a.get("assigneeType"),
                "scope_type": a.get("scopeType"),
                "org_unit_id": a.get("orgUnitId"),
                "condition": a.get("condition"),
                "expire_time": parse_timestamp((a.get("expirationDetails") or {}).get("expireTime")),
            }
            for a in payload.get("items", [])
        ]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))


# ---------------------------------------------------------------------------
# Directory: devices
# ---------------------------------------------------------------------------

CHROMEOS_SCHEMA = pa.schema(
    [
        afield("device_id", pa.string(), "Unique device ID.", nullable=False),
        afield("serial_number", pa.string(), "Hardware serial number."),
        afield("status", pa.string(), "Device status, e.g. ACTIVE, DISABLED, DEPROVISIONED."),
        afield("model", pa.string(), "Device model."),
        afield("os_version", pa.string(), "ChromeOS version."),
        afield("platform_version", pa.string(), "ChromeOS platform version."),
        afield("firmware_version", pa.string(), "Firmware version."),
        afield("org_unit_path", pa.string(), "Organizational unit the device is in."),
        afield("annotated_user", pa.string(), "Admin-annotated user."),
        afield("annotated_location", pa.string(), "Admin-annotated location."),
        afield("annotated_asset_id", pa.string(), "Admin-annotated asset ID."),
        afield("notes", pa.string(), "Admin notes."),
        afield("mac_address", pa.string(), "Wireless MAC address."),
        afield("last_sync", TIMESTAMPTZ, "Last policy sync with the management server."),
        afield("last_enrollment_time", TIMESTAMPTZ, "Most recent enrollment time."),
        afield("support_end_date", TIMESTAMPTZ, "Final date the device model is supported."),
        afield("auto_update_through", TIMESTAMPTZ, "Time after which the device stops receiving updates."),
        afield("boot_mode", pa.string(), "Boot mode: Verified or Dev."),
        afield("recent_users", LIST_VARCHAR, "E-mails of recent users, most recent first."),
        json_field("extra", "Remaining device fields (CPU/disk/RAM reports, active time ranges, network), JSON."),
    ]
)

_CHROMEOS_MAPPED = {
    "deviceId",
    "serialNumber",
    "status",
    "model",
    "osVersion",
    "platformVersion",
    "firmwareVersion",
    "orgUnitPath",
    "annotatedUser",
    "annotatedLocation",
    "annotatedAssetId",
    "notes",
    "macAddress",
    "lastSync",
    "lastEnrollmentTime",
    "supportEndDate",
    "autoUpdateThrough",
    "bootMode",
    "recentUsers",
}


class ChromeOsDevicesAdapter:
    """Directory ``chromeosdevices.list``: managed ChromeOS devices."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.device.chromeos.readonly"]
    schema = CHROMEOS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Push ``org_unit_path =`` into the exact-OU ``orgUnitPath`` parameter."""
        ou = _single(filters, "org_unit_path")
        return {"orgUnitPath": ou} if isinstance(ou, str) else {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of ChromeOS devices."""
        params: dict[str, Any] = {
            "customerId": _customer(args),
            "maxResults": _page_size(args, 200, 300),
            "projection": "FULL",
        }
        if args.query:
            params["query"] = args.query
        ou = args.org_unit_path or pushed.get("orgUnitPath", "")
        if ou:
            params["orgUnitPath"] = ou
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "chromeosdevices.list")(**params))
        rows = [self._map(d) for d in payload.get("chromeosdevices", [])]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))

    @staticmethod
    def _map(d: dict[str, Any]) -> dict[str, Any]:
        return {
            "device_id": d.get("deviceId"),
            "serial_number": d.get("serialNumber"),
            "status": d.get("status"),
            "model": d.get("model"),
            "os_version": d.get("osVersion"),
            "platform_version": d.get("platformVersion"),
            "firmware_version": d.get("firmwareVersion"),
            "org_unit_path": d.get("orgUnitPath"),
            "annotated_user": d.get("annotatedUser"),
            "annotated_location": d.get("annotatedLocation"),
            "annotated_asset_id": d.get("annotatedAssetId"),
            "notes": d.get("notes"),
            "mac_address": d.get("macAddress"),
            "last_sync": _real_time(d.get("lastSync")),
            "last_enrollment_time": _real_time(d.get("lastEnrollmentTime")),
            "support_end_date": _real_time(d.get("supportEndDate")),
            "auto_update_through": _real_time(d.get("autoUpdateThrough")),
            "boot_mode": d.get("bootMode"),
            "recent_users": [u["email"] for u in d.get("recentUsers") or [] if u.get("email")] or None,
            "extra": _extra(d, _CHROMEOS_MAPPED),
        }


MOBILE_SCHEMA = pa.schema(
    [
        afield("resource_id", pa.string(), "Unique ID the API uses for the device.", nullable=False),
        afield("device_id", pa.string(), "Device serial-style ID."),
        afield("email", LIST_VARCHAR, "E-mail addresses of the device's owners."),
        afield("name", LIST_VARCHAR, "Owner names."),
        afield("model", pa.string(), "Device model."),
        afield("os", pa.string(), "Operating system and version."),
        afield("type", pa.string(), "Device type, e.g. ANDROID, IOS_SYNC."),
        afield("status", pa.string(), "Status, e.g. APPROVED, BLOCKED, PENDING."),
        afield("first_sync", TIMESTAMPTZ, "First sync with the management server."),
        afield("last_sync", TIMESTAMPTZ, "Most recent sync."),
        afield("device_compromised_status", pa.string(), "Compromised status (e.g. rooted/jailbroken)."),
        afield("encryption_status", pa.string(), "Device encryption status."),
        afield("device_password_status", pa.string(), "Whether a device password is set."),
        afield("security_patch_level", TIMESTAMPTZ, "Android security patch level date."),
        afield("serial_number", pa.string(), "Hardware serial number."),
        afield("manufacturer", pa.string(), "Device manufacturer."),
        afield("user_agent", pa.string(), "User agent of the sync client."),
        json_field("extra", "Remaining device fields (applications, other accounts, network), JSON."),
    ]
)

_MOBILE_MAPPED = {
    "resourceId",
    "deviceId",
    "email",
    "name",
    "model",
    "os",
    "type",
    "status",
    "firstSync",
    "lastSync",
    "deviceCompromisedStatus",
    "encryptionStatus",
    "devicePasswordStatus",
    "securityPatchLevel",
    "serialNumber",
    "manufacturer",
    "userAgent",
}


class MobileDevicesAdapter:
    """Directory ``mobiledevices.list``: managed mobile devices."""

    api = "admin"
    version = "directory_v1"
    scopes = [_SCOPE + "admin.directory.device.mobile.readonly"]
    schema = MOBILE_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Mobile-device search syntax is free text; nothing is translated."""
        del filters
        return {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of mobile devices."""
        del pushed
        params: dict[str, Any] = {
            "customerId": _customer(args),
            "maxResults": _page_size(args, 100, 100),
            "projection": "FULL",
        }
        if args.query:
            params["query"] = args.query
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "mobiledevices.list")(**params))
        rows = [
            {
                "resource_id": d.get("resourceId"),
                "device_id": d.get("deviceId"),
                "email": d.get("email"),
                "name": d.get("name"),
                "model": d.get("model"),
                "os": d.get("os"),
                "type": d.get("type"),
                "status": d.get("status"),
                "first_sync": _real_time(d.get("firstSync")),
                "last_sync": _real_time(d.get("lastSync")),
                "device_compromised_status": d.get("deviceCompromisedStatus"),
                "encryption_status": d.get("encryptionStatus"),
                "device_password_status": d.get("devicePasswordStatus"),
                "security_patch_level": _epoch_ms(d.get("securityPatchLevel")),
                "serial_number": d.get("serialNumber"),
                "manufacturer": d.get("manufacturer"),
                "user_agent": d.get("userAgent"),
                "extra": _extra(d, _MOBILE_MAPPED),
            }
            for d in payload.get("mobiledevices", [])
        ]
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))


# ---------------------------------------------------------------------------
# Reports: activities (audit log)
# ---------------------------------------------------------------------------

ACTIVITIES_SCHEMA = pa.schema(
    [
        afield("time", TIMESTAMPTZ, "When the activity occurred.", nullable=False),
        afield("application_name", pa.string(), "Audit application, e.g. 'login', 'admin', 'drive'."),
        afield("unique_qualifier", pa.string(), "Disambiguates activities with the same time."),
        afield("customer_id", pa.string(), "Workspace customer ID."),
        afield("actor_email", pa.string(), "Primary e-mail of the actor, when there is one."),
        afield("actor_profile_id", pa.string(), "Workspace profile ID of the actor."),
        afield("actor_caller_type", pa.string(), "Actor type, e.g. USER or KEY."),
        afield("actor_key", pa.string(), "OAuth consumer key / robot ID when caller type is KEY."),
        afield("ip_address", pa.string(), "IP address the action was performed from."),
        afield("owner_domain", pa.string(), "Domain affected by the event."),
        afield("event_index", pa.int32(), "Position of this event within the activity record (0-based)."),
        afield("event_type", pa.string(), "Event type, e.g. 'login' or 'USER_SETTINGS'."),
        afield("event_name", pa.string(), "Event name, e.g. 'login_failure' or 'CHANGE_PASSWORD'."),
        json_field("parameters", "Event parameters as a JSON object keyed by parameter name."),
        json_field("network_info", "Network details (IP ASNs, region), JSON."),
        json_field("resource_details", "Resources the activity acted on, JSON."),
        afield("etag", pa.string(), "ETag of the activity record."),
    ]
)


def _nested_value(p: dict[str, Any]) -> Any:
    """Reduce one Reports (nested) parameter to its Python value."""
    if "value" in p:
        return p["value"]
    if "intValue" in p:
        return parse_int(p["intValue"])
    if "boolValue" in p:
        return p["boolValue"]
    if "multiValue" in p:
        return list(p["multiValue"])
    if "multiIntValue" in p:
        return [parse_int(v) for v in p["multiIntValue"]]
    if "multiBoolValue" in p:
        return list(p["multiBoolValue"])
    if "messageValue" in p:
        return _params_object((p["messageValue"] or {}).get("parameter") or [])
    if "multiMessageValue" in p:
        return [_params_object((m or {}).get("parameter") or []) for m in p["multiMessageValue"]]
    return None


def _params_object(params: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn a Reports ``parameters`` array into a ``{name: value}`` object."""
    return {str(p.get("name")): _nested_value(p) for p in params if p.get("name") is not None}


class ActivitiesAdapter:
    """Reports ``activities.list``: one row per audit **event** (activities flattened)."""

    api = "admin"
    version = "reports_v1"
    scopes = [_SCOPE + "admin.reports.audit.readonly"]
    schema = ACTIVITIES_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Push time bounds, event name, actor and IP into the API's own filters."""
        pushed: dict[str, Any] = {}
        low, high = _time_bounds(filters, "time")
        if low is not None:
            pushed["startTime"] = _rfc3339(low)
        if high is not None:
            pushed["endTime"] = _rfc3339(high)
        event = _single(filters, "event_name")
        if isinstance(event, str):
            pushed["eventName"] = event
        actor = _single(filters, "actor_email")
        if isinstance(actor, str):
            pushed["userKey"] = actor
        ip = _single(filters, "ip_address")
        if isinstance(ip, str):
            pushed["actorIpAddress"] = ip
        return pushed

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of activities and flatten each activity's events."""
        user_key = args.user_key or "all"
        if user_key == "all" and pushed.get("userKey"):
            user_key = pushed["userKey"]
        params: dict[str, Any] = {
            "userKey": user_key,
            "applicationName": args.application,
            "maxResults": _page_size(args, 1000, 1000),
        }
        start = _rfc3339_arg(args.start_time) or pushed.get("startTime")
        end = _rfc3339_arg(args.end_time) or pushed.get("endTime")
        if start:
            params["startTime"] = start
        if end:
            params["endTime"] = end
        event = args.event_name or pushed.get("eventName")
        if event:
            params["eventName"] = event
        ip = args.actor_ip_address or pushed.get("actorIpAddress")
        if ip:
            params["actorIpAddress"] = ip
        if args.filters:
            params["filters"] = args.filters
        if args.org_unit_id:
            params["orgUnitID"] = args.org_unit_id
        if args.group_id_filter:
            params["groupIdFilter"] = args.group_id_filter
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "activities.list")(**params))
        rows: list[dict[str, Any]] = []
        for item in payload.get("items", []):
            rows.extend(self._flatten(item))
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))

    @staticmethod
    def _flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        ident = item.get("id") or {}
        actor = item.get("actor") or {}
        base = {
            "time": parse_timestamp(ident.get("time")),
            "application_name": ident.get("applicationName"),
            "unique_qualifier": ident.get("uniqueQualifier"),
            "customer_id": ident.get("customerId"),
            "actor_email": actor.get("email"),
            "actor_profile_id": actor.get("profileId"),
            "actor_caller_type": actor.get("callerType"),
            "actor_key": actor.get("key"),
            "ip_address": item.get("ipAddress"),
            "owner_domain": item.get("ownerDomain"),
            "network_info": json_dumps(item.get("networkInfo")),
            "resource_details": json_dumps(item.get("resourceDetails")),
            "etag": item.get("etag"),
        }
        events = item.get("events") or []
        if not events:
            return [base]
        return [
            {
                **base,
                "event_index": index,
                "event_type": event.get("type"),
                "event_name": event.get("name"),
                "parameters": json_dumps(_params_object(event.get("parameters") or [])),
            }
            for index, event in enumerate(events)
        ]


def _rfc3339_arg(value: str) -> str | None:
    """Normalize a user-supplied date/datetime argument to the RFC-3339 the API requires."""
    if not value:
        return None
    parsed = parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"cannot parse {value!r} as a date or RFC-3339 timestamp")
    return _rfc3339(parsed)


# ---------------------------------------------------------------------------
# Reports: usage
# ---------------------------------------------------------------------------

USAGE_SCHEMA = pa.schema(
    [
        afield("date", pa.date32(), "Report date (UTC).", nullable=False),
        afield("entity_type", pa.string(), "CUSTOMER or USER."),
        afield("customer_id", pa.string(), "Workspace customer ID."),
        afield("user_email", pa.string(), "User e-mail (user reports)."),
        afield("profile_id", pa.string(), "User profile ID (user reports)."),
        afield("parameter", pa.string(), "Metric name as app:metric, e.g. 'accounts:num_users'.", nullable=False),
        afield("int_value", pa.int64(), "Integer metric value."),
        afield("bool_value", pa.bool_(), "Boolean metric value."),
        afield("string_value", pa.string(), "String metric value."),
        afield("datetime_value", TIMESTAMPTZ, "Timestamp metric value."),
        json_field("msg_value", "Structured metric value, JSON."),
    ]
)

_USAGE_REPORTS = {"customer": "customerUsageReports.get", "user": "userUsageReport.get"}


class UsageReportsAdapter:
    """Reports usage (customer / user): one row per (entity, date, metric).

    Usage reports are per-day, so a date RANGE expands to one request per day.
    The cursor is composite — ``"<day index>|<pageToken>"`` — so the scan can
    resume mid-range and mid-day across HTTP ticks.
    """

    api = "admin"
    version = "reports_v1"
    scopes = [_SCOPE + "admin.reports.usage.readonly"]
    schema = USAGE_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Push dates (``=`` / ``IN`` / range), metric names and the user into the request."""
        pushed: dict[str, Any] = {}
        dates = _discrete(filters, "date")
        if dates:
            pushed["dates"] = sorted({d.isoformat() for d in dates if isinstance(d, date)})
        else:
            # Whole days: no widening (an exclusive bound only adds one extra day).
            low, high = _time_bounds(filters, "date", slack=timedelta(0))
            if low is not None:
                pushed["date_min"] = low.date().isoformat()
            if high is not None:
                pushed["date_max"] = high.date().isoformat()
        names = _discrete(filters, "parameter")
        if names and all(isinstance(n, str) and ":" in n for n in names):
            pushed["parameters"] = ",".join(sorted(set(names)))
        user = _single(filters, "user_email")
        if isinstance(user, str):
            pushed["userKey"] = user
        return pushed

    @staticmethod
    def dates(args: Any, pushed: dict[str, Any], today: date | None = None) -> list[str]:
        """The report days to fetch, from ``date := ...`` or the pushed predicate."""
        if args.date:
            return [_parse_day(args.date).isoformat()]
        if pushed.get("dates"):
            return list(pushed["dates"])
        if pushed.get("date_min"):
            start = _parse_day(pushed["date_min"])
            end = _parse_day(pushed["date_max"]) if pushed.get("date_max") else (today or datetime.now(UTC).date())
            days = (end - start).days + 1
            if days > MAX_USAGE_DAYS:
                raise ValueError(f"date range spans {days} days; usage reports allow at most {MAX_USAGE_DAYS}")
            return [(start + timedelta(days=i)).isoformat() for i in range(max(days, 0))]
        raise ValueError(
            "usage_reports needs a report day: pass date := 'YYYY-MM-DD' or filter "
            "WHERE date = DATE '...' / date BETWEEN ... AND ..."
        )

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of one day's usage report."""
        report = (args.report or "customer").lower()
        if report not in _USAGE_REPORTS:
            raise ValueError(f"report must be one of {sorted(_USAGE_REPORTS)}, got {args.report!r}")
        days = self.dates(args, pushed)
        index, _, token = (cursor or "0|").partition("|")
        day_index = int(index)
        if day_index >= len(days):
            return Page(rows=[], next_cursor=None)
        params: dict[str, Any] = {"date": days[day_index]}
        if args.customer_id:
            params["customerId"] = args.customer_id
        parameters = args.parameters or pushed.get("parameters", "")
        if parameters:
            params["parameters"] = parameters
        if report == "user":
            user_key = args.user_key or "all"
            if user_key == "all" and pushed.get("userKey"):
                user_key = pushed["userKey"]
            params["userKey"] = user_key
            params["maxResults"] = _page_size(args, 1000, 1000)
            if args.filters:
                params["filters"] = args.filters
            if args.org_unit_id:
                params["orgUnitID"] = args.org_unit_id
        if token:
            params["pageToken"] = token
        payload = execute(resolve_method(service, _USAGE_REPORTS[report])(**params))
        rows = [row for item in payload.get("usageReports", []) for row in self._flatten(item)]
        next_token = payload.get("nextPageToken")
        if next_token:
            next_cursor: str | None = f"{day_index}|{next_token}"
        elif day_index + 1 < len(days):
            next_cursor = f"{day_index + 1}|"
        else:
            next_cursor = None
        return Page(rows=rows, next_cursor=next_cursor)

    @staticmethod
    def _flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        entity = item.get("entity") or {}
        day = _parse_day(item["date"]) if item.get("date") else None
        base = {
            "date": day,
            "entity_type": entity.get("type"),
            "customer_id": entity.get("customerId"),
            "user_email": entity.get("userEmail"),
            "profile_id": entity.get("profileId"),
        }
        return [
            {
                **base,
                "parameter": p.get("name"),
                "int_value": parse_int(p.get("intValue")),
                "bool_value": p.get("boolValue"),
                "string_value": p.get("stringValue"),
                "datetime_value": parse_timestamp(p.get("datetimeValue")),
                "msg_value": json_dumps(p.get("msgValue")),
            }
            for p in item.get("parameters") or []
            if p.get("name") is not None
        ]


def _parse_day(value: str) -> date:
    """Parse a 'YYYY-MM-DD' report day."""
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"cannot parse {value!r} as a YYYY-MM-DD date") from exc


# ---------------------------------------------------------------------------
# Alert Center
# ---------------------------------------------------------------------------

ALERTS_SCHEMA = pa.schema(
    [
        afield("alert_id", pa.string(), "Alert ID.", nullable=False),
        afield("customer_id", pa.string(), "Workspace customer ID."),
        afield("type", pa.string(), "Alert type, e.g. 'Suspicious login'."),
        afield("source", pa.string(), "Alert source, e.g. 'Google identity'."),
        afield("create_time", TIMESTAMPTZ, "When the alert was created."),
        afield("start_time", TIMESTAMPTZ, "When the first event behind the alert happened."),
        afield("end_time", TIMESTAMPTZ, "When the last event behind the alert happened."),
        afield("update_time", TIMESTAMPTZ, "When the alert was last updated."),
        afield("deleted", pa.bool_(), "Whether the alert is marked deleted."),
        afield("status", pa.string(), "Triage status from alert metadata, e.g. NOT_STARTED."),
        afield("severity", pa.string(), "Severity from alert metadata: HIGH, MEDIUM or LOW."),
        afield("assignee", pa.string(), "E-mail of the assigned administrator."),
        afield("security_investigation_tool_link", pa.string(), "Link to the security investigation tool."),
        json_field("data", "Type-specific alert payload, JSON."),
    ]
)


class AlertsAdapter:
    """Alert Center ``alerts.list``: security and system alerts."""

    api = "alertcenter"
    version = "v1beta1"
    scopes = [_SCOPE + "apps.alerts"]
    schema = ALERTS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """Translate time bounds and type/source equality into an Alert Center filter."""
        clauses: list[str] = []
        for column, field in (("create_time", "createTime"), ("start_time", "startTime")):
            low, high = _time_bounds(filters, column)
            if low is not None:
                clauses.append(f'{field} >= "{_rfc3339(low)}"')
            if high is not None:
                clauses.append(f'{field} <= "{_rfc3339(high)}"')
        for column in ("type", "source"):
            values = _discrete(filters, column)
            if values and all(isinstance(v, str) for v in values):
                ors = " OR ".join(f"{column}={_quote_alert(v)}" for v in values)
                clauses.append(f"({ors})" if len(values) > 1 else ors)
        return {"filter": " AND ".join(clauses)} if clauses else {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch one page of alerts."""
        params: dict[str, Any] = {"pageSize": _page_size(args, 100, 1000)}
        if args.customer_id:
            params["customerId"] = args.customer_id
        parts = [f"({args.filter})" if args.filter else "", pushed.get("filter", "")]
        expr = " AND ".join(p for p in parts if p)
        if expr:
            params["filter"] = expr
        if args.order_by:
            params["orderBy"] = args.order_by
        if cursor:
            params["pageToken"] = cursor
        payload = execute(resolve_method(service, "alerts.list")(**params))
        rows = []
        for a in payload.get("alerts", []):
            meta = a.get("metadata") or {}
            rows.append(
                {
                    "alert_id": a.get("alertId"),
                    "customer_id": a.get("customerId"),
                    "type": a.get("type"),
                    "source": a.get("source"),
                    "create_time": parse_timestamp(a.get("createTime")),
                    "start_time": parse_timestamp(a.get("startTime")),
                    "end_time": parse_timestamp(a.get("endTime")),
                    "update_time": parse_timestamp(a.get("updateTime")),
                    "deleted": a.get("deleted"),
                    "status": meta.get("status"),
                    "severity": meta.get("severity"),
                    "assignee": meta.get("assignee"),
                    "security_investigation_tool_link": a.get("securityInvestigationToolLink"),
                    "data": json_dumps(a.get("data")),
                }
            )
        return Page(rows=rows, next_cursor=payload.get("nextPageToken"))


# ---------------------------------------------------------------------------
# Groups Settings
# ---------------------------------------------------------------------------

#: (column, Groups Settings field, is-boolean) for the typed settings columns.
_GROUP_SETTINGS_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("name", "name", False),
    ("who_can_join", "whoCanJoin", False),
    ("who_can_view_membership", "whoCanViewMembership", False),
    ("who_can_view_group", "whoCanViewGroup", False),
    ("who_can_post_message", "whoCanPostMessage", False),
    ("who_can_contact_owner", "whoCanContactOwner", False),
    ("who_can_discover_group", "whoCanDiscoverGroup", False),
    ("who_can_moderate_members", "whoCanModerateMembers", False),
    ("who_can_moderate_content", "whoCanModerateContent", False),
    ("who_can_leave_group", "whoCanLeaveGroup", False),
    ("message_moderation_level", "messageModerationLevel", False),
    ("spam_moderation_level", "spamModerationLevel", False),
    ("allow_external_members", "allowExternalMembers", True),
    ("allow_web_posting", "allowWebPosting", True),
    ("members_can_post_as_the_group", "membersCanPostAsTheGroup", True),
    ("include_in_global_address_list", "includeInGlobalAddressList", True),
    ("enable_collaborative_inbox", "enableCollaborativeInbox", True),
    ("is_archived", "isArchived", True),
    ("archive_only", "archiveOnly", True),
)

GROUP_SETTINGS_SCHEMA = pa.schema(
    [
        afield("email", pa.string(), "Group e-mail address.", nullable=False),
        *[
            afield(
                column,
                pa.bool_() if is_bool else pa.string(),
                f"Groups Settings '{field}'" + (" (true/false)." if is_bool else "."),
            )
            for column, field, is_bool in _GROUP_SETTINGS_FIELDS
        ],
        json_field("settings", "The full Groups Settings resource, JSON."),
    ]
)


class GroupSettingsAdapter:
    """Groups Settings ``groups.get``: one row of access/moderation settings per group."""

    api = "groupssettings"
    version = "v1"
    scopes = [_SCOPE + "apps.groups.settings"]
    schema = GROUP_SETTINGS_SCHEMA

    def pushdown(self, filters: Any) -> dict[str, Any]:
        """A single-group lookup; nothing to push."""
        del filters
        return {}

    def fetch_page(self, service: Any, args: Any, cursor: str | None, pushed: dict[str, Any]) -> Page:
        """Fetch the settings of one group (the client requests ``alt=json``)."""
        del cursor, pushed
        payload = execute(resolve_method(service, "groups.get")(groupUniqueId=args.group_email))
        row: dict[str, Any] = {"email": payload.get("email") or args.group_email}
        for column, field, is_bool in _GROUP_SETTINGS_FIELDS:
            value = payload.get(field)
            row[column] = _bool(value) if is_bool else value
        settings = {k: v for k, v in payload.items() if k != "kind"}
        row["settings"] = json.dumps(settings, ensure_ascii=False, sort_keys=True) if settings else None
        return Page(rows=[row], next_cursor=None)

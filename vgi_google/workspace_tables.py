"""The ``google.workspace`` table functions: Google Workspace admin data as SQL.

Thirteen READ table functions over the Admin SDK Directory API (users, groups,
group members, org units, ChromeOS / mobile devices, domains, admin roles and
role assignments), the Admin SDK Reports API (audit-log activities and usage
reports), the Alert Center API and the Groups Settings API.

They share the worker's discovery-backed client and service-account auth, and
add **filter pushdown**: each function declares ``filter_pushdown`` +
``auto_apply_filters``; at init the SQL predicates DuckDB pushes are translated
by the adapter's ``pushdown()`` into the API's own server-side filters and
stored in the (serializable) scan state, and the worker re-applies the exact SQL
predicate to every batch, so a partially-pushable ``WHERE`` is still correct.

Pagination is the same one-page-per-tick, ``nextPageToken``-as-state driver as
the curated adapters, and every function is pinned to one worker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import (
    BindParams,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from . import auth, client, discovery
from . import workspace as ws
from .meta import object_tags
from .schema_utils import rows_to_batch

#: Reports audit applications (the ``applicationName`` pattern of the Reports API
#: discovery document, revision 20261111).
AUDIT_APPLICATIONS: tuple[str, ...] = (
    "access_evaluation",
    "access_transparency",
    "admin",
    "admin_data_action",
    "assignments",
    "calendar",
    "chat",
    "chrome",
    "classroom",
    "cloud_search",
    "contacts",
    "context_aware_access",
    "data_studio",
    "data_migration",
    "directory_sync",
    "drive",
    "gcp",
    "gmail",
    "gplus",
    "graduation",
    "groups",
    "groups_enterprise",
    "jamboard",
    "keep",
    "ldap",
    "login",
    "meet",
    "meet_hardware",
    "mobile",
    "profile",
    "rules",
    "saml",
    "token",
    "user_accounts",
    "vault",
    "gemini_in_workspace_apps",
    "tasks",
    "takeout",
    "voice",
    "chrome_sync",
    "workspace_studio",
    "gemini_notebook",
)

CAT_DIRECTORY = "Workspace directory"
CAT_REPORTS = "Audit and usage reports"
CAT_SECURITY = "Alerts and group settings"

#: Categories registry for the ``workspace`` schema (VGI413).
WORKSPACE_CATEGORIES: list[dict[str, str]] = [
    {
        "name": CAT_DIRECTORY,
        "description": (
            "Admin SDK Directory objects of a Google Workspace tenant: users, groups and their "
            "members, organizational units, domains, ChromeOS and mobile devices, admin roles and "
            "role assignments."
        ),
    },
    {
        "name": CAT_REPORTS,
        "description": (
            "Admin SDK Reports: the audit log (login, admin, Drive, token, SAML, groups and other "
            "application events) and the daily customer / user usage metrics."
        ),
    },
    {
        "name": CAT_SECURITY,
        "description": (
            "Alert Center security and system alerts, and per-group access and moderation settings "
            "from the Groups Settings API."
        ),
    },
]

_DWD_HINT = (
    " — Workspace admin APIs need a service account with domain-wide delegation: set 'subject' "
    "in the google_service_account secret to an administrator's e-mail, and authorize this "
    "function's scope ({scopes}) for the service account's client ID in the Admin console "
    "(Security > API controls > Domain-wide delegation)"
)


@dataclass(kw_only=True)
class _WorkspaceState(ArrowSerializableDataclass):
    """Externalized scan state: the page cursor plus the translated pushdown.

    Attributes:
        cursor: The adapter's opaque page cursor (Google's ``nextPageToken``,
            or the usage reports' ``"<day>|<token>"`` composite).
        emitted: Rows emitted so far (to honor ``count``).
        started: False until the first page has been fetched.
        done: True once the scan should stop.
        pushed_json: JSON of the API parameters translated from pushed SQL
            filters, computed once at init so it survives HTTP transport.
    """

    cursor: str | None = None
    emitted: int = 0
    started: bool = False
    done: bool = False
    pushed_json: str = "{}"


def _initial_state(cls: Any, params: ProcessParams[Any]) -> _WorkspaceState:
    """Translate the pushed SQL filters (if any) into the adapter's API parameters."""
    pushed: dict[str, Any] = {}
    init_call = params.init_call
    raw = getattr(init_call, "pushdown_filters", None) if init_call is not None else None
    if raw is not None:
        filters = cls.pushdown_filters(
            raw,
            join_keys=getattr(init_call, "join_keys", None),
            output_schema=getattr(init_call, "output_schema", None),
        )
        if filters is not None:
            pushed = cls._adapter.pushdown(filters)
    return _WorkspaceState(pushed_json=json.dumps(pushed, sort_keys=True))


#: Empty pages followed within one tick before yielding (bounds a pathological API).
_MAX_EMPTY_PAGES = 100


def _scopes_text(adapter: Any) -> str:
    return ", ".join(adapter.scopes)


def _run_tick(adapter: Any, params: ProcessParams[Any], state: _WorkspaceState, out: OutputCollector) -> None:
    """Fetch, emit and advance one page; convert every failure into a clean error."""
    count = params.args.count
    if state.done or state.emitted >= count or (state.started and state.cursor is None):
        out.finish()
        return
    from google.auth.exceptions import GoogleAuthError

    pushed = json.loads(state.pushed_json or "{}")
    try:
        service = client.build_client(
            adapter.api, adapter.version, secrets=params.secrets, scopes=adapter.scopes, require_auth=True
        )
        page = adapter.fetch_page(service, params.args, state.cursor, pushed)
        # A tick must emit or finish: skip over empty pages that still have a
        # successor (e.g. a usage-report day with no data inside a date range).
        skipped = 0
        while not page.rows and page.next_cursor is not None and skipped < _MAX_EMPTY_PAGES:
            page = adapter.fetch_page(service, params.args, page.next_cursor, pushed)
            skipped += 1
    except (discovery.GoogleApiError, auth.AuthError, GoogleAuthError) as exc:
        message = str(exc)
        if isinstance(exc, GoogleAuthError) or any(t in message for t in ("HTTP 401", "HTTP 403", "unauthorized")):
            message += _DWD_HINT.format(scopes=_scopes_text(adapter))
        raise RuntimeError(f"{type(adapter).__name__} failed: {message}") from exc
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{type(adapter).__name__} failed: {exc}") from exc

    state.started = True
    rows = page.rows[: count - state.emitted]
    if rows:
        out.emit(rows_to_batch(rows, params.output_schema))
        state.emitted += len(rows)
    state.cursor = page.next_cursor
    if page.next_cursor is None or state.emitted >= count:
        state.done = True
    if not rows:
        out.finish()


_DUCK_TYPES: dict[str, str] = {
    "string": "VARCHAR",
    "int64": "BIGINT",
    "int32": "INTEGER",
    "bool": "BOOLEAN",
    "date32[day]": "DATE",
    "timestamp[us, tz=UTC]": "TIMESTAMPTZ",
    "list<item: string>": "VARCHAR[]",
}


def _duck_type(field: pa.Field[Any]) -> str:
    """DuckDB type name of an output column (JSON columns are tagged VARCHAR)."""
    if pa.types.is_list(field.type) and pa.types.is_struct(field.type.value_type):
        inner = ", ".join(f"{f.name} {_DUCK_TYPES[str(f.type)]}" for f in field.type.value_type)
        return f"STRUCT({inner})[]"
    return _DUCK_TYPES[str(field.type)]


def result_columns(schema: pa.Schema) -> list[dict[str, str]]:
    """The ``vgi.result_columns_schema`` entries derived from an adapter schema."""
    return [
        {
            "name": f.name,
            "type": _duck_type(f),
            "description": (f.metadata or {}).get(b"comment", b"").decode("utf-8") or f.name,
        }
        for f in schema
    ]


def _tags(
    *,
    name: str,
    title: str,
    category: str,
    adapter: Any,
    what: str,
    when: str,
    pushdown: str,
    keywords: tuple[str, ...],
    examples: list[dict[str, str]],
) -> dict[str, str]:
    """Build the per-function lint/discovery tags from a few facts."""
    scope = _scopes_text(adapter)
    doc_llm = (
        f"## {name}\n\n{what}\n\n**When to use:** {when}\n\n"
        f"**Pushdown:** {pushdown} The worker re-applies the full SQL predicate, so results are exact "
        "even when only part of a `WHERE` clause could be pushed. `count` caps the rows fetched from "
        "the API (before any predicate the API could not apply).\n\n"
        f"**Auth:** a `google_service_account` secret with domain-wide delegation (`subject` = an "
        f"administrator's e-mail) and the read-only scope `{scope}` authorized for it."
    )
    doc_md = (
        f"# {title}\n\n{what}\n\n## Notes\n\n"
        f"- Pushdown: {pushdown}\n"
        f"- Required OAuth scope: `{scope}` (domain-wide delegation, impersonating an admin via the "
        "secret's `subject`).\n"
        "- Pages are fetched on demand; the attached example queries show typical filters."
    )
    return object_tags(
        title=title,
        category=category,
        doc_llm=doc_llm,
        doc_md=doc_md,
        keywords=keywords,
        relative_path="vgi_google/workspace_tables.py",
        result_columns=result_columns(adapter.schema),
        example_queries=examples,
    )


def _examples(examples: list[dict[str, str]]) -> list[FunctionExample]:
    return [FunctionExample(sql=e["sql"], description=e["description"]) for e in examples]


def _count_arg(default: int) -> Any:
    return Arg("count", default=default, doc="Maximum rows to return.", ge=1, le=10_000_000)


def _page_arg(maximum: int) -> Any:
    return Arg("page_size", default=0, doc="Rows per API request (0 = the API maximum).", ge=0, le=maximum)


_CUSTOMER_ARG: Any = Arg(
    "customer", default="my_customer", doc="Customer ID, or 'my_customer' for the admin's own tenant."
)

# Composed examples (two workspace functions in one query), shared with the schema listing.
COMPOSED_ADMIN_ROLE_HOLDERS: dict[str, str] = {
    "description": (
        "Who holds which admin role: join role assignments to role names and to the assignee's "
        "user record (users are matched on their immutable ID)"
    ),
    "sql": (
        "SELECT r.role_name, u.primary_email, a.scope_type "
        "FROM google.workspace.role_assignments() AS a "
        "JOIN google.workspace.roles() AS r USING (role_id) "
        "LEFT JOIN google.workspace.users() AS u ON u.id = a.assigned_to "
        "ORDER BY r.role_name, u.primary_email"
    ),
}

COMPOSED_FAILED_LOGINS_BY_OU: dict[str, str] = {
    "description": (
        "Failed sign-ins in November 2026 per organizational unit: login_failure events (time and "
        "event name pushed to the Reports API) joined to the actor's user record"
    ),
    "sql": (
        "SELECT u.org_unit_path, count(*) AS failures "
        "FROM google.workspace.activities('login') AS e "
        "JOIN google.workspace.users() AS u ON u.primary_email = e.actor_email "
        "WHERE e.event_name = 'login_failure' "
        "AND e.time >= TIMESTAMPTZ '2026-11-01 00:00:00+00' AND e.time < TIMESTAMPTZ '2026-12-01 00:00:00+00' "
        "GROUP BY ALL ORDER BY failures DESC"
    ),
}

COMPOSED_EXAMPLES: tuple[dict[str, str], ...] = (COMPOSED_ADMIN_ROLE_HOLDERS, COMPOSED_FAILED_LOGINS_BY_OU)


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _UsersArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    domain: Annotated[str, Arg("domain", default="", doc="Restrict to one domain (instead of the whole customer).")]
    query: Annotated[str, Arg("query", default="", doc='Directory user-search clauses, e.g. "orgName=Engineering".')]
    show_deleted: Annotated[bool, Arg("show_deleted", default=False, doc="List recently deleted users instead.")]
    order_by: Annotated[
        str,
        Arg(
            "order_by",
            default="",
            doc="Sort key: email, givenName or familyName.",
            choices=["", "email", "givenName", "familyName"],
        ),
    ]
    projection: Annotated[
        str,
        Arg(
            "projection", default="basic", doc="'basic' or 'full' (includes custom schemas).", choices=["basic", "full"]
        ),
    ]
    count: Annotated[int, _count_arg(100_000)]
    page_size: Annotated[int, _page_arg(500)]


_USERS_EXAMPLES = [
    {
        "description": "Super administrators without 2-step verification (both predicates are pushed down)",
        "sql": (
            "SELECT primary_email, last_login_time FROM google.workspace.users() "
            "WHERE is_admin AND NOT is_enrolled_in_2sv ORDER BY primary_email"
        ),
    },
    {
        "description": "Accounts in the Sales org unit that have not signed in since 2026-08-01",
        "sql": (
            "SELECT primary_email, last_login_time FROM google.workspace.users() "
            "WHERE org_unit_path = '/Sales' "
            "AND (last_login_time IS NULL OR last_login_time < TIMESTAMPTZ '2026-08-01 00:00:00+00') "
            "ORDER BY primary_email"
        ),
    },
    COMPOSED_ADMIN_ROLE_HOLDERS,
]


@init_single_worker
@bind_fixed_schema
class WorkspaceUsersFunction(TableFunctionGenerator[_UsersArgs, _WorkspaceState]):
    """Google Workspace user accounts (Admin SDK Directory ``users.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.UsersAdapter.schema
    _adapter: ClassVar[Any] = ws.UsersAdapter()

    class Meta:
        """Function metadata."""

        name = "users"
        description = "Google Workspace user accounts from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_USERS_EXAMPLES)
        tags = _tags(
            name="users",
            title="Workspace User Accounts",
            category=CAT_DIRECTORY,
            adapter=ws.UsersAdapter,
            what=(
                "Lists the tenant's user accounts with typed columns: identity (id, primary e-mail, "
                "names), admin flags, suspension/archival state, org unit, 2-step-verification "
                "enrollment and enforcement, creation and last sign-in times (NULL when never "
                "signed in), aliases, and the remaining profile fields as JSON in `extra`."
            ),
            when="security posture reviews (admins without 2SV, dormant accounts), joining people to audit events.",
            pushdown=(
                "`primary_email =` becomes the search clause `email=` (also matches aliases), "
                "`org_unit_path =` becomes `orgUnitPath=` (matches that unit and its children), and "
                "equality or bare predicates on `is_admin`, `is_delegated_admin`, `suspended`, "
                "`archived`, `is_enrolled_in_2sv`, `is_enforced_in_2sv` become `field=true|false` "
                "clauses, AND-ed with any `query` argument."
            ),
            keywords=("workspace users", "directory", "accounts", "admins", "2sv", "suspended", "org unit"),
            examples=_USERS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_UsersArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_UsersArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_UsersArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _GroupsArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    domain: Annotated[str, Arg("domain", default="", doc="Restrict to one domain (instead of the whole customer).")]
    user_key: Annotated[str, Arg("user_key", default="", doc="List only the groups this user or group belongs to.")]
    query: Annotated[str, Arg("query", default="", doc='Directory group-search clauses, e.g. "name:eng*".')]
    count: Annotated[int, _count_arg(100_000)]
    page_size: Annotated[int, _page_arg(200)]


_GROUPS_EXAMPLES = [
    {
        "description": "The largest groups by direct member count",
        "sql": (
            "SELECT email, name, direct_members_count FROM google.workspace.groups() "
            "ORDER BY direct_members_count DESC, email LIMIT 20"
        ),
    },
    {
        "description": "Groups a given user belongs to",
        "sql": "SELECT email, name FROM google.workspace.groups(user_key := 'ada@example.com') ORDER BY email",
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceGroupsFunction(TableFunctionGenerator[_GroupsArgs, _WorkspaceState]):
    """Google Workspace groups (Admin SDK Directory ``groups.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.GroupsAdapter.schema
    _adapter: ClassVar[Any] = ws.GroupsAdapter()

    class Meta:
        """Function metadata."""

        name = "groups"
        description = "Google Workspace groups from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_GROUPS_EXAMPLES)
        tags = _tags(
            name="groups",
            title="Workspace Groups Directory",
            category=CAT_DIRECTORY,
            adapter=ws.GroupsAdapter,
            what=(
                "Lists the tenant's groups (or, with `user_key`, the groups one user belongs to): id, "
                "e-mail, name, description, direct member count, whether an admin created it, aliases."
            ),
            when="inventory groups, find large or admin-created groups, or start a membership audit.",
            pushdown=(
                "`email =` and `name =` become the exact search clauses `email=` / `name=` (not "
                "combined with `user_key`, which the API cannot mix with a search query)."
            ),
            keywords=("workspace groups", "google groups", "distribution lists", "directory", "mailing lists"),
            examples=_GROUPS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_GroupsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_GroupsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_GroupsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _MembersArgs:
    group_key: Annotated[str, Arg(0, doc="The group's e-mail address; an alias or the unique group ID also works.")]
    roles: Annotated[str, Arg("roles", default="", doc="Comma list of OWNER, MANAGER, MEMBER to restrict to.")]
    include_derived_membership: Annotated[
        bool, Arg("include_derived_membership", default=False, doc="Also list members of nested groups.")
    ]
    count: Annotated[int, _count_arg(100_000)]
    page_size: Annotated[int, _page_arg(200)]


_MEMBERS_EXAMPLES = [
    {
        "description": "Owners and managers of a group (the role filter is pushed into the roles parameter)",
        "sql": (
            "SELECT email, role FROM google.workspace.group_members('eng@example.com') "
            "WHERE role IN ('OWNER', 'MANAGER') ORDER BY role, email"
        ),
    },
    {
        "description": "External or customer-wide members of a group",
        "sql": (
            "SELECT email, type FROM google.workspace.group_members('eng@example.com') "
            "WHERE type IN ('EXTERNAL', 'CUSTOMER') ORDER BY email"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceGroupMembersFunction(TableFunctionGenerator[_MembersArgs, _WorkspaceState]):
    """Members of one Google Workspace group (Admin SDK Directory ``members.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.MembersAdapter.schema
    _adapter: ClassVar[Any] = ws.MembersAdapter()

    class Meta:
        """Function metadata."""

        name = "group_members"
        description = "Members of a Google Workspace group from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_MEMBERS_EXAMPLES)
        tags = _tags(
            name="group_members",
            title="Workspace Group Membership",
            category=CAT_DIRECTORY,
            adapter=ws.MembersAdapter,
            what=(
                "Lists the members of one group: member id, e-mail, role (OWNER/MANAGER/MEMBER), "
                "type (USER/GROUP/CUSTOMER/EXTERNAL), status and mail delivery setting."
            ),
            when="audit who owns or can manage a group, or find external members.",
            pushdown="`role =` / `role IN (...)` becomes the `roles` request parameter.",
            keywords=("group members", "membership", "owners", "managers", "external members", "workspace"),
            examples=_MEMBERS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_MembersArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 200), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_MembersArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_MembersArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _OrgUnitsArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    org_unit_path: Annotated[str, Arg("org_unit_path", default="", doc="Start below this org unit (default: root).")]
    type: Annotated[
        str,
        Arg(
            "type",
            default="all",
            doc="'all', 'children' or 'allIncludingParent'.",
            choices=["all", "children", "allIncludingParent"],
        ),
    ]
    count: Annotated[int, _count_arg(100_000)]


_ORG_UNITS_EXAMPLES = [
    {
        "description": "The organizational-unit tree, ordered by path",
        "sql": (
            "SELECT org_unit_path, name, parent_org_unit_path FROM google.workspace.org_units() ORDER BY org_unit_path"
        ),
    },
    {
        "description": "Direct child units of /Engineering",
        "sql": (
            "SELECT org_unit_path FROM google.workspace.org_units(org_unit_path := '/Engineering', "
            "type := 'children') ORDER BY org_unit_path"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceOrgUnitsFunction(TableFunctionGenerator[_OrgUnitsArgs, _WorkspaceState]):
    """Organizational units (Admin SDK Directory ``orgunits.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.OrgUnitsAdapter.schema
    _adapter: ClassVar[Any] = ws.OrgUnitsAdapter()

    class Meta:
        """Function metadata."""

        name = "org_units"
        description = "Google Workspace organizational units from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_ORG_UNITS_EXAMPLES)
        tags = _tags(
            name="org_units",
            title="Workspace Organizational Units",
            category=CAT_DIRECTORY,
            adapter=ws.OrgUnitsAdapter,
            what=(
                "Lists the organizational-unit tree: unit id and path, name, description and the "
                "parent unit's id and path."
            ),
            when="map users and devices onto the OU hierarchy, or roll metrics up by unit.",
            pushdown="none (the tree comes back in one response; use `org_unit_path` / `type` to scope it).",
            keywords=("org units", "organizational units", "ou", "hierarchy", "workspace"),
            examples=_ORG_UNITS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_OrgUnitsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 50), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_OrgUnitsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_OrgUnitsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit the org units."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _DomainsArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    count: Annotated[int, _count_arg(10_000)]


_DOMAINS_EXAMPLES = [
    {
        "description": "Unverified domains in the tenant",
        "sql": (
            "SELECT domain_name, creation_time FROM google.workspace.domains() WHERE NOT verified ORDER BY domain_name"
        ),
    },
    {
        "description": "Every domain with its alias domains",
        "sql": "SELECT domain_name, is_primary, domain_aliases FROM google.workspace.domains() ORDER BY domain_name",
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceDomainsFunction(TableFunctionGenerator[_DomainsArgs, _WorkspaceState]):
    """The tenant's domains (Admin SDK Directory ``domains.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.DomainsAdapter.schema
    _adapter: ClassVar[Any] = ws.DomainsAdapter()

    class Meta:
        """Function metadata."""

        name = "domains"
        description = "Google Workspace domains from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_DOMAINS_EXAMPLES)
        tags = _tags(
            name="domains",
            title="Workspace Tenant Domains",
            category=CAT_DIRECTORY,
            adapter=ws.DomainsAdapter,
            what=(
                "Lists the tenant's domains: name, whether it is primary, whether ownership is "
                "verified, when it was added, and its alias domains."
            ),
            when="check domain verification or enumerate the addresses a tenant can send from.",
            pushdown="none (one response per tenant).",
            keywords=("domains", "domain aliases", "verified domains", "workspace tenant"),
            examples=_DOMAINS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_DomainsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 5), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_DomainsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_DomainsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit the domains."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _RolesArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    count: Annotated[int, _count_arg(10_000)]
    page_size: Annotated[int, _page_arg(100)]


_ROLES_EXAMPLES = [
    {
        "description": "Custom (non-system) admin roles and how many privileges each grants",
        "sql": (
            "SELECT role_name, len(privileges) AS privilege_count FROM google.workspace.roles() "
            "WHERE NOT is_system_role ORDER BY privilege_count DESC, role_name"
        ),
    },
    COMPOSED_ADMIN_ROLE_HOLDERS,
]


@init_single_worker
@bind_fixed_schema
class WorkspaceRolesFunction(TableFunctionGenerator[_RolesArgs, _WorkspaceState]):
    """Admin roles (Admin SDK Directory ``roles.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.RolesAdapter.schema
    _adapter: ClassVar[Any] = ws.RolesAdapter()

    class Meta:
        """Function metadata."""

        name = "roles"
        description = "Google Workspace admin roles and privileges from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_ROLES_EXAMPLES)
        tags = _tags(
            name="roles",
            title="Workspace Admin Roles",
            category=CAT_DIRECTORY,
            adapter=ws.RolesAdapter,
            what=(
                "Lists the tenant's admin roles: id, name, description, system/super-admin flags and "
                "the granted privileges as a list of (privilege_name, service_id) structs."
            ),
            when="review custom admin roles and what they can do; join to `role_assignments`.",
            pushdown="none (`roles.list` has no server-side filter).",
            keywords=("admin roles", "privileges", "rbac", "delegated admin", "workspace"),
            examples=_ROLES_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_RolesArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 50), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_RolesArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_RolesArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _RoleAssignmentsArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    role_id: Annotated[str, Arg("role_id", default="", doc="Only assignments of this role ID.")]
    user_key: Annotated[str, Arg("user_key", default="", doc="Only assignments held by this user (e-mail or ID).")]
    count: Annotated[int, _count_arg(100_000)]
    page_size: Annotated[int, _page_arg(200)]


_ROLE_ASSIGNMENTS_EXAMPLES = [
    {
        "description": "Assignments of one role (role_id is pushed into the roleId parameter)",
        "sql": (
            "SELECT assigned_to, assignee_type, scope_type FROM google.workspace.role_assignments() "
            "WHERE role_id = '9170516996784129' ORDER BY assigned_to"
        ),
    },
    COMPOSED_ADMIN_ROLE_HOLDERS,
]


@init_single_worker
@bind_fixed_schema
class WorkspaceRoleAssignmentsFunction(TableFunctionGenerator[_RoleAssignmentsArgs, _WorkspaceState]):
    """Admin role assignments (Admin SDK Directory ``roleAssignments.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.RoleAssignmentsAdapter.schema
    _adapter: ClassVar[Any] = ws.RoleAssignmentsAdapter()

    class Meta:
        """Function metadata."""

        name = "role_assignments"
        description = "Google Workspace admin role assignments from the Admin SDK Directory API"
        categories = ["google", "workspace", "directory"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_ROLE_ASSIGNMENTS_EXAMPLES)
        tags = _tags(
            name="role_assignments",
            title="Workspace Admin Role Assignments",
            category=CAT_DIRECTORY,
            adapter=ws.RoleAssignmentsAdapter,
            what=(
                "Lists who holds which admin role: assignment id, role id, assignee id and type, "
                "scope (tenant-wide or one org unit), optional condition and expiry time."
            ),
            when="privileged-access reviews; join `roles` for names and `users` for e-mails.",
            pushdown=(
                "`role_id =` becomes the `roleId` parameter and `assigned_to =` becomes `userKey` "
                "(the API takes one of the two per request; `role_id` wins)."
            ),
            keywords=("role assignments", "admin access", "privileged access", "rbac", "workspace"),
            examples=_ROLE_ASSIGNMENTS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_RoleAssignmentsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 100), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_RoleAssignmentsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_RoleAssignmentsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _ChromeOsArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    query: Annotated[str, Arg("query", default="", doc='ChromeOS device search, e.g. "status:provisioned".')]
    org_unit_path: Annotated[str, Arg("org_unit_path", default="", doc="Only devices in this org unit.")]
    count: Annotated[int, _count_arg(1_000_000)]
    page_size: Annotated[int, _page_arg(300)]


_CHROMEOS_EXAMPLES = [
    {
        "description": "Devices past their auto-update expiration",
        "sql": (
            "SELECT serial_number, model, auto_update_through FROM google.workspace.chromeos_devices() "
            "WHERE auto_update_through < TIMESTAMPTZ '2026-11-15 00:00:00+00' ORDER BY auto_update_through"
        ),
    },
    {
        "description": "Device count per model in one org unit (org_unit_path is pushed down)",
        "sql": (
            "SELECT model, count(*) AS devices FROM google.workspace.chromeos_devices() "
            "WHERE org_unit_path = '/Classrooms' GROUP BY model ORDER BY devices DESC, model"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceChromeOsDevicesFunction(TableFunctionGenerator[_ChromeOsArgs, _WorkspaceState]):
    """Managed ChromeOS devices (Admin SDK Directory ``chromeosdevices.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.ChromeOsDevicesAdapter.schema
    _adapter: ClassVar[Any] = ws.ChromeOsDevicesAdapter()

    class Meta:
        """Function metadata."""

        name = "chromeos_devices"
        description = "Managed ChromeOS devices from the Admin SDK Directory API"
        categories = ["google", "workspace", "devices"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_CHROMEOS_EXAMPLES)
        tags = _tags(
            name="chromeos_devices",
            title="Managed ChromeOS Devices",
            category=CAT_DIRECTORY,
            adapter=ws.ChromeOsDevicesAdapter,
            what=(
                "Lists enrolled ChromeOS devices: id, serial, status, model, OS/platform/firmware "
                "versions, org unit, admin annotations, sync/enrollment times, support and "
                "auto-update end dates, boot mode, recent users, and the hardware reports as JSON."
            ),
            when="fleet inventory, update-expiry planning, finding devices by user or location.",
            pushdown="`org_unit_path =` becomes the exact-unit `orgUnitPath` parameter.",
            keywords=("chromebooks", "chromeos", "devices", "fleet", "auto update", "workspace"),
            examples=_CHROMEOS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_ChromeOsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_ChromeOsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_ChromeOsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _MobileArgs:
    customer: Annotated[str, _CUSTOMER_ARG]
    query: Annotated[str, Arg("query", default="", doc='Mobile device search, e.g. "status:approved".')]
    count: Annotated[int, _count_arg(1_000_000)]
    page_size: Annotated[int, _page_arg(100)]


_MOBILE_EXAMPLES = [
    {
        "description": "Compromised or unencrypted mobile devices",
        "sql": (
            "SELECT email, model, os, device_compromised_status, encryption_status "
            "FROM google.workspace.mobile_devices() "
            "WHERE device_compromised_status = 'Compromised' OR encryption_status = 'Unencrypted' "
            "ORDER BY model"
        ),
    },
    {
        "description": "Mobile devices that have not synced since 2026-10-01",
        "sql": (
            "SELECT resource_id, model, last_sync FROM google.workspace.mobile_devices() "
            "WHERE last_sync < TIMESTAMPTZ '2026-10-01 00:00:00+00' ORDER BY last_sync"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceMobileDevicesFunction(TableFunctionGenerator[_MobileArgs, _WorkspaceState]):
    """Managed mobile devices (Admin SDK Directory ``mobiledevices.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.MobileDevicesAdapter.schema
    _adapter: ClassVar[Any] = ws.MobileDevicesAdapter()

    class Meta:
        """Function metadata."""

        name = "mobile_devices"
        description = "Managed mobile devices from the Admin SDK Directory API"
        categories = ["google", "workspace", "devices"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_MOBILE_EXAMPLES)
        tags = _tags(
            name="mobile_devices",
            title="Managed Mobile Devices",
            category=CAT_DIRECTORY,
            adapter=ws.MobileDevicesAdapter,
            what=(
                "Lists managed Android/iOS devices: ids, owner e-mails and names, model, OS, type, "
                "approval status, first/last sync, compromise/encryption/password status, security "
                "patch level, serial and manufacturer, and the remaining fields as JSON."
            ),
            when="mobile security posture: compromised, unencrypted or stale devices.",
            pushdown="none (use the `query` argument for the API's own device search).",
            keywords=("mobile devices", "android", "ios", "mdm", "endpoint", "workspace"),
            examples=_MOBILE_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_MobileArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_MobileArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_MobileArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _ActivitiesArgs:
    application: Annotated[
        str,
        Arg(
            0,
            doc="Audit application, e.g. 'login', 'admin', 'drive', 'token', 'saml', 'groups'.",
            choices=AUDIT_APPLICATIONS,
        ),
    ]
    user_key: Annotated[str, Arg("user_key", default="all", doc="'all' or one user's e-mail / profile ID.")]
    start_time: Annotated[str, Arg("start_time", default="", doc="Earliest event time (date or RFC-3339).")]
    end_time: Annotated[str, Arg("end_time", default="", doc="Latest event time (date or RFC-3339).")]
    event_name: Annotated[str, Arg("event_name", default="", doc="Only this event name, e.g. 'login_failure'.")]
    actor_ip_address: Annotated[str, Arg("actor_ip_address", default="", doc="Only events from this IP address.")]
    filters: Annotated[str, Arg("filters", default="", doc="Reports parameter filters, e.g. 'doc_type==document'.")]
    org_unit_id: Annotated[str, Arg("org_unit_id", default="", doc="Only actors in this org unit ('id:...').")]
    group_id_filter: Annotated[
        str, Arg("group_id_filter", default="", doc="Only actors in these groups ('id:...,id:...').")
    ]
    count: Annotated[int, _count_arg(100_000)]
    page_size: Annotated[int, _page_arg(1000)]


_ACTIVITIES_EXAMPLES = [
    {
        "description": "Failed sign-ins in the first week of November 2026 (time range and event name pushed down)",
        "sql": (
            "SELECT time, actor_email, ip_address, parameters ->> '$.login_failure_type' AS failure_type "
            "FROM google.workspace.activities('login') "
            "WHERE event_name = 'login_failure' "
            "AND time >= TIMESTAMPTZ '2026-11-01 00:00:00+00' AND time < TIMESTAMPTZ '2026-11-08 00:00:00+00' "
            "ORDER BY time"
        ),
    },
    {
        "description": "Admin console changes made by one administrator, by event name",
        "sql": (
            "SELECT event_name, count(*) AS n FROM google.workspace.activities('admin') "
            "WHERE actor_email = 'admin@example.com' GROUP BY event_name ORDER BY n DESC, event_name"
        ),
    },
    {
        "description": "Third-party OAuth grants (token audit), newest first",
        "sql": (
            "SELECT time, actor_email, parameters ->> '$.app_name' AS app "
            "FROM google.workspace.activities('token') WHERE event_name = 'authorize' ORDER BY time DESC LIMIT 100"
        ),
    },
    COMPOSED_FAILED_LOGINS_BY_OU,
]


@init_single_worker
@bind_fixed_schema
class WorkspaceActivitiesFunction(TableFunctionGenerator[_ActivitiesArgs, _WorkspaceState]):
    """The Workspace audit log (Admin SDK Reports ``activities.list``), one row per event."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.ActivitiesAdapter.schema
    _adapter: ClassVar[Any] = ws.ActivitiesAdapter()

    class Meta:
        """Function metadata."""

        name = "activities"
        description = "Google Workspace audit-log events from the Admin SDK Reports API"
        categories = ["google", "workspace", "audit"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_ACTIVITIES_EXAMPLES)
        tags = _tags(
            name="activities",
            title="Workspace Audit Log Events",
            category=CAT_REPORTS,
            adapter=ws.ActivitiesAdapter,
            what=(
                "Reads one audit application's activity log (login, admin, drive, token, saml, "
                "groups, calendar, meet, mobile, chrome, ...). Each activity is flattened to one row "
                "per event: time, actor (e-mail, profile id, caller type, key), IP address, owner "
                "domain, event type/name, and the event parameters as a JSON object keyed by "
                "parameter name (`parameters ->> '$.doc_title'`)."
            ),
            when="security investigations, admin change review, OAuth-grant audits, Drive sharing audits.",
            pushdown=(
                "bounds on `time` become `startTime` / `endTime` (widened by one second); "
                "`event_name =` becomes `eventName`; `actor_email =` becomes the `userKey` path "
                "parameter (when `user_key` is 'all'); `ip_address =` becomes `actorIpAddress`. "
                "Without a time bound the API returns up to the last 180 days."
            ),
            keywords=(
                "audit log",
                "reports api",
                "login events",
                "admin activity",
                "drive audit",
                "token audit",
                "saml",
                "security investigation",
            ),
            examples=_ACTIVITIES_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_ActivitiesArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_ActivitiesArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_ActivitiesArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _UsageArgs:
    report: Annotated[
        str,
        Arg(
            "report",
            default="customer",
            doc="'customer' (tenant totals) or 'user' (per user).",
            choices=["customer", "user"],
        ),
    ]
    date: Annotated[str, Arg("date", default="", doc="Report day 'YYYY-MM-DD' (or filter on the date column).")]
    parameters: Annotated[
        str,
        Arg("parameters", default="", doc="Comma list of metrics, e.g. 'accounts:num_users,gmail:num_emails_sent'."),
    ]
    user_key: Annotated[str, Arg("user_key", default="all", doc="User report only: 'all' or one user's e-mail.")]
    filters: Annotated[
        str, Arg("filters", default="", doc="User report only: metric filters, e.g. 'gmail:num_emails_sent>0'.")
    ]
    org_unit_id: Annotated[
        str, Arg("org_unit_id", default="", doc="User report only: users in this org unit ('id:...').")
    ]
    customer_id: Annotated[str, Arg("customer_id", default="", doc="Customer ID (default: the admin's tenant).")]
    count: Annotated[int, _count_arg(1_000_000)]
    page_size: Annotated[int, _page_arg(1000)]


_USAGE_EXAMPLES = [
    {
        "description": "Tenant-wide user and storage metrics for one day (date and metric names pushed down)",
        "sql": (
            "SELECT parameter, int_value FROM google.workspace.usage_reports() "
            "WHERE date = DATE '2026-11-10' AND parameter IN ('accounts:num_users', 'accounts:used_quota_in_mb') "
            "ORDER BY parameter"
        ),
    },
    {
        "description": "Daily e-mails sent per user over one week",
        "sql": (
            "SELECT date, user_email, int_value AS emails_sent "
            "FROM google.workspace.usage_reports(report := 'user', parameters := 'gmail:num_emails_sent') "
            "WHERE date BETWEEN DATE '2026-11-01' AND DATE '2026-11-07' ORDER BY date, emails_sent DESC"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceUsageReportsFunction(TableFunctionGenerator[_UsageArgs, _WorkspaceState]):
    """Daily Workspace usage metrics (Admin SDK Reports customer / user usage)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.UsageReportsAdapter.schema
    _adapter: ClassVar[Any] = ws.UsageReportsAdapter()

    class Meta:
        """Function metadata."""

        name = "usage_reports"
        description = "Google Workspace daily usage metrics from the Admin SDK Reports API"
        categories = ["google", "workspace", "usage"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_USAGE_EXAMPLES)
        tags = _tags(
            name="usage_reports",
            title="Workspace Daily Usage Metrics",
            category=CAT_REPORTS,
            adapter=ws.UsageReportsAdapter,
            what=(
                "Reads the daily usage reports — tenant totals (`report := 'customer'`) or per user "
                "(`report := 'user'`) — as one long-format row per (entity, date, metric) with the "
                "value in the typed `int_value` / `bool_value` / `string_value` / `datetime_value` "
                "column (structured values in `msg_value`). A day is required: pass `date` or filter "
                "the `date` column; a range expands to one request per day (at most 92 days)."
            ),
            when="license and adoption tracking, storage growth, per-user activity metrics.",
            pushdown=(
                "`date =` / `IN` / range selects the report days; `parameter =` / `IN` (names of the "
                "form app:metric) becomes the `parameters` list; `user_email =` becomes `userKey` "
                "for user reports. Reports lag by a few days and may be partial for recent dates."
            ),
            keywords=("usage reports", "adoption", "license usage", "storage quota", "gmail usage", "metrics"),
            examples=_USAGE_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_UsageArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 1000), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_UsageArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_UsageArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


# ---------------------------------------------------------------------------
# Alert Center / Groups Settings
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _AlertsArgs:
    filter: Annotated[
        str, Arg("filter", default="", doc="Alert Center filter, e.g. 'severity=\"HIGH\"' (AND-ed with pushdown).")
    ]
    order_by: Annotated[str, Arg("order_by", default="", doc="Sort order, e.g. 'create_time desc'.")]
    customer_id: Annotated[str, Arg("customer_id", default="", doc="Customer ID (default: the admin's tenant).")]
    count: Annotated[int, _count_arg(100_000)]
    page_size: Annotated[int, _page_arg(1000)]


_ALERTS_EXAMPLES = [
    {
        "description": "High-severity alerts created since 2026-11-01 (the time bound is pushed into the filter)",
        "sql": (
            "SELECT create_time, type, source, status FROM google.workspace.alerts() "
            "WHERE create_time >= TIMESTAMPTZ '2026-11-01 00:00:00+00' AND severity = 'HIGH' "
            "ORDER BY create_time DESC"
        ),
    },
    {
        "description": "Open alert counts by type",
        "sql": (
            "SELECT type, count(*) AS open_alerts FROM google.workspace.alerts() "
            "WHERE status <> 'CLOSED' AND NOT deleted GROUP BY type ORDER BY open_alerts DESC, type"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceAlertsFunction(TableFunctionGenerator[_AlertsArgs, _WorkspaceState]):
    """Alert Center alerts (``alerts.list``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.AlertsAdapter.schema
    _adapter: ClassVar[Any] = ws.AlertsAdapter()

    class Meta:
        """Function metadata."""

        name = "alerts"
        description = "Google Workspace security and system alerts from the Alert Center API"
        categories = ["google", "workspace", "security"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_ALERTS_EXAMPLES)
        tags = _tags(
            name="alerts",
            title="Workspace Alert Center Alerts",
            category=CAT_SECURITY,
            adapter=ws.AlertsAdapter,
            what=(
                "Lists Alert Center alerts (suspicious logins, phishing, device compromise, "
                "government-backed attacks, ...): id, type, source, create/start/end/update times, "
                "deleted flag, triage status, severity, assignee and the type-specific payload as JSON."
            ),
            when="security triage dashboards and alert-volume trends.",
            pushdown=(
                "bounds on `create_time` / `start_time` and `type` / `source` equality or `IN` are "
                "compiled into the Alert Center `filter` expression, AND-ed with any `filter` argument."
            ),
            keywords=("alert center", "security alerts", "phishing", "suspicious login", "incidents", "workspace"),
            examples=_ALERTS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_AlertsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=min(params.args.count, 100), max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[_AlertsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_AlertsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit one page of rows per tick."""
        _run_tick(cls._adapter, params, state, out)


@dataclass(slots=True, frozen=True)
class _GroupSettingsArgs:
    group_email: Annotated[str, Arg(0, doc="The group's e-mail address.")]
    count: Annotated[int, _count_arg(1)]


_GROUP_SETTINGS_EXAMPLES = [
    {
        "description": "Who can join, view membership and post to one group, and whether outsiders are allowed",
        "sql": (
            "SELECT who_can_join, who_can_view_membership, who_can_post_message, allow_external_members "
            "FROM google.workspace.group_settings('eng@example.com')"
        ),
    },
    {
        "description": "Read a setting that has no typed column from the full settings JSON",
        "sql": (
            "SELECT settings ->> '$.whoCanAddReferences' AS who_can_add_references "
            "FROM google.workspace.group_settings('eng@example.com')"
        ),
    },
]


@init_single_worker
@bind_fixed_schema
class WorkspaceGroupSettingsFunction(TableFunctionGenerator[_GroupSettingsArgs, _WorkspaceState]):
    """Access and moderation settings of one group (Groups Settings ``groups.get``)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ws.GroupSettingsAdapter.schema
    _adapter: ClassVar[Any] = ws.GroupSettingsAdapter()

    class Meta:
        """Function metadata."""

        name = "group_settings"
        description = "Access and moderation settings of a Google group from the Groups Settings API"
        categories = ["google", "workspace", "security"]
        required_secrets = auth.REQUIRED_SECRETS
        filter_pushdown = True
        auto_apply_filters = True
        examples = _examples(_GROUP_SETTINGS_EXAMPLES)
        tags = _tags(
            name="group_settings",
            title="Google Group Access Settings",
            category=CAT_SECURITY,
            adapter=ws.GroupSettingsAdapter,
            what=(
                "Returns one row with a group's access and moderation settings: who can join, view "
                "the group or its membership, post, contact owners, discover, moderate and leave; "
                "moderation levels; and boolean flags such as `allow_external_members` (the API's "
                "'true'/'false' strings become real booleans). The full resource is in `settings`."
            ),
            when="check whether a group is open to outsiders or to self-join before trusting it for access.",
            pushdown="none (a single-group lookup).",
            keywords=("group settings", "groups settings api", "external members", "who can join", "moderation"),
            examples=_GROUP_SETTINGS_EXAMPLES,
        )

    @classmethod
    def cardinality(cls, params: BindParams[_GroupSettingsArgs]) -> TableCardinality:
        """Estimate the result cardinality."""
        return TableCardinality(estimate=1, max=1)

    @classmethod
    def initial_state(cls, params: ProcessParams[_GroupSettingsArgs]) -> _WorkspaceState:
        """Create the scan state, translating pushed filters."""
        return _initial_state(cls, params)

    @classmethod
    def process(cls, params: ProcessParams[_GroupSettingsArgs], state: _WorkspaceState, out: OutputCollector) -> None:
        """Emit the settings row."""
        _run_tick(cls._adapter, params, state, out)


WORKSPACE_FUNCTIONS: list[type] = [
    WorkspaceUsersFunction,
    WorkspaceGroupsFunction,
    WorkspaceGroupMembersFunction,
    WorkspaceOrgUnitsFunction,
    WorkspaceDomainsFunction,
    WorkspaceRolesFunction,
    WorkspaceRoleAssignmentsFunction,
    WorkspaceChromeOsDevicesFunction,
    WorkspaceMobileDevicesFunction,
    WorkspaceActivitiesFunction,
    WorkspaceUsageReportsFunction,
    WorkspaceAlertsFunction,
    WorkspaceGroupSettingsFunction,
]

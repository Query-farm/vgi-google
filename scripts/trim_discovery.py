"""Trim Google's published discovery documents down to the methods the tests use.

The Workspace admin tests replay the REAL discovery documents for the Admin SDK
Directory / Reports APIs, the Alert Center API and the Groups Settings API, so the
client the adapters build validates parameters (required path params, regex
patterns such as the Reports ``startTime`` RFC-3339 pattern, enums, int ranges)
exactly as it does against live Google. Only the method definitions the adapters
call (plus the schemas they transitively reference) are kept, to keep the
fixtures small; every kept definition is byte-for-byte Google's.

Usage (network; fetches the public, credential-free discovery documents)::

    uv run python scripts/trim_discovery.py tests/fixtures/discovery
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

#: (api, version) -> dotted method paths to keep.
KEEP: dict[tuple[str, str], list[str]] = {
    ("admin", "directory_v1"): [
        "users.list",
        "groups.list",
        "members.list",
        "orgunits.list",
        "chromeosdevices.list",
        "mobiledevices.list",
        "domains.list",
        "roles.list",
        "roleAssignments.list",
    ],
    ("admin", "reports_v1"): [
        "activities.list",
        "customerUsageReports.get",
        "userUsageReport.get",
    ],
    ("alertcenter", "v1beta1"): ["alerts.list"],
    ("groupssettings", "v1"): ["groups.get"],
}


def _refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.add(ref)
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def _keep(src: dict[str, Any], parts: list[str], dst: dict[str, Any]) -> None:
    """Copy the method at dotted ``parts`` from ``src`` into ``dst`` (same nesting)."""
    if len(parts) == 1:
        dst.setdefault("methods", {})[parts[0]] = src["methods"][parts[0]]
        return
    child = dst.setdefault("resources", {}).setdefault(parts[0], {})
    _keep(src["resources"][parts[0]], parts[1:], child)


def trim(doc: dict[str, Any], methods: list[str]) -> dict[str, Any]:
    """Return ``doc`` with only ``methods`` (and the schemas they reference)."""
    root: dict[str, Any] = {}
    for dotted in methods:
        _keep(doc, dotted.split("."), root)
    kept = root.get("resources", {})
    needed: set[str] = set()
    _refs(kept, needed)
    schemas = doc.get("schemas", {})
    frontier = list(needed)
    while frontier:
        found: set[str] = set()
        _refs(schemas.get(frontier.pop(), {}), found)
        for ref in found - needed:
            needed.add(ref)
            frontier.append(ref)
    trimmed = {k: v for k, v in doc.items() if k not in ("resources", "schemas")}
    trimmed["resources"] = kept
    trimmed["schemas"] = {k: v for k, v in schemas.items() if k in needed}
    return trimmed


def main(argv: list[str]) -> int:
    """Fetch, trim and write each discovery document into ``argv[0]``."""
    out_dir = Path(argv[0] if argv else "tests/fixtures/discovery")
    out_dir.mkdir(parents=True, exist_ok=True)
    for (api, version), methods in KEEP.items():
        url = f"https://www.googleapis.com/discovery/v1/apis/{api}/{version}/rest"
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
            doc = json.load(resp)
        path = out_dir / f"{api}.{version}.json"
        path.write_text(json.dumps(trim(doc, methods), indent=1, sort_keys=True) + "\n")
        print(f"wrote {path} (revision {doc.get('revision')})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

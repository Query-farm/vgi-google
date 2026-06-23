"""Discovery introspection tests: google_apis / google_methods, offline.

These point ``VGI_GOOGLE_DISCOVERY_DIR`` at a temp directory of static discovery
artifacts (an ``apis.list.json`` directory listing and a per-API discovery doc),
so ``google_apis`` and ``google_methods`` run with no network. They assert the
flattened method tree (dotted method paths, required-parameter extraction) and
the name filter — the surface a user needs to discover what ``google_call`` can
reach.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.harness import invoke_table_function
from tests.mock_google import DRIVE_DOC
from vgi_google.tables import GoogleApisFunction, GoogleMethodsFunction

_APIS_LIST = {
    "items": [
        {"name": "drive", "version": "v3", "title": "Google Drive API", "preferred": True,
         "discoveryRestUrl": "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest"},
        {"name": "sheets", "version": "v4", "title": "Google Sheets API", "preferred": True,
         "discoveryRestUrl": "https://www.googleapis.com/discovery/v1/apis/sheets/v4/rest"},
        {"name": "calendar", "version": "v3", "title": "Calendar API", "preferred": True,
         "discoveryRestUrl": "x"},
    ]
}


@pytest.fixture()
def discovery_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "apis.list.json").write_text(json.dumps(_APIS_LIST))
    (tmp_path / "drive.v3.json").write_text(json.dumps(DRIVE_DOC))
    monkeypatch.setenv("VGI_GOOGLE_DISCOVERY_DIR", str(tmp_path))
    return tmp_path


def test_google_apis_lists_and_filters(discovery_dir: Path) -> None:
    table = invoke_table_function(GoogleApisFunction)
    rows = table.to_pylist()
    assert {r["name"] for r in rows} == {"drive", "sheets", "calendar"}
    assert any(r["name"] == "drive" and r["version"] == "v3" and r["preferred"] for r in rows)

    filtered = invoke_table_function(GoogleApisFunction, named={"name": "sheet"})
    names = {r["name"] for r in filtered.to_pylist()}
    assert names == {"sheets"}


def test_google_methods_flattens_tree(discovery_dir: Path) -> None:
    table = invoke_table_function(GoogleMethodsFunction, positional=("drive", "v3"))
    rows = {r["method"]: r for r in table.to_pylist()}
    assert "files.list" in rows
    assert rows["files.list"]["http_method"] == "GET"
    # Parameters are surfaced as a LIST<VARCHAR>.
    assert "pageToken" in rows["files.list"]["parameters"]

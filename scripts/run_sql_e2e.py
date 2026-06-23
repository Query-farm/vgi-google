"""Run the haybarn SQL E2E suite against the canned Google transport.

Drives the REAL worker subprocess (over stdio) with ``VGI_GOOGLE_MOCK=1`` so its
adapters build clients from static discovery docs and are served deterministic,
paginated responses by ``tests.mock_google`` — nothing here touches a live Google
API. A temp directory of static discovery artifacts is exported via
``VGI_GOOGLE_DISCOVERY_DIR`` so ``google_apis`` / ``google_methods`` also run
offline.

Reads:
    VGI_GOOGLE_WORKER   worker stdio command (required)
    HAYBARN             runner binary (default: haybarn-unittest)
    TEST_DIR            haybarn --test-dir (default: .)
    TEST_PATTERN        haybarn glob (default: test/sql/*)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Ensure the repo root is importable so `tests.mock_google` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.mock_google import DRIVE_DOC  # noqa: E402

_APIS_LIST = {
    "items": [
        {"name": "drive", "version": "v3", "title": "Google Drive API", "preferred": True, "discoveryRestUrl": "x"},
        {"name": "sheets", "version": "v4", "title": "Google Sheets API", "preferred": True, "discoveryRestUrl": "x"},
        {"name": "calendar", "version": "v3", "title": "Calendar API", "preferred": True, "discoveryRestUrl": "x"},
    ]
}


def main() -> int:
    worker = os.environ.get("VGI_GOOGLE_WORKER")
    if not worker:
        print("ERROR: VGI_GOOGLE_WORKER is not set", file=sys.stderr)
        return 2

    haybarn = os.environ.get("HAYBARN", "haybarn-unittest")
    test_dir = os.environ.get("TEST_DIR", ".")
    pattern = os.environ.get("TEST_PATTERN", "test/sql/*")

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "apis.list.json").write_text(json.dumps(_APIS_LIST))
        (Path(tmp) / "drive.v3.json").write_text(json.dumps(DRIVE_DOC))

        env = dict(os.environ)
        env["VGI_GOOGLE_WORKER"] = worker
        env["VGI_GOOGLE_MOCK"] = "1"
        env["VGI_GOOGLE_DISCOVERY_DIR"] = tmp

        print(f"mock Google transport enabled; running {haybarn} {pattern}")
        proc = subprocess.run([haybarn, "--test-dir", test_dir, pattern], env=env)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

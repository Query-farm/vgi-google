#!/usr/bin/env bash
# Copyright 2026 Query Farm LLC - https://query.farm
#
# Run this repo's sqllogictest suite (test/sql/*.test) against the vgi-google
# VGI worker, using a prebuilt standalone `haybarn-unittest` and the signed
# community `vgi` extension — no C++ build from source. See ci/README.md.
#
# The worker is launched with VGI_GOOGLE_MOCK=1 so its adapters build from static
# discovery docs and are served canned, paginated responses (tests/mock_google.py).
# A temp dir of static discovery artifacts is exported via VGI_GOOGLE_DISCOVERY_DIR
# so google_apis / google_methods also run offline — nothing touches a live
# Google API.
#
# Required environment:
#   HAYBARN_UNITTEST   path to the haybarn-unittest binary
#   VGI_GOOGLE_WORKER  worker LOCATION the .test files ATTACH (a stdio command)
# Optional:
#   STAGE              scratch dir for the preprocessed test tree (default: mktemp)
set -euo pipefail

: "${HAYBARN_UNITTEST:?path to the haybarn-unittest binary}"
: "${VGI_GOOGLE_WORKER:?worker LOCATION (stdio command or http:// URL)}"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STAGE="${STAGE:-$(mktemp -d)}"

echo "Staging preprocessed tests into $STAGE ..."
mkdir -p "$STAGE/test/sql"
for f in "$REPO"/test/sql/*.test; do
  awk -f "$HERE/preprocess-require.awk" "$f" > "$STAGE/test/sql/$(basename "$f")"
done

# Build the static discovery dir the offline discovery helpers read (the same
# artifacts scripts/run_sql_e2e.py writes), from the repo root so
# `tests.mock_google` imports.
DISCO="$(mktemp -d)"
cleanup() { rm -rf "$DISCO"; }
trap cleanup EXIT
( cd "$REPO" && DISCO="$DISCO" uv run --no-sync python - <<'PY'
import json, os
from pathlib import Path
from tests.mock_google import DRIVE_DOC

apis_list = {
    "items": [
        {"name": "drive", "version": "v3", "title": "Google Drive API", "preferred": True, "discoveryRestUrl": "x"},
        {"name": "sheets", "version": "v4", "title": "Google Sheets API", "preferred": True, "discoveryRestUrl": "x"},
        {"name": "calendar", "version": "v3", "title": "Calendar API", "preferred": True, "discoveryRestUrl": "x"},
    ]
}
d = Path(os.environ["DISCO"])
(d / "apis.list.json").write_text(json.dumps(apis_list))
(d / "drive.v3.json").write_text(json.dumps(DRIVE_DOC))
PY
)
export VGI_GOOGLE_MOCK=1
export VGI_GOOGLE_DISCOVERY_DIR="$DISCO"

cd "$STAGE"

# Warm the extension cache once: vgi from the signed community channel. A miss
# here is only a warning — the per-test INSTALL/LOAD (injected by
# preprocess-require.awk) is what actually gates each file.
echo "Warming the extension cache (vgi from community) ..."
mkdir -p "$STAGE/test"
cat > "$STAGE/test/_warm.test" <<'EOF'
# name: test/_warm.test
# group: [warm]
statement ok
INSTALL vgi FROM community;
EOF
"$HAYBARN_UNITTEST" "test/_warm.test" >/dev/null 2>&1 || echo "::warning::extension warm step did not fully succeed"
rm -f "$STAGE/test/_warm.test"

# Run the whole suite in one invocation, streaming the runner's native
# sqllogictest report. Any failed assertion exits non-zero and fails the job.
echo "Running suite (worker: $VGI_GOOGLE_WORKER) ..."
"$HAYBARN_UNITTEST" "test/sql/*"

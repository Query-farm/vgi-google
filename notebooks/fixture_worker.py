"""Install explicit synthetic HTTP responses through the real worker's test seam."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httplib2
from vgi_google.client import set_http_factory
from vgi_google.worker import GoogleWorker

DIRECTORY = Path(__file__).with_name("fixtures")
QUERY = "mimeType='application/pdf' and trashed=false"


class SyntheticHttp:
    """Route only the notebook's two read-only API shapes; never open a socket."""

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        if method != "GET":
            raise RuntimeError("Notebook fixture permits GET only")
        if parsed.netloc == "www.googleapis.com" and parsed.path == "/drive/v3/files":
            if query.get("q") != [QUERY] or query.get("pageSize") != ["3"]:
                raise RuntimeError("Fixture supports only the documented PDF query and page size 3")
            token = query.get("pageToken", [""])[0]
            offsets = {"": 0, "page-3": 3, "page-6": 6}
            if token not in offsets:
                raise RuntimeError("Unexpected fixture continuation token")
            rows = json.loads((DIRECTORY / "drive-files.json").read_text())
            offset = offsets[token]
            payload = {"files": rows[offset : offset + 3]}
            if offset + 3 < len(rows):
                payload["nextPageToken"] = f"page-{offset + 3}"
            audit = {"api": "drive", "page_token": token, "returned_rows": len(payload["files"])}
        elif (
            parsed.netloc == "sheets.googleapis.com"
            and unquote(parsed.path) == "/v4/spreadsheets/tracking-demo/values/Log!A1:B50"
        ):
            if query.get("majorDimension") != ["ROWS"] or query.get("valueRenderOption") != [
                "FORMATTED_VALUE"
            ]:
                raise RuntimeError("Unexpected fixture sheet render options")
            payload = json.loads((DIRECTORY / "sheet-values.json").read_text())
            audit = {"api": "sheets", "range": "Log!A1:B50", "returned_rows": len(payload["values"]) - 1}
        else:
            raise RuntimeError(f"No synthetic response for {method} {parsed.netloc}{parsed.path}")
        audit_path = Path(os.environ["NOTEBOOK_GOOGLE_AUDIT"])
        with audit_path.open("a") as stream:
            stream.write(json.dumps(audit) + "\n")
        return httplib2.Response({"status": "200", "content-type": "application/json"}), json.dumps(
            payload
        ).encode()


def http_factory(api, version):
    if (api, version) not in [("drive", "v3"), ("sheets", "v4")]:
        raise RuntimeError("Notebook fixture supports only Drive v3 and Sheets v4")
    return SyntheticHttp(), json.loads((DIRECTORY / f"{api}.{version}.json").read_text())


if __name__ == "__main__":
    set_http_factory(http_factory)
    GoogleWorker.main()

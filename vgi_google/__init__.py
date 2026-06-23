"""vgi-google: query Google APIs from SQL as VGI DuckDB table functions.

The public surface is the ``google`` catalog assembled in ``google_worker.py``;
this package holds its pieces:

* :mod:`vgi_google.discovery` — the discovery-backed Google client factory
  (``google-api-python-client``) plus a generic dotted-method caller.
* :mod:`vgi_google.auth` — service-account / API-key credential resolution via
  the VGI secret provider (never inline).
* :mod:`vgi_google.adapters` — curated READ adapters mapping each Google API's
  response into a clean, typed Arrow schema (Sheets, Drive, Calendar, YouTube).
* :mod:`vgi_google.tables` — the table functions exposed to SQL.
* :mod:`vgi_google.schema_utils` — Arrow-schema helpers (LIST / TIMESTAMPTZ /
  JSON-tagged columns) and row-to-batch builders.

This is an *egress / commodity* connector: the value lives in the upstream
Google APIs (their data, quotas and billing), not in the worker. The durable
cleverness is the discovery-driven design — broad reach across ~every Google
REST API for low maintenance — not a defensibility story. See the README.
"""

from __future__ import annotations

__version__ = "0.1.0"

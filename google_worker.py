# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.46.1",
#     "google-api-python-client>=2.100",
#     "google-auth>=2.20",
# ]
# ///
"""Repo-root PEP-723 shim for the google worker.

The worker itself (the ``google`` catalog, :class:`GoogleWorker`, and
:func:`main`) lives in :mod:`vgi_google.worker` so it is importable from the
built wheel and reachable by the ``vgi-google-worker`` console script and the
Docker image's ``vgi-serve vgi_google.worker:GoogleWorker`` HTTP entrypoint.

This shim keeps ``uv run google_worker.py`` (used by the Makefile,
``ci/run-integration.sh``, and the tests as the stdio worker command) working
unchanged by re-exporting them.

Usage:
    uv run google_worker.py              # serve over stdio (DuckDB subprocess)
    python serve.py --port 8000          # serve over HTTP

    INSTALL vgi FROM community; LOAD vgi;
    ATTACH 'google' (TYPE vgi, LOCATION 'uv run google_worker.py');
"""

from __future__ import annotations

from vgi_google.worker import GoogleWorker, main

__all__ = ["GoogleWorker", "main"]


if __name__ == "__main__":
    main()

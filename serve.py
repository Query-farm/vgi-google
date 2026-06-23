# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]",
#     "google-api-python-client>=2.100",
#     "google-auth>=2.20",
# ]
#
# [tool.uv.sources]
# vgi-python = { path = "../vgi-python" }
# ///
"""HTTP entrypoint for the google worker.

Forces the worker's CLI into HTTP mode (``Worker.main()`` serves stdio by
default) so callers only pass ``--host``/``--port``.
"""

import sys

from google_worker import GoogleWorker

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    GoogleWorker.main()

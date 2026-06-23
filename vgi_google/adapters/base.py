"""The curated-adapter protocol shared by Sheets / Drive / Calendar / YouTube.

An adapter is a small value object that knows, for one Google API, how to:

* name the discovery ``api`` / ``version`` to build a client for;
* declare the OAuth ``scopes`` it needs (least-privilege; documented per
  adapter);
* declare its clean, typed output :class:`pa.Schema`;
* fetch ONE page of results given the bound client and an opaque cursor, and map
  that page into a list of plain ``dict`` rows plus the ``next_cursor`` for the
  following page.

The cursor is the **externalized scan state**: Google's
``pageToken``/``nextPageToken`` carried as a plain string across ``process()``
ticks (and, under HTTP transport, across requests). One page per tick, ``count``
caps the total — see :mod:`vgi_google.tables`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import pyarrow as pa


@dataclass(slots=True)
class Page:
    """One page of adapter rows plus the cursor for the next page.

    ``next_cursor`` is None when the API reports no further page (no
    ``nextPageToken``), which tells the table function to stop.
    """

    rows: list[dict[str, Any]]
    next_cursor: str | None


class Adapter(Protocol):
    """A curated READ adapter over one Google API."""

    #: Discovery API name and version to build the client for.
    api: str
    version: str
    #: OAuth scopes the adapter needs (documented; least-privilege).
    scopes: list[str]
    #: The adapter's clean, typed output schema.
    schema: pa.Schema

    def fetch_page(self, service: Any, args: Any, cursor: str | None) -> Page:
        """Fetch and map one page of results.

        Args:
            service: A built discovery client (from
                :func:`vgi_google.discovery.build_service`).
            args: The parsed table-function arguments dataclass for this adapter.
            cursor: The opaque ``pageToken`` from a previous page, or None to
                start.
        """
        ...

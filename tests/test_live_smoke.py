"""OPTIONAL, gated live smoke tests — NOT part of the CI gate.

These hit real Google APIs and so require credentials and network; they are
skipped unless ``VGI_GOOGLE_LIVE=1`` is set. Provide either a service-account
key (``VGI_GOOGLE_SERVICE_ACCOUNT_FILE``) or an API key (``VGI_GOOGLE_API_KEY``).

They are intentionally minimal — a single low-quota call per surface — and exist
only to confirm the discovery build + auth wiring works against the real service.
Quota/billing for these calls is on whoever runs them.
"""

from __future__ import annotations

import os

import pytest

LIVE = os.environ.get("VGI_GOOGLE_LIVE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set VGI_GOOGLE_LIVE=1 to run live smoke tests")


def test_live_google_apis_lists_real_directory() -> None:
    """google_apis() against the live Discovery directory returns many APIs."""
    from tests.harness import invoke_table_function
    from vgi_google.tables import GoogleApisFunction

    table = invoke_table_function(GoogleApisFunction)
    assert table.num_rows > 50  # the live directory lists hundreds of APIs


def test_live_youtube_search_with_api_key() -> None:
    """google_youtube needs an API key; skip if none configured."""
    if not os.environ.get("VGI_GOOGLE_API_KEY"):
        pytest.skip("VGI_GOOGLE_API_KEY not set")
    from tests.harness import invoke_table_function
    from vgi_google.tables import GoogleYouTubeFunction

    table = invoke_table_function(GoogleYouTubeFunction, positional=("duckdb",), named={"count": 3})
    assert table.num_rows >= 1
    assert table.to_pylist()[0]["video_id"]

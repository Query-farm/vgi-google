"""Curated READ adapters registry (priority order: Sheets, Drive, Calendar, YouTube).

Each adapter wraps one Google API and is exposed as a dedicated table function in
:mod:`vgi_google.tables`. They share the discovery-backed client factory and the
secret-provider auth, so adding one is: write the adapter module and register it
here. The generic ``google_call`` hatch covers everything without an adapter.
"""

from __future__ import annotations

from .base import Adapter, Page
from .calendar import CalendarAdapter
from .drive import DriveAdapter
from .sheets import SheetsAdapter
from .youtube import YouTubeAdapter

__all__ = [
    "Adapter",
    "CalendarAdapter",
    "DriveAdapter",
    "Page",
    "SheetsAdapter",
    "YouTubeAdapter",
]

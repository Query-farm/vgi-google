"""Configure a credential-free synthetic transport and present SQL results."""

from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib.pyplot as plt
import seaborn as sns
from IPython.display import Markdown, display


def configure(age):
    if type(age) is not int or not 0 <= age <= 365:
        raise ValueError("MIN_AGE_DAYS must be an integer from 0 through 365")
    key = "NOTEBOOK_GOOGLE_AUDIT"
    previous = os.environ.get(key)
    audit = Path("artifacts/request-audit.jsonl").resolve()
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("")
    os.environ[key] = str(audit)
    return previous, audit


def verify_audit(audit):
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    expected = [
        {"api": "drive", "page_token": "", "returned_rows": 3},
        {"api": "drive", "page_token": "page-3", "returned_rows": 3},
        {"api": "drive", "page_token": "page-6", "returned_rows": 3},
        {"api": "sheets", "range": "Log!A1:B50", "returned_rows": 6},
    ]
    if rows != expected:
        raise AssertionError(f"Expected three Drive pages and one Sheet fetch, got {rows!r}")
    return (
        "Transport audit passed: three successive Drive page tokens and "
        "one Sheets fetch, with no repeated calls."
    )


def cleanup(previous):
    if previous is None:
        os.environ.pop("NOTEBOOK_GOOGLE_AUDIT", None)
    else:
        os.environ["NOTEBOOK_GOOGLE_AUDIT"] = previous


def prose(text: str) -> None:
    display(Markdown(text))


def theme() -> None:
    sns.set_theme(
        style="whitegrid",
        rc={
            "figure.dpi": 145,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": "#E8EDE9",
            "text.color": "#243B2F",
            "axes.labelcolor": "#405047",
            "text.parse_math": False,
            "legend.frameon": False,
        },
    )


def chart(fig, title: str, subtitle: str, source: str) -> None:
    fig.text(0.08, 0.98, title, ha="left", va="top", fontsize=17, fontweight="semibold")
    fig.text(0.08, 0.90, subtitle, ha="left", va="top", fontsize=10, color="#58685F")
    fig.tight_layout(rect=(0.02, 0.11, 1, 0.82))
    fig.text(0.08, 0.03, source, fontsize=8, color="#6C7D73")
    plt.show()

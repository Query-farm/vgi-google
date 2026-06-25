"""Shared helpers for per-object ``vgi-lint`` discovery/description metadata.

The ``vgi-lint`` strict profile expects these on **every** function and table.
Each table function surfaces them in its ``Meta.tags``:

- ``vgi.title`` (VGI124) — a human-friendly display name. It MUST differ from the
  machine name once normalized (lowercase + strip non-alphanumerics), so every
  title here carries extra descriptive words (VGI125).
- ``vgi.doc_llm`` (VGI112) — a Markdown narrative aimed at an LLM/agent: what the
  object does, when to use it, its inputs/outputs and key behaviors/edge cases.
- ``vgi.doc_md`` (VGI113) — a Markdown narrative for human docs (overview, usage,
  notes). Distinct content from ``vgi.doc_llm`` (identical values are flagged).
- ``vgi.keywords`` (VGI126 / VGI138) — search terms / synonyms, serialized as a
  JSON array of strings (NOT a comma-separated string).

``vgi.source_url`` is set ONLY on the catalog object (VGI139): per-object
source_url tags are redundant and flagged, so they are deliberately omitted here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

# Base GitHub blob URL for source files in this repo (pinned to ``main``).
SOURCE_BASE = "https://github.com/Query-farm/vgi-google/blob/main"


def keywords_json(keywords: Sequence[str]) -> str:
    """Serialize search keywords as a ``vgi.keywords`` JSON array of strings.

    VGI138 requires ``vgi.keywords`` to be a JSON array (``["a","b"]``), not a
    comma-separated string.
    """
    return json.dumps(list(keywords), ensure_ascii=False)


def object_tags(
    *,
    title: str,
    doc_llm: str,
    doc_md: str,
    keywords: Sequence[str],
    relative_path: str,
    result_columns_md: str | None = None,
    executable_examples: str | None = None,
) -> dict[str, str]:
    """Build the standard per-object discovery/description tag dict.

    Args:
        title: Human-friendly display name (must differ from the machine name).
        doc_llm: Markdown narrative for an LLM/agent audience.
        doc_md: Markdown narrative for human docs (distinct from ``doc_llm``).
        keywords: Search terms/synonyms, serialized to a JSON array of strings
            for ``vgi.keywords`` (VGI138).
        relative_path: Implementing file, relative to the repo root. Retained for
            readability/traceability; not emitted as a per-object source_url
            (VGI139 keeps source_url on the catalog only).
        result_columns_md: Optional Markdown table of returned columns
            (``vgi.result_columns_md``), for table functions.
        executable_examples: Optional JSON string of guaranteed-runnable examples
            (``vgi.executable_examples``).

    Returns:
        A tag dict ready to drop into a function's ``Meta.tags``.
    """
    del relative_path  # implementation file is recorded only on the catalog object
    tags: dict[str, str] = {
        "vgi.title": title,
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
        "vgi.keywords": keywords_json(keywords),
    }
    if result_columns_md is not None:
        tags["vgi.result_columns_md"] = result_columns_md
    if executable_examples is not None:
        tags["vgi.executable_examples"] = executable_examples
    return tags

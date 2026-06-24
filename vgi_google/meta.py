"""Shared helpers for per-object ``vgi-lint`` discovery/description metadata.

The ``vgi-lint`` strict profile (0.26.0) expects these on **every** function and
table. Each table function surfaces them in its ``Meta.tags``:

- ``vgi.title`` (VGI124) — a human-friendly display name. It MUST differ from the
  machine name once normalized (lowercase + strip non-alphanumerics), so every
  title here carries extra descriptive words (VGI125).
- ``vgi.doc_llm`` (VGI112) — a Markdown narrative aimed at an LLM/agent: what the
  object does, when to use it, its inputs/outputs and key behaviors/edge cases.
- ``vgi.doc_md`` (VGI113) — a Markdown narrative for human docs (overview, usage,
  notes). Distinct content from ``vgi.doc_llm`` (identical values are flagged).
- ``vgi.keywords`` (VGI126) — comma-separated search terms / synonyms.
- ``vgi.source_url`` (VGI128) — a link to the implementing source file.

``source_url(path)`` builds the canonical GitHub blob URL so every object points
at exactly where it is implemented.
"""

from __future__ import annotations

# Base GitHub blob URL for source files in this repo (pinned to ``main``).
SOURCE_BASE = "https://github.com/Query-farm/vgi-google/blob/main"


def source_url(relative_path: str) -> str:
    """Build the implementation ``vgi.source_url`` for a repo-relative path.

    Example: ``source_url("vgi_google/tables.py")``.
    """
    return f"{SOURCE_BASE}/{relative_path}"


def object_tags(
    *,
    title: str,
    doc_llm: str,
    doc_md: str,
    keywords: str,
    relative_path: str,
    result_columns_md: str | None = None,
    executable_examples: str | None = None,
) -> dict[str, str]:
    """Build the standard per-object discovery/description tag dict.

    Args:
        title: Human-friendly display name (must differ from the machine name).
        doc_llm: Markdown narrative for an LLM/agent audience.
        doc_md: Markdown narrative for human docs (distinct from ``doc_llm``).
        keywords: Comma-separated search terms/synonyms.
        relative_path: Implementing file, relative to the repo root.
        result_columns_md: Optional Markdown table of returned columns
            (``vgi.result_columns_md``), for table functions.
        executable_examples: Optional JSON string of guaranteed-runnable examples
            (``vgi.executable_examples``).

    Returns:
        A tag dict ready to drop into a function's ``Meta.tags``.
    """
    tags: dict[str, str] = {
        "vgi.title": title,
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
        "vgi.keywords": keywords,
        "vgi.source_url": source_url(relative_path),
    }
    if result_columns_md is not None:
        tags["vgi.result_columns_md"] = result_columns_md
    if executable_examples is not None:
        tags["vgi.executable_examples"] = executable_examples
    return tags

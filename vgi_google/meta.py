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
from collections.abc import Mapping, Sequence

# Base GitHub blob URL for source files in this repo (pinned to ``main``).
SOURCE_BASE = "https://github.com/Query-farm/vgi-google/blob/main"


def keywords_json(keywords: Sequence[str]) -> str:
    """Serialize search keywords as a ``vgi.keywords`` JSON array of strings.

    VGI138 requires ``vgi.keywords`` to be a JSON array (``["a","b"]``), not a
    comma-separated string.
    """
    return json.dumps(list(keywords), ensure_ascii=False)


def example_queries_json(examples: Sequence[Mapping[str, str]]) -> str:
    """Serialize described example queries as a ``vgi.example_queries`` JSON array.

    VGI515 requires each schema- and function-level example query to carry a
    non-empty description. The native ``duckdb_functions().examples`` carrier
    drops the description, so we additionally emit the examples as an explicit
    ``vgi.example_queries`` tag: a JSON array of ``{description, sql}`` objects.
    """
    return json.dumps(
        [{"description": e["description"], "sql": e["sql"]} for e in examples],
        ensure_ascii=False,
    )


def result_columns_json(columns: Sequence[Mapping[str, str]]) -> str:
    """Serialize a static result schema as a ``vgi.result_columns_schema`` array.

    VGI307/VGI321-323 require a table function with no backing table to declare
    its result schema as a JSON array of ``{name, type, description}`` objects,
    where ``type`` is a real DuckDB type and ``description`` is non-blank. This
    replaces the retired free-form ``vgi.result_columns_md`` (VGI414).
    """
    return json.dumps(
        [{"name": c["name"], "type": c["type"], "description": c["description"]} for c in columns],
        ensure_ascii=False,
    )


def object_tags(
    *,
    title: str,
    doc_llm: str,
    doc_md: str,
    keywords: Sequence[str],
    relative_path: str,
    category: str,
    result_columns: Sequence[Mapping[str, str]] | None = None,
    executable_examples: str | None = None,
    example_queries: Sequence[Mapping[str, str]] | None = None,
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
        category: The ``vgi.category`` this object belongs to (VGI413). It MUST
            name one of the categories declared in the schema's
            ``vgi.categories`` registry.
        result_columns: Optional structured result schema — a sequence of
            ``{name, type, description}`` mappings emitted as the structured
            ``vgi.result_columns_schema`` tag (VGI307), for table functions.
        executable_examples: Optional JSON string of guaranteed-runnable examples
            (``vgi.executable_examples``).
        example_queries: Optional described example queries — a sequence of
            ``{description, sql}`` mappings emitted as the ``vgi.example_queries``
            tag so every example carries a description (VGI515).

    Returns:
        A tag dict ready to drop into a function's ``Meta.tags``.
    """
    del relative_path  # implementation file is recorded only on the catalog object
    tags: dict[str, str] = {
        "vgi.title": title,
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
        "vgi.keywords": keywords_json(keywords),
        "vgi.category": category,
    }
    if result_columns is not None:
        tags["vgi.result_columns_schema"] = result_columns_json(result_columns)
    if executable_examples is not None:
        tags["vgi.executable_examples"] = executable_examples
    if example_queries is not None:
        tags["vgi.example_queries"] = example_queries_json(example_queries)
    return tags

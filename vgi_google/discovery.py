"""Discovery-backed Google client factory + generic dotted-method caller.

Google publishes first-party, high-quality **discovery documents** for ~every
REST API, and ``google-api-python-client`` builds a working client for ANY of
them at runtime from those docs. That is what makes the generic path here sound
(unlike OpenAPI heuristics over a random REST API): the curated adapters and the
``google_call`` escape hatch both go through one factory.

Two build paths share a single seam so tests need no network and no credentials:

* **Live** — ``build(api, version, ...)`` fetches the discovery doc from Google's
  Discovery Service and wires real credentials / an API key.
* **Deterministic / test** — if a static discovery doc is available (a
  ``VGI_GOOGLE_DISCOVERY_DIR`` directory of ``<api>.<version>.json`` files, or an
  injected ``discovery_doc``) we ``build_from_document`` instead, and an injected
  ``http`` (an ``HttpMock`` / ``HttpMockSequence``) serves canned method
  responses. The exact same adapter code runs against both.

Network discipline (mirrors the other VGI connectors): a per-call timeout, and
bounded retry with exponential backoff on 429 / 5xx and Google "rate limit
exceeded" / "quota exceeded" errors. Everything else fails fast as a clean
:class:`GoogleApiError`, which the table functions surface as a DuckDB error —
the worker never crashes.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

# How many times to retry a 429/5xx before giving up (so up to N+1 attempts).
DEFAULT_RETRIES = 3
# Base backoff (seconds); doubles each retry.
_BACKOFF_BASE = 0.5
# Per-request timeout (seconds), passed to the underlying http transport.
DEFAULT_TIMEOUT = 30.0

# Directory of static discovery docs, named "<api>.<version>.json". When set,
# the factory builds from these instead of fetching live — the test seam.
ENV_DISCOVERY_DIR = "VGI_GOOGLE_DISCOVERY_DIR"
# Disable the on-disk discovery cache (avoids a noisy warning / file writes).
_STATIC_DISCOVERY = None  # use default behavior


class GoogleApiError(RuntimeError):
    """A Google API call failed (build, transport, HTTP, or quota error).

    Carries a human-readable message safe to surface as a DuckDB error. Quota /
    rate-limit exhaustion lands here too (after retries), never as a crash.
    """


def _discovery_doc_path(api: str, version: str) -> Path | None:
    """Locate a static discovery doc for ``api``/``version`` under the env dir."""
    directory = os.environ.get(ENV_DISCOVERY_DIR)
    if not directory:
        return None
    candidate = Path(directory) / f"{api}.{version}.json"
    return candidate if candidate.exists() else None


def build_service(
    api: str,
    version: str,
    *,
    credentials: Any | None = None,
    api_key: str | None = None,
    http: Any | None = None,
    discovery_doc: dict[str, Any] | None = None,
) -> Any:
    """Build a discovery-driven client for ``api``/``version``.

    Args:
        api: API name (e.g. ``"sheets"``, ``"drive"``, ``"calendar"``).
        version: API version (e.g. ``"v4"``, ``"v3"``).
        credentials: A ``google.auth`` Credentials object (service account), or
            None.
        api_key: A developer API key string, or None. Used when ``credentials``
            is absent (public APIs).
        http: An injectable transport (``HttpMock``/``HttpMockSequence`` in
            tests). When provided, build-from-document is used and no live
            discovery fetch or auth happens.
        discovery_doc: An in-memory discovery document; when provided it is used
            instead of fetching live.

    Returns:
        A built service resource.

    Raises:
        GoogleApiError: discovery could not be fetched/parsed, or the build
            failed.
    """
    from googleapiclient.discovery import build, build_from_document

    # Resolve a static discovery doc from the env dir if one was not passed in.
    if discovery_doc is None:
        path = _discovery_doc_path(api, version)
        if path is not None:
            try:
                discovery_doc = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise GoogleApiError(f"could not read discovery doc {path}: {exc}") from exc

    try:
        if discovery_doc is not None or http is not None:
            # Deterministic path: build from a document, serve via injected http.
            if discovery_doc is None:
                raise GoogleApiError(
                    f"an http transport was injected for {api}:{version} but no discovery "
                    f"document was provided (set {ENV_DISCOVERY_DIR} or pass discovery_doc)"
                )
            return build_from_document(
                discovery_doc,
                http=http,
                developerKey=api_key,
                credentials=credentials if http is None else None,
            )
        # Live path: fetch discovery from Google, wire real auth.
        return build(
            api,
            version,
            credentials=credentials,
            developerKey=api_key,
            cache_discovery=False,
            num_retries=DEFAULT_RETRIES,
        )
    except HttpError as exc:
        raise GoogleApiError(_format_http_error(api, version, exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any build failure becomes a clean error
        raise GoogleApiError(f"failed to build client for {api}:{version}: {exc}") from exc


def resolve_method(service: Any, method: str) -> Any:
    """Walk a dotted method path (e.g. ``spreadsheets.values.get``) to a bound call.

    Returns a callable that, given keyword params, yields an executable request.
    Raises :class:`GoogleApiError` if any segment is not a resource/method on the
    service — a clean DuckDB error rather than an ``AttributeError`` crash.
    """
    parts = [p for p in method.split(".") if p]
    if not parts:
        raise GoogleApiError("method path is empty")
    obj = service
    for segment in parts[:-1]:
        nxt = getattr(obj, segment, None)
        if nxt is None or not callable(nxt):
            raise GoogleApiError(f"unknown resource segment {segment!r} in method {method!r}")
        obj = nxt()
    final = getattr(obj, parts[-1], None)
    if final is None or not callable(final):
        raise GoogleApiError(f"unknown method {parts[-1]!r} in method path {method!r}")
    return final


def execute(request: Any, *, retries: int = DEFAULT_RETRIES, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Execute a built request with a timeout and bounded retry on 429/5xx/quota.

    Retries transient rate-limit and server errors with exponential backoff;
    non-retryable client errors (400/401/403-non-quota/404) and exhausted retries
    raise :class:`GoogleApiError`.
    """
    # Apply a per-request timeout to the request's http transport when possible.
    _set_request_timeout(request, timeout)

    last_error: str = "unknown error"
    for attempt in range(retries + 1):
        try:
            return _as_json(request.execute(num_retries=0))
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            retryable = status in (429, 500, 502, 503, 504) or _is_quota_error(exc)
            last_error = _format_http_error_from_exc(exc)
            if retryable and attempt < retries:
                _sleep(_BACKOFF_BASE * (2**attempt))
                continue
            raise GoogleApiError(last_error) from exc
        except (httplib2.HttpLib2Error, OSError, TimeoutError) as exc:
            last_error = f"request failed: {exc}"
            if attempt < retries:
                _sleep(_BACKOFF_BASE * (2**attempt))
                continue
            raise GoogleApiError(last_error) from exc

    raise GoogleApiError(last_error)


def _as_json(result: Any) -> Any:
    """Normalize an execute() result to a parsed Python object.

    The discovery client deserializes JSON when a method declares a ``response``
    schema, but returns the raw body otherwise; we json-decode any bytes/str so
    adapters always get a dict/list regardless of the discovery doc's detail.
    """
    if isinstance(result, (bytes, bytearray)):
        return json.loads(result.decode("utf-8"))
    if isinstance(result, str):
        return json.loads(result)
    return result


def _set_request_timeout(request: Any, timeout: float) -> None:
    """Best-effort: set the read timeout on the request's http transport."""
    http = getattr(request, "http", None)
    if http is not None:
        with contextlib.suppress(AttributeError, TypeError):
            http.timeout = timeout


def _is_quota_error(exc: HttpError) -> bool:
    """True when an HttpError is a rate-limit / quota-exceeded error.

    Google returns these as 403 with reason ``rateLimitExceeded`` /
    ``userRateLimitExceeded`` / ``quotaExceeded`` (in addition to plain 429), so
    we look past the status code at the structured reason.
    """
    reason = ""
    try:
        payload = json.loads(exc.content.decode("utf-8") if isinstance(exc.content, bytes) else exc.content)
        errors = payload.get("error", {}).get("errors", [])
        reason = " ".join(str(e.get("reason", "")) for e in errors).lower()
    except (ValueError, AttributeError, TypeError):
        reason = ""
    return any(token in reason for token in ("ratelimitexceeded", "quotaexceeded"))


def _format_http_error(api: str, version: str, exc: HttpError) -> str:
    return f"{api}:{version} discovery/build failed: {_format_http_error_from_exc(exc)}"


def _format_http_error_from_exc(exc: HttpError) -> str:
    """A compact, credential-free message from an HttpError."""
    status = getattr(getattr(exc, "resp", None), "status", "?")
    message = ""
    try:
        payload = json.loads(exc.content.decode("utf-8") if isinstance(exc.content, bytes) else exc.content)
        message = str(payload.get("error", {}).get("message", "")).strip()
    except (ValueError, AttributeError, TypeError):
        message = ""
    suffix = f": {message}" if message else ""
    return f"Google API returned HTTP {status}{suffix}"


def _sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch the backoff sleep to a no-op."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Discovery introspection (google_apis / google_methods)
# ---------------------------------------------------------------------------

# The Discovery Service "list APIs" endpoint. A static JSON file at
# ``$VGI_GOOGLE_DISCOVERY_DIR/apis.list.json`` overrides it for deterministic
# tests (and offline use).
_DISCOVERY_LIST_URL = "https://www.googleapis.com/discovery/v1/apis"


def list_apis(name_filter: str | None = None) -> list[dict[str, Any]]:
    """Return the list of reachable Google APIs (name/version/title/preferred).

    Reads a static ``apis.list.json`` from ``VGI_GOOGLE_DISCOVERY_DIR`` when
    present (tests / offline); otherwise fetches the live Discovery directory.
    """
    directory = os.environ.get(ENV_DISCOVERY_DIR)
    payload: dict[str, Any]
    if directory and (Path(directory) / "apis.list.json").exists():
        payload = json.loads((Path(directory) / "apis.list.json").read_text())
    else:
        payload = _http_get_json(_DISCOVERY_LIST_URL)

    rows: list[dict[str, Any]] = []
    needle = (name_filter or "").lower()
    for item in payload.get("items", []):
        name = item.get("name", "")
        if needle and needle not in name.lower():
            continue
        rows.append(
            {
                "name": name,
                "version": item.get("version", ""),
                "title": item.get("title"),
                "preferred": item.get("preferred"),
                "discovery_url": item.get("discoveryRestUrl"),
            }
        )
    return rows


def fetch_discovery_doc(api: str, version: str) -> dict[str, Any]:
    """Return the discovery document for ``api``/``version``.

    Prefers a static doc under ``VGI_GOOGLE_DISCOVERY_DIR`` (tests / offline);
    otherwise fetches the live discovery document from Google.
    """
    path = _discovery_doc_path(api, version)
    if path is not None:
        try:
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        except (OSError, ValueError) as exc:
            raise GoogleApiError(f"could not read discovery doc {path}: {exc}") from exc
    url = f"https://www.googleapis.com/discovery/v1/apis/{api}/{version}/rest"
    return _http_get_json(url)


def list_methods(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a discovery document's resource tree into one row per method."""
    rows: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], prefix: str) -> None:
        for mname, method in (node.get("methods") or {}).items():
            full = f"{prefix}{mname}" if prefix else mname
            params = method.get("parameters") or {}
            required = sorted(p for p, spec in params.items() if spec.get("required"))
            rows.append(
                {
                    "method": full,
                    "http_method": method.get("httpMethod"),
                    "path": method.get("path") or method.get("flatPath"),
                    "description": method.get("description"),
                    "parameters": sorted(params.keys()) or None,
                    "required_parameters": required or None,
                }
            )
        for rname, resource in (node.get("resources") or {}).items():
            walk(resource, f"{prefix}{rname}.")

    walk(doc, "")
    return sorted(rows, key=lambda r: r["method"])


def _http_get_json(url: str) -> dict[str, Any]:
    """GET a JSON document with a timeout and bounded retry on 429/5xx."""
    last_error = "unknown error"
    for attempt in range(DEFAULT_RETRIES + 1):
        try:
            http = httplib2.Http(timeout=DEFAULT_TIMEOUT)
            resp, content = http.request(url, "GET")
        except (httplib2.HttpLib2Error, OSError) as exc:
            last_error = f"request to {url} failed: {exc}"
            if attempt < DEFAULT_RETRIES:
                _sleep(_BACKOFF_BASE * (2**attempt))
                continue
            raise GoogleApiError(last_error) from exc
        status = int(resp.status)
        if status == 429 or status >= 500:
            last_error = f"{url} returned HTTP {status}"
            if attempt < DEFAULT_RETRIES:
                _sleep(_BACKOFF_BASE * (2**attempt))
                continue
            raise GoogleApiError(last_error)
        if status >= 400:
            raise GoogleApiError(f"{url} returned HTTP {status}")
        try:
            return json.loads(content)  # type: ignore[no-any-return]
        except ValueError as exc:
            raise GoogleApiError(f"{url} returned non-JSON content: {exc}") from exc
    raise GoogleApiError(last_error)

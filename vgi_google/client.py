"""Per-call client assembly: resolve auth, then build a discovery client.

This is the thin bridge between the table functions and the two lower layers
(:mod:`vgi_google.auth` and :mod:`vgi_google.discovery`). It exists so the
adapters and the generic hatch share one path to a built, authenticated client,
and so tests have a single seam to inject a canned HTTP transport.

Test seam: :func:`set_http_factory` installs a callable ``(api, version) ->
http`` that returns an ``HttpMock`` / ``HttpMockSequence``. When set, clients are
built from a static discovery doc and served by that transport — no live
discovery fetch, no credentials. Production leaves it unset.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import auth, discovery

# Test seam: when set, called as factory(api, version) -> (http, discovery_doc).
# The discovery doc is needed because build_from_document does not fetch one.
_HTTP_FACTORY: Callable[[str, str], tuple[Any, dict[str, Any]]] | None = None


def set_http_factory(factory: Callable[[str, str], tuple[Any, dict[str, Any]]] | None) -> None:
    """Install (or clear) the injectable-transport factory used by tests.

    The factory takes ``(api, version)`` and returns a tuple of an injectable
    ``httplib2``-style transport and the static discovery document for that API.
    """
    global _HTTP_FACTORY
    _HTTP_FACTORY = factory


def build_client(
    api: str,
    version: str,
    *,
    secrets: Any,
    scopes: list[str] | None = None,
    require_auth: bool = True,
) -> Any:
    """Resolve credentials from ``secrets`` and build a client for ``api``/``version``.

    Args:
        api: Discovery API name.
        version: API version.
        secrets: A VGI SecretsAccessor (``params.secrets``).
        scopes: OAuth scopes for a service-account credential (least-privilege).
        require_auth: When True and no credential resolves (and no test transport
            is injected), raise :class:`vgi_google.auth.AuthError`. Public APIs
            reached via an API key satisfy this; an unauthenticated call does
            not.

    Returns:
        A built discovery client.
    """
    if _HTTP_FACTORY is not None:
        # Test path: canned transport + static discovery doc, no real auth.
        http, doc = _HTTP_FACTORY(api, version)
        return discovery.build_service(api, version, http=http, discovery_doc=doc)

    resolved = auth.resolve(secrets, scopes=scopes)
    if require_auth and not resolved.has_any:
        raise auth.AuthError(
            f"no Google credential configured for {api}:{version}; create a "
            f"'{auth.SECRET_SERVICE_ACCOUNT}' secret (service-account JSON key) or, for a "
            f"public API, a '{auth.SECRET_API_KEY}' secret"
        )
    return discovery.build_service(
        api,
        version,
        credentials=resolved.credentials,
        api_key=resolved.api_key,
    )

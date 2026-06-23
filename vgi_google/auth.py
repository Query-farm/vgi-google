"""Google credential resolution — service-account-first, secret-provider only.

The hard part of a Google connector is auth, and it is *shared* across every
adapter and the generic ``google_call`` hatch; this module is the single place
that turns a VGI secret into something ``google-api-python-client`` can use.

Three credential modes, in priority order (service-account is the default fit
for a server-side connector — no interactive consent, accesses exactly what the
account is granted):

1. **Service account (JSON key)** — DEFAULT. A ``google_service_account`` secret
   whose value carries the JSON key (key ``key_json`` / ``credentials_json``, or
   the raw service-account object). Optional ``subject`` enables domain-wide
   delegation; ``scopes`` (space/comma list) narrows the grant.
2. **API key** — for public-data APIs (YouTube public, public Sheets, Maps). A
   ``google_api_key`` secret with key ``api_key`` (or ``key``).
3. **OAuth2 user (3-legged, refresh token)** — DOCUMENTED ADVANCED PATH ONLY,
   not wired into v1 adapters. See the README; building it is a roadmap item.

Secrets are read via the VGI ``SecretsAccessor`` (``params.secrets``) — NEVER
inline. For tests and local smoke runs, two environment escape hatches exist so
the suite can run with no real secret manager:

* ``VGI_GOOGLE_SERVICE_ACCOUNT_FILE`` — path to a service-account JSON key.
* ``VGI_GOOGLE_API_KEY`` — a raw API key.

These env hatches are a developer convenience; production deployments should use
the secret provider exclusively.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.auth.credentials import Credentials

#: VGI secret types this worker understands.
SECRET_SERVICE_ACCOUNT = "google_service_account"
SECRET_API_KEY = "google_api_key"
SECRET_OAUTH_USER = "google_oauth_user"  # documented; not consumed by v1 adapters

# Environment escape hatches (tests / local smoke only).
ENV_SERVICE_ACCOUNT_FILE = "VGI_GOOGLE_SERVICE_ACCOUNT_FILE"
ENV_API_KEY = "VGI_GOOGLE_API_KEY"


class AuthError(RuntimeError):
    """No usable credential could be resolved for a call.

    Carries a human-readable, secret-free message safe to surface as a clean
    DuckDB error (never the credential contents).
    """


@dataclass(slots=True)
class GoogleAuth:
    """Resolved auth for one call: at most one of ``credentials`` / ``api_key``.

    ``credentials`` is a ready ``google.auth`` Credentials object (service
    account). ``api_key`` is a developer key string. When both are None the
    caller had no usable secret — adapters that require auth raise
    :class:`AuthError`; public APIs may proceed unauthenticated only if the API
    itself allows it.
    """

    credentials: Credentials | None = None
    api_key: str | None = None

    @property
    def has_any(self) -> bool:
        return self.credentials is not None or self.api_key is not None


def _as_py(value: Any) -> Any:
    """Unwrap a ``pa.Scalar`` (as delivered by SecretsAccessor) to a Python value."""
    return value.as_py() if hasattr(value, "as_py") else value


def _scopes_from(value: Any) -> list[str] | None:
    """Parse a space/comma-separated scopes string (or list) into a scope list."""
    raw = _as_py(value)
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [str(s) for s in raw if s]
    parts = [p.strip() for p in str(raw).replace(",", " ").split()]
    return [p for p in parts if p] or None


def _service_account_credentials(
    info_or_path: dict[str, Any] | str,
    *,
    scopes: list[str] | None,
    subject: str | None,
) -> Credentials:
    """Build service-account credentials from a JSON key dict or a file path."""
    from google.oauth2 import service_account

    if isinstance(info_or_path, str):
        creds = service_account.Credentials.from_service_account_file(info_or_path, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_info(info_or_path, scopes=scopes)
    if subject:
        creds = creds.with_subject(subject)
    return creds


def _extract_service_account_info(secret: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the service-account JSON object out of a resolved secret dict.

    Accepts a few shapes operators reach for: a ``key_json`` /
    ``credentials_json`` string holding the JSON, or the secret itself already
    being the service-account object (recognized by ``"type":"service_account"``
    and a ``private_key``).
    """
    for key in ("key_json", "credentials_json", "json", "service_account_json"):
        if key in secret and secret[key] is not None:
            raw = _as_py(secret[key])
            if isinstance(raw, str):
                return json.loads(raw)
            if isinstance(raw, dict):
                return raw
    flat = {k: _as_py(v) for k, v in secret.items()}
    if flat.get("type") == "service_account" and "private_key" in flat:
        return flat
    return None


def resolve(secrets: Any, *, scopes: list[str] | None = None) -> GoogleAuth:
    """Resolve the best available credential for a call, service-account first.

    Args:
        secrets: A VGI ``SecretsAccessor`` (``params.secrets``). May be a plain
            object exposing ``.get(secret_type, ...)`` (mirrored in tests).
        scopes: OAuth scopes to attach to a service-account credential. Adapters
            pass the scopes they need so the grant is least-privilege.

    Resolution order: a ``google_service_account`` secret (or the
    ``VGI_GOOGLE_SERVICE_ACCOUNT_FILE`` env hatch) → a ``google_api_key`` secret
    (or ``VGI_GOOGLE_API_KEY``). Returns an empty :class:`GoogleAuth` (``has_any``
    False) when nothing is configured; the caller decides whether that is fatal.
    """
    sa_secret = _get_secret(secrets, SECRET_SERVICE_ACCOUNT)
    if sa_secret:
        info = _extract_service_account_info(sa_secret)
        if info is not None:
            secret_scopes = _scopes_from(sa_secret.get("scopes"))
            subject = _as_py(sa_secret.get("subject")) if "subject" in sa_secret else None
            creds = _service_account_credentials(
                info, scopes=scopes or secret_scopes, subject=subject
            )
            return GoogleAuth(credentials=creds)

    sa_file = os.environ.get(ENV_SERVICE_ACCOUNT_FILE)
    if sa_file:
        creds = _service_account_credentials(sa_file.strip(), scopes=scopes, subject=None)
        return GoogleAuth(credentials=creds)

    key_secret = _get_secret(secrets, SECRET_API_KEY)
    if key_secret:
        for key in ("api_key", "key", "developer_key"):
            if key in key_secret and key_secret[key] is not None:
                return GoogleAuth(api_key=str(_as_py(key_secret[key])))

    env_key = os.environ.get(ENV_API_KEY)
    if env_key and env_key.strip():
        return GoogleAuth(api_key=env_key.strip())

    return GoogleAuth()


def _get_secret(secrets: Any, secret_type: str) -> dict[str, Any] | None:
    """Fetch a secret dict via a SecretsAccessor-like object, tolerating None."""
    if secrets is None:
        return None
    getter = getattr(secrets, "get", None)
    if getter is None:
        return None
    try:
        result = getter(secret_type)
    except TypeError:
        result = getter(secret_type, None)
    return result or None

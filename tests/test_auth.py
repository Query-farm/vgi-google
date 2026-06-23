"""Auth resolution unit tests: secret-provider first, env hatches, priority.

These exercise :func:`vgi_google.auth.resolve` against a small fake
SecretsAccessor (a dict-backed ``.get``) and the two env escape hatches, without
touching ``google.oauth2`` for the actual key parse (we stub the credential
builder so no real RSA key is needed). They assert: service-account beats
API-key; the JSON key is read from several accepted shapes; scopes/subject are
threaded through; and an empty config yields no auth (``has_any`` False).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from vgi_google import auth


class _Secrets:
    """A minimal SecretsAccessor stand-in: dict of type -> value mapping."""

    def __init__(self, values: dict[str, dict[str, Any]]) -> None:
        self._values = values

    def get(self, secret_type: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self._values.get(secret_type)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(auth.ENV_SERVICE_ACCOUNT_FILE, raising=False)
    monkeypatch.delenv(auth.ENV_API_KEY, raising=False)


@pytest.fixture()
def stub_sa(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the service-account credential builder; capture how it was called."""
    calls: list[dict[str, Any]] = []

    def fake_builder(info_or_path: Any, *, scopes: Any, subject: Any) -> str:
        calls.append({"info": info_or_path, "scopes": scopes, "subject": subject})
        return f"creds:{subject or 'none'}"

    monkeypatch.setattr(auth, "_service_account_credentials", fake_builder)
    return calls


def _sa_key() -> dict[str, Any]:
    return {"type": "service_account", "private_key": "KEY", "client_email": "svc@x.iam"}


def test_service_account_from_key_json_string(stub_sa: list[dict[str, Any]]) -> None:
    secrets = _Secrets({auth.SECRET_SERVICE_ACCOUNT: {"key_json": json.dumps(_sa_key()), "scopes": "a b"}})
    result = auth.resolve(secrets, scopes=None)
    assert result.credentials == "creds:none"
    assert result.api_key is None
    assert stub_sa[0]["scopes"] == ["a", "b"]  # parsed from the secret


def test_service_account_inline_object_with_subject(stub_sa: list[dict[str, Any]]) -> None:
    secret = {**_sa_key(), "subject": "user@workspace.com"}
    secrets = _Secrets({auth.SECRET_SERVICE_ACCOUNT: secret})
    result = auth.resolve(secrets, scopes=["scopeX"])
    assert result.credentials == "creds:user@workspace.com"
    assert stub_sa[0]["subject"] == "user@workspace.com"
    assert stub_sa[0]["scopes"] == ["scopeX"]  # caller scopes win


def test_service_account_beats_api_key(stub_sa: list[dict[str, Any]]) -> None:
    secrets = _Secrets(
        {
            auth.SECRET_SERVICE_ACCOUNT: {"key_json": json.dumps(_sa_key())},
            auth.SECRET_API_KEY: {"api_key": "AIzaKEY"},
        }
    )
    result = auth.resolve(secrets)
    assert result.credentials is not None
    assert result.api_key is None


def test_api_key_secret() -> None:
    secrets = _Secrets({auth.SECRET_API_KEY: {"api_key": "AIzaKEY"}})
    result = auth.resolve(secrets)
    assert result.api_key == "AIzaKEY"
    assert result.credentials is None


def test_env_api_key_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.ENV_API_KEY, "ENVKEY")
    result = auth.resolve(_Secrets({}))
    assert result.api_key == "ENVKEY"


def test_env_service_account_file_hatch(monkeypatch: pytest.MonkeyPatch, stub_sa: list[dict[str, Any]]) -> None:
    monkeypatch.setenv(auth.ENV_SERVICE_ACCOUNT_FILE, "/tmp/key.json")
    result = auth.resolve(_Secrets({}), scopes=["s"])
    assert result.credentials == "creds:none"
    assert stub_sa[0]["info"] == "/tmp/key.json"


def test_no_credential_is_empty() -> None:
    result = auth.resolve(_Secrets({}))
    assert result.has_any is False
    assert result.credentials is None and result.api_key is None

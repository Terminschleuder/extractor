"""API-key authentication for the ingestion API.

The backend authenticates with a long-lived API key sent in the
``Authorization`` header using the ``Api-Key`` keyword (see
``accounts/authentication.py`` in the backend). This module builds that
header so the client can attach it to every request.

    Authorization: Api-Key <raw-key>

No JWT, no refresh, no multi-auth — API key only (per the user's decision).
"""

from __future__ import annotations

from pydantic import SecretStr

from .errors import ConfigError

KEYWORD = "Api-Key"


class APIKeyAuth:
    """Attach a raw API key to requests as ``Authorization: Api-Key <key>``."""

    def __init__(self, api_key: SecretStr | str | None) -> None:
        if isinstance(api_key, SecretStr):
            self._key = api_key.get_secret_value()
        else:
            self._key = api_key

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def headers(self) -> dict[str, str]:
        if not self.configured:
            raise ConfigError(
                "EXTRACTOR_API_KEY is not set; cannot authenticate to the ingestion API."
            )
        return {"Authorization": f"{KEYWORD} {self._key}"}
"""OAuth2 bearer tokens for providers whose ``auth`` is ``oauth2``.

A client credentials grant (or a refresh token grant, when there is one)
against ``tokenUrl``, with the client authenticating in the body or with HTTP
Basic. A token is reused until shortly before it expires. Errors never include
the token endpoint's response, which can hold credentials.
"""

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from forge_common.model_provider.config import (
    ModelProviderAuthError,
    ModelProviderConfigError,
    OAuth2Auth,
)

__all__ = ["OAuth2TokenSource"]

# A token this close to expiring is renewed first.
_EXPIRY_MARGIN_MS = 5_000


@dataclass(frozen=True)
class _Credentials:
    access: str
    refresh: str | None
    # Epoch milliseconds.
    expires: float


def _form_encode(value: str) -> str:
    # application/x-www-form-urlencoded, as the client's Basic credentials must
    # be (RFC 6749, section 2.3.1), as a browser's URLSearchParams encodes
    # them: Python leaves ~ as it is.
    return quote_plus(value, safe="*").replace("~", "%7E")


def _jwt_expires_at(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, int | float) and not isinstance(exp, bool):
        return float(exp) * 1_000
    return None


class OAuth2TokenSource:
    """
    A provider's bearer token, fetched when it's first needed and renewed
    before it expires. Safe to share between concurrent requests: one fetch
    serves them all.

    :param config: The provider's ``auth``.
    :param transport: The HTTP transport, e.g. ``httpx.MockTransport`` in
        tests; httpx's own by default.
    :param now: The time in epoch milliseconds.
    """

    def __init__(
        self,
        config: OAuth2Auth,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = lambda: time.time() * 1_000,
    ) -> None:
        self._config = config
        self._transport = transport
        self._now = now
        self._credentials: _Credentials | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        """
        :return: An access token that isn't about to expire.
        :raises ModelProviderAuthError: The token endpoint refused, or answered
            with something other than a bearer token.
        :raises ModelProviderConfigError: ``expiresAt`` isn't a date.
        """
        async with self._lock:
            credentials = self._credentials
            if credentials is None:
                credentials = await self._initial()
            elif credentials.expires <= self._now() + _EXPIRY_MARGIN_MS:
                credentials = await self._request(credentials.refresh)
            self._credentials = credentials
            return credentials.access

    async def _initial(self) -> _Credentials:
        config = self._config
        if config.access_token is not None:
            configured = _Credentials(
                access=config.access_token.get_secret_value(),
                refresh=self._configured_refresh_token(),
                expires=self._configured_expires_at(),
            )
            if configured.expires > self._now() + _EXPIRY_MARGIN_MS:
                return configured
        return await self._request(self._configured_refresh_token())

    def _configured_refresh_token(self) -> str | None:
        token = self._config.refresh_token
        return token.get_secret_value() if token is not None else None

    def _configured_expires_at(self) -> float:
        config = self._config
        expires_at = config.expires_at
        if isinstance(expires_at, int):
            return float(expires_at)
        if isinstance(expires_at, datetime):
            return expires_at.timestamp() * 1_000
        if isinstance(expires_at, str):
            try:
                parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                raise ModelProviderConfigError(
                    "OAuth expiresAt must be an ISO date or epoch milliseconds"
                ) from None
            return parsed.timestamp() * 1_000
        now = self._now()
        if config.access_token is not None:
            from_jwt = _jwt_expires_at(config.access_token.get_secret_value())
            if from_jwt is not None:
                return from_jwt
            return now + config.default_expires_in_seconds * 1_000
        return now

    async def _request(self, refresh_token: str | None) -> _Credentials:
        config = self._config
        form: dict[str, str] = dict(config.token_parameters)
        form["grant_type"] = "refresh_token" if refresh_token else "client_credentials"
        if refresh_token:
            form["refresh_token"] = refresh_token
        if config.scopes:
            form["scope"] = " ".join(config.scopes)
        if config.audience:
            form["audience"] = config.audience
        headers = {
            **config.token_headers,
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        }
        secret = config.client_secret.get_secret_value()
        if config.client_authentication == "basic":
            pair = f"{_form_encode(config.client_id)}:{_form_encode(secret)}"
            headers["authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form["client_id"] = config.client_id
            form["client_secret"] = secret

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=config.token_request_timeout_ms / 1_000,
            ) as client:
                response = await client.post(config.token_url, data=form, headers=headers)
        except httpx.HTTPError as error:
            raise ModelProviderAuthError("OAuth token request failed") from error
        if not response.is_success:
            # Token endpoint bodies can contain credentials and internal
            # details: never include them in errors or logs.
            raise ModelProviderAuthError(
                f"OAuth token endpoint returned HTTP {response.status_code}"
            )
        try:
            payload: Any = response.json()
        except ValueError:
            raise ModelProviderAuthError("OAuth token endpoint returned invalid JSON") from None
        return self._credentials_from(payload, refresh_token)

    def _credentials_from(self, payload: Any, refresh_token: str | None) -> _Credentials:
        invalid = ModelProviderAuthError("OAuth token endpoint returned an invalid token response")
        if not isinstance(payload, dict):
            raise invalid
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        token_type = payload.get("token_type")
        expires_in: Any = payload.get("expires_in")
        if not isinstance(access, str) or not access:
            raise invalid
        if refresh is not None and (not isinstance(refresh, str) or not refresh):
            raise invalid
        if token_type is not None and not isinstance(token_type, str):
            raise invalid
        if expires_in is not None:
            try:
                expires_in = float(expires_in)
            except (TypeError, ValueError):
                raise invalid from None
            if isinstance(payload["expires_in"], bool) or not expires_in > 0:
                raise invalid
        if token_type is not None and token_type.lower() != "bearer":
            raise ModelProviderAuthError("OAuth token endpoint returned a non-Bearer token")

        lifetime = expires_in or self._config.default_expires_in_seconds
        # Renew a little early: a tenth of its life, at most 30 seconds.
        skew = min(30, lifetime // 10)
        return _Credentials(
            access=access,
            refresh=refresh or refresh_token,
            expires=self._now() + max(1, lifetime - skew) * 1_000,
        )

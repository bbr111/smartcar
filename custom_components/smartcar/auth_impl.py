from __future__ import annotations

import time
from typing import Any, cast

from aiohttp import ClientSession

from .auth import AbstractAuth
from .const import OAUTH2_APP_TOKEN


class AsyncConfigEntryAuth(AbstractAuth):
    """Provide Smartcar v3 app authentication tied to config entry credentials."""

    def __init__(
        self,
        websession: ClientSession,
        client_id: str,
        client_secret: str,
        host: str,
    ) -> None:
        """Initialize Smartcar auth."""
        super().__init__(websession, host)
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._expires_at = 0.0

    async def async_get_access_token(self) -> str:
        """Return a valid Smartcar v3 application access token."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        response = await self._websession.post(
            OAUTH2_APP_TOKEN,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        response.raise_for_status()
        token: dict[str, Any] = await response.json()
        self._access_token = cast("str", token["access_token"])
        self._expires_at = time.time() + int(token.get("expires_in", 3600))
        return self._access_token


class ClientCredentialsAuthImpl(AbstractAuth):
    """Smartcar v3 client-credentials authentication implementation."""

    def __init__(
        self,
        websession: ClientSession,
        client_id: str,
        client_secret: str,
        host: str,
    ) -> None:
        """Initialize Smartcar v3 client credentials auth."""
        super().__init__(websession, host)
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._expires_at = 0.0

    async def async_get_access_token(self) -> str:
        """Return a valid Smartcar v3 application access token."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        response = await self._websession.post(
            OAUTH2_APP_TOKEN,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        response.raise_for_status()
        token: dict[str, Any] = await response.json()
        self._access_token = cast("str", token["access_token"])
        self._expires_at = time.time() + int(token.get("expires_in", 3600))
        return self._access_token


class AccessTokenAuthImpl(AbstractAuth):
    """Authentication implementation for a pre-fetched access token."""

    def __init__(
        self,
        websession: ClientSession,
        access_token: str,
        host: str,
    ) -> None:
        """Init auth with an existing access token."""
        super().__init__(websession, host)
        self._access_token = access_token

    async def async_get_access_token(self) -> str:
        """Return the access token."""
        return self._access_token

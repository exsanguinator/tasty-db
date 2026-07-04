"""OAuth2 token management.

TastyTrade requires OAuth2 for all API access (session-token login is no longer
offered to API users). The personal-app flow: create an OAuth application and a
personal grant at my.tastytrade.com, which yields a long-lived refresh token.
Access tokens are minted from it via POST /oauth/token and live 15 minutes.
"""

from __future__ import annotations

import time

import requests

from .config import USER_AGENT, Config


class AuthError(RuntimeError):
    pass


class TokenManager:
    """Mints and caches short-lived access tokens from the refresh token."""

    # refresh 60s before the advertised expiry
    _EARLY_REFRESH_SECONDS = 60

    def __init__(self, config: Config, session: requests.Session | None = None):
        if not config.has_credentials:
            raise AuthError(
                "Missing credentials: set TT_CLIENT_SECRET and TT_REFRESH_TOKEN "
                "(see README for how to create an OAuth app and personal grant)."
            )
        self._config = config
        self._session = session or requests.Session()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def access_token(self) -> str:
        if self._access_token is None or time.time() >= self._expires_at:
            self._refresh()
        assert self._access_token is not None
        return self._access_token

    def _refresh(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._config.refresh_token,
            "client_secret": self._config.client_secret,
        }
        if self._config.client_id:
            data["client_id"] = self._config.client_id
        resp = self._session.post(
            f"{self._config.base_url}/oauth/token",
            data=data,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if resp.status_code != 200:
            raise AuthError(f"Token refresh failed ({resp.status_code}): {resp.text[:500]}")
        body = resp.json()
        self._access_token = body["access_token"]
        expires_in = int(body.get("expires_in", 900))
        self._expires_at = time.time() + max(expires_in - self._EARLY_REFRESH_SECONDS, 30)

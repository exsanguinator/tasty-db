"""Thin REST client for the TastyTrade open API.

Endpoints used (confirmed against developer.tastytrade.com specs):
- GET /customers/me/accounts
- GET /accounts/{account_number}/transactions   (paginated, sort=Asc)
- GET /instruments/equity-options/{symbol}
- GET /instruments/futures/{symbol}
- GET /instruments/future-options/{symbol}
- GET /instruments/future-products/{exchange}/{code} is avoided; product data
  arrives nested on the future / future-option responses.

All responses use the standard envelope: {"data": {...}, "context": ...} with
list endpoints nesting items under data.items and pagination alongside.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterator
from urllib.parse import quote

import requests

from .auth import TokenManager
from .config import USER_AGENT, Config


class ApiError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class TastyClient:
    def __init__(self, config: Config, token_manager: TokenManager | None = None):
        self._config = config
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._tokens = token_manager or TokenManager(config, self._session)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        resp = self._session.get(
            f"{self._config.base_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._tokens.access_token()}"},
            timeout=60,
        )
        if resp.status_code != 200:
            raise ApiError(resp.status_code, resp.text[:500])
        return resp.json()

    # -- accounts ----------------------------------------------------------

    def account_numbers(self) -> list[str]:
        body = self._get("/customers/me/accounts")
        items = body["data"]["items"]
        return [item["account"]["account-number"] for item in items]

    # -- transactions ------------------------------------------------------

    def iter_transactions(
        self,
        account_number: str,
        start_date: date | None = None,
        per_page: int = 1000,
    ) -> Iterator[dict]:
        """Yield all transactions for an account, oldest first."""
        page_offset = 0
        while True:
            params: dict[str, Any] = {
                "sort": "Asc",
                "per-page": per_page,
                "page-offset": page_offset,
            }
            if start_date is not None:
                params["start-date"] = start_date.isoformat()
            body = self._get(f"/accounts/{account_number}/transactions", params)
            items = body["data"]["items"]
            yield from items
            pagination = body.get("pagination") or {}
            total_pages = pagination.get("total-pages")
            page_offset += 1
            if total_pages is not None:
                if page_offset >= total_pages:
                    return
            elif len(items) < per_page:
                return

    # -- instruments -------------------------------------------------------

    def _instrument(self, path_prefix: str, symbol: str) -> dict:
        body = self._get(f"{path_prefix}/{quote(symbol, safe='')}")
        return body["data"]

    def equity_option(self, symbol: str) -> dict:
        return self._instrument("/instruments/equity-options", symbol)

    def future(self, symbol: str) -> dict:
        return self._instrument("/instruments/futures", symbol)

    def future_option(self, symbol: str) -> dict:
        return self._instrument("/instruments/future-options", symbol)

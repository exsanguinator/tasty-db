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

    def accounts(self) -> list[dict]:
        """Account objects visible to the grant (account-number, nickname,
        account-type-name, margin-or-cash, ...)."""
        body = self._get("/customers/me/accounts")
        return [item["account"] for item in body["data"]["items"]]

    def account_numbers(self) -> list[str]:
        return [account["account-number"] for account in self.accounts()]

    def _iter_pages(
        self, path: str, params: dict[str, Any], per_page: int
    ) -> Iterator[dict]:
        """Yield items across a paginated list endpoint (page-offset/per-page)."""
        page_offset = 0
        while True:
            body = self._get(
                path, {**params, "per-page": per_page, "page-offset": page_offset}
            )
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

    # -- transactions ------------------------------------------------------

    def iter_transactions(
        self,
        account_number: str,
        start_date: date | None = None,
        per_page: int = 1000,
    ) -> Iterator[dict]:
        """Yield all transactions for an account, oldest first."""
        params: dict[str, Any] = {"sort": "Asc"}
        if start_date is not None:
            params["start-date"] = start_date.isoformat()
        yield from self._iter_pages(
            f"/accounts/{account_number}/transactions", params, per_page
        )

    # -- balances ------------------------------------------------------------

    def iter_balance_snapshots(
        self,
        account_number: str,
        start_date: date | None = None,
        end_date: date | None = None,
        time_of_day: str = "EOD",
        per_page: int = 1000,
    ) -> Iterator[dict]:
        """Yield historical AccountBalanceSnapshot items (kebab-case keys:
        snapshot-date, net-liquidating-value, cash-balance, ...)."""
        params: dict[str, Any] = {"time-of-day": time_of_day}
        if start_date is not None:
            params["start-date"] = start_date.isoformat()
        if end_date is not None:
            params["end-date"] = end_date.isoformat()
        yield from self._iter_pages(
            f"/accounts/{account_number}/balance-snapshots", params, per_page
        )

    def net_liq_history(self, account_number: str, time_back: str = "all") -> list[dict]:
        """Daily net-liq OHLC history. NOTE: camelCase response keys (like
        /market-data/by-type) and production-only — sandbox has no data."""
        body = self._get(
            f"/accounts/{account_number}/net-liq/history", {"time-back": time_back}
        )
        return body["data"]["items"]

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

    # -- market data ---------------------------------------------------------

    # NOTE: this endpoint returns camelCase keys (Java service), unlike the
    # kebab-case used everywhere else, and takes singular hyphenated params.
    MARKET_DATA_BATCH_LIMIT = 100

    def market_data_by_type(self, symbols_by_type: dict[str, list[str]]) -> list[dict]:
        """Point-in-time snapshots for symbols grouped by query param name
        ("equity", "equity-option", "future", "future-option"). Batches to the
        combined 100-symbols-per-request limit."""
        flat = [
            (param, symbol)
            for param, symbols in symbols_by_type.items()
            for symbol in symbols
        ]
        items: list[dict] = []
        for start in range(0, len(flat), self.MARKET_DATA_BATCH_LIMIT):
            params: dict[str, list[str]] = {}
            for param, symbol in flat[start:start + self.MARKET_DATA_BATCH_LIMIT]:
                params.setdefault(f"{param}[]", []).append(symbol)
            body = self._get("/market-data/by-type", params)
            items.extend(body["data"]["items"])
        return items

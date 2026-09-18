"""Alpaca HTTP client. Only the PAPER trading host exists in this codebase (ADR-0020); market data is host-agnostic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
MARKET_DATA_URL = "https://data.alpaca.markets"


@dataclass(frozen=True)
class AlpacaCredentials:
    key: str
    secret: str

    @property
    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}


class AlpacaClient:
    def __init__(
        self, creds: AlpacaCredentials | None, http: httpx.Client | None = None, trading_url: str = PAPER_TRADING_URL
    ):
        if not trading_url.startswith(PAPER_TRADING_URL):
            raise ValueError("only the paper trading host is allowed in this codebase (ADR-0020)")
        self.creds = creds
        self.trading_url = trading_url
        self.http = http or httpx.Client(timeout=15.0)

    @property
    def authenticated(self) -> bool:
        return self.creds is not None

    def get(self, path: str, params: dict[str, Any] | None = None, base: str | None = None) -> Any:
        if self.creds is None:
            raise RuntimeError("ALPACA_CREDENTIALS_MISSING")
        r = self.http.get(f"{base or self.trading_url}{path}", params=params, headers=self.creds.headers)
        if r.status_code >= 400:
            raise AlpacaHttpError(r.status_code, r.text[:500])
        return r.json()

    def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> Any:
        if self.creds is None:
            raise RuntimeError("ALPACA_CREDENTIALS_MISSING")
        r = self.http.request(method, f"{self.trading_url}{path}", json=json, params=params, headers=self.creds.headers)
        if r.status_code >= 400:
            raise AlpacaHttpError(r.status_code, r.text[:500])
        return r.json() if r.content else None

    def post(self, path: str, json: dict[str, Any]) -> Any:
        return self._request("POST", path, json=json)

    def patch(self, path: str, json: dict[str, Any]) -> Any:
        return self._request("PATCH", path, json=json)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)


class AlpacaHttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body

"""Rate-aware read-only Binance REST metadata/time client."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PublicBinanceREST:
    def __init__(self, spot_base: str, futures_base: str, client: httpx.AsyncClient | None = None):
        self.bases = {"spot": spot_base, "futures": futures_base}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0),
                                                   headers={"User-Agent": "AegisTradeReadOnly/0.1"})
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def _get(self, market: str, path: str, params: dict | None = None) -> dict:
        if market not in self.bases:
            raise ValueError("invalid market")
        url = self.bases[market] + path
        for attempt in range(4):
            r = await self.client.get(url, params=params)
            if r.status_code in (418, 429):
                if r.status_code == 418 or attempt == 3:
                    raise RuntimeError(f"Binance rate-limited/banned request HTTP {r.status_code}")
                retry_header = r.headers.get("retry-after", "1")
                try:
                    sleep = min(max(int(retry_header), 1), 60)
                except ValueError:
                    sleep = 2 ** attempt
                await asyncio.sleep(sleep)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("REST request retries exceeded")

    async def server_time(self, market: str) -> int:
        path = "/api/v3/time" if market == "spot" else "/fapi/v1/time"
        result = await self._get(market, path)
        return int(result["serverTime"])

    async def exchange_info(self, market: str, symbols: tuple[str, ...]) -> list[dict[str, Any]]:
        path = "/api/v3/exchangeInfo" if market == "spot" else "/fapi/v1/exchangeInfo"
        # Use the full metadata snapshot to avoid assuming uniform symbol filter support.
        body = await self._get(market, path)
        wanted = set(symbols)
        found = [i for i in body.get("symbols", []) if i.get("symbol") in wanted]
        missing = wanted - {s["symbol"] for s in found}
        if missing:
            raise ValueError(f"Unavailable exchange symbols for {market}: {sorted(missing)}")
        return found

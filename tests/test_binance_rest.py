import httpx
import pytest

from trader.binance_rest import PublicBinanceREST


@pytest.mark.asyncio
async def test_public_metadata_both_markets():
    paths = []

    def handle(req: httpx.Request):
        paths.append(req.url.path)
        if req.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": 12345})
        return httpx.Response(200, json={"symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "filters": []},
            {"symbol": "OTHERUSDT", "baseAsset": "OTHER", "quoteAsset": "USDT"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rest = PublicBinanceREST("https://example.com", "https://futures.example.com", client)
        assert await rest.server_time("spot") == 12345
        assert len(await rest.exchange_info("spot", ("BTCUSDT",))) == 1
        assert len(await rest.exchange_info("futures", ("BTCUSDT",))) == 1
        assert "/api/v3/exchangeInfo" in paths
        assert "/fapi/v1/exchangeInfo" in paths


@pytest.mark.asyncio
async def test_missing_symbol_fails_closed():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"symbols": []}))) as client:
        rest = PublicBinanceREST("https://example.com", "https://example.com", client)
        with pytest.raises(ValueError, match="Unavailable"):
            await rest.exchange_info("spot", ("BTCUSDT",))

import time
from types import SimpleNamespace

import httpx
import pytest

# Runtime API dependencies are installed by pyproject.toml in Docker and CI.
pytest.importorskip("redis")
from trader.api import app
from trader.security import issue_token

KEY = "module-1-test-jwt-key-32-chars-long-at-minimum"


class FakeStore:
    async def user_by_id(self, user_id):
        return (1, "ADMIN", True) if user_id == 1 else None

    async def latest(self, market=None, symbol=None):
        return [{"market": "spot", "symbol": "BTCUSDT", "event_type": "kline",
                 "event_id": "x", "exchange_ts": 42, "received_ts": int(time.time() * 1000),
                 "payload": {"close": "100"}}]

    async def streams(self):
        return [{"market": "spot", "connected": True, "last_event_ts": int(time.time() * 1000),
                 "reconnects": 0, "last_error": None, "updated_at": "test"}]

    async def instruments(self, market=None):
        return []

    async def unresolved_gaps(self):
        return []


@pytest.mark.asyncio
async def test_authenticated_read_only_monitoring():
    app.state.config = SimpleNamespace(jwt_secret=KEY)
    app.state.db = FakeStore()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/v1/market/latest")
        assert r.status_code == 401
        token = issue_token(1, "ADMIN", KEY)
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.get("/v1/market/latest", headers=headers)
        assert r.status_code == 200 and r.json()["count"] == 1
        assert (await client.get("/v1/market/streams", headers=headers)).json()["streams"][0]["healthy"]
        assert not (await client.get("/v1/trading/capabilities", headers=headers)).json()["live_trading_enabled"]
        assert (await client.post("/v1/orders", headers=headers)).status_code == 404

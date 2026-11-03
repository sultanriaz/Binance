"""Pinned production Spot / USD-M REST adapters, with no automatic write retry."""
from __future__ import annotations
import time
import httpx
from trader.exchange.adapters import SandboxAdapter
from trader.exchange.transport import SandboxTransport, ExchangeError
from trader.live.settings import LIVE_HOSTS

class LiveTransport(SandboxTransport):
    def __init__(self, code: str, api_key: str, secret: str, *, client=None):
        if code not in LIVE_HOSTS or not api_key or not secret:
            raise ValueError('Invalid live environment or missing credentials')
        self.code=code
        self.host=LIVE_HOSTS[code]
        self.api_key=api_key
        self.secret=secret
        self.client=client or httpx.AsyncClient(timeout=httpx.Timeout(10.0),follow_redirects=False)
        self.owns_client=client is None
        self.clock_offset=0

    async def synchronize(self):
        start=int(time.time()*1000)
        resp=await self.request('GET','/api/v3/time' if self.code=='spot-live' else '/fapi/v1/time')
        end=int(time.time()*1000)
        self.clock_offset=int(resp['serverTime'])-(start+end)//2
        if abs(self.clock_offset)>5000:
            raise ExchangeError('Clock offset too large for live orders')

    async def request(self, method: str, path: str, params: dict | None = None, *, signed=False, key_only=False):
        orig_host = self.host
        if path.startswith('/sapi/'):
            self.host = 'https://api.binance.com'
        try:
            return await super().request(method, path, params, signed=signed, key_only=key_only)
        finally:
            self.host = orig_host


class LiveAdapter(SandboxAdapter):
    def __init__(self, transport: LiveTransport):
        if transport.code not in LIVE_HOSTS:
            raise ValueError('Only dedicated live transport is permitted')
        self.t=transport
        self.code=transport.code
        self.market='spot' if self.code=='spot-live' else 'futures'
        self.prefix='/api/v3' if self.market=='spot' else '/fapi/v1'

    async def submit(self, payload: dict, client_id: str):
        # Spot supported: MARKET/LIMIT. Futures: MARKET entry or reduce-only;
        # conditional STOP_MARKET order is placed by protection workflow.
        if self.market=='spot':
            if payload['type'] not in ('MARKET','LIMIT'):
                raise ValueError('Live spot supports market and limit only')
        else:
            if payload['type'] not in ('MARKET','STOP_MARKET','TAKE_PROFIT_MARKET'):
                raise ValueError('Live futures supports market, protective stops, and take profit only')
            if payload['type'] in ('STOP_MARKET','TAKE_PROFIT_MARKET') and not payload.get('reduce_only'):
                raise ValueError(f"{payload['type']} must reduce only")
        return await super().submit(payload,client_id)

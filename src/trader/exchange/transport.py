"""Minimal signed Binance sandbox HTTP client. Never retries an order write."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from trader.exchange.settings import HOSTS


class ExchangeError(Exception):
    def __init__(self, reason: str, code: int | str | None = None):
        super().__init__(reason)
        self.code = code


class UncertainDelivery(ExchangeError):
    """A write may have reached the exchange. Query its stable client ID, never blindly retry."""


class SandboxTransport:
    def __init__(self, code: str, api_key: str, secret: str, *, client: httpx.AsyncClient | None = None):
        if code not in HOSTS:
            raise ValueError('Unrecognized or non-sandbox exchange endpoint')
        if not api_key or not secret:
            raise ValueError('Sandbox credentials required')
        self.code = code
        self.host = HOSTS[code][0]
        self.api_key = api_key
        self.secret = secret
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0), follow_redirects=False)
        self.owns_client = client is None
        self.clock_offset = 0

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    @staticmethod
    def signature(secret: str, params: dict) -> tuple[str, str]:
        query = urlencode([(k, str(v)) for k, v in params.items() if v is not None])
        sign = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return query, sign

    async def synchronize(self):
        path = '/api/v3/time' if self.code == 'spot-testnet' else '/fapi/v1/time'
        start = int(time.time() * 1000)
        data = await self.request('GET', path)
        end = int(time.time() * 1000)
        self.clock_offset = int(data['serverTime']) - (start + end) // 2
        if abs(self.clock_offset) > 10000:
            raise ExchangeError('Local clock drift exceeds 10 seconds')

    async def request(self, method: str, path: str, params: dict | None = None, *, signed=False, key_only=False):
        method = method.upper()
        if method not in ('GET', 'POST', 'PUT', 'DELETE') or not path.startswith('/') or '://' in path:
            raise ValueError('Unsafe endpoint or HTTP method')
        params = dict(params or {})
        if signed:
            params['timestamp'] = int(time.time() * 1000) + self.clock_offset
            params['recvWindow'] = 5000
            raw, sign = self.signature(self.secret, params)
            payload = f'{raw}&signature={sign}'
        else:
            payload = urlencode(params)
        headers = {'X-MBX-APIKEY': self.api_key} if signed or key_only else {}
        url = self.host + path
        try:
            # No automatic HTTP retries for POST/DELETE. ALL uncertain writes require reconciliation.
            if method == 'GET':
                res = await self.client.request('GET', url + (('?' + payload) if payload else ''), headers=headers)
            else:
                res = await self.client.request(method, url, content=payload,
                                                headers={**headers, 'Content-Type': 'application/x-www-form-urlencoded'})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if method != 'GET':
                raise UncertainDelivery('Network failure during exchange write; execution outcome UNKNOWN') from exc
            raise ExchangeError('Exchange read unavailable') from exc
        if 300 <= res.status_code < 400:
            raise ExchangeError('Exchange redirect refused')
        try:
            body = res.json()
        except ValueError:
            body = {}
        if res.status_code >= 400:
            err_code = body.get('code') if isinstance(body, dict) else None
            msg = str(body.get('msg', 'Exchange rejected request'))[:180] if isinstance(body, dict) else 'Exchange rejected request'
            # Binance 5xx, -1006, -1007 can have UNKNOWN delivery. Treat as uncertain.
            if method != 'GET' and (res.status_code >= 500 or err_code in (-1006,-1007)):
                raise UncertainDelivery('Exchange server error; write outcome UNKNOWN', err_code)
            if res.status_code in (418, 429):
                raise ExchangeError('Binance rate limited / IP banned; operator action required', res.status_code)
            raise ExchangeError(msg, err_code or res.status_code)
        return body

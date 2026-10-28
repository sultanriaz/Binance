"""Defense in depth for the web operator control plane and release environment."""
from __future__ import annotations

import hashlib
import os
from urllib.parse import urlsplit
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from fastapi import HTTPException


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            oversized = int(request.headers.get('content-length','0')) > 1_048_576
        except ValueError:
            return JSONResponse({'detail':'Invalid request length'},status_code=400)
        if oversized:
            return JSONResponse({'detail':'Request too large'},status_code=413)
        if os.getenv('DEPLOYMENT_MODE') in ('staging','production'):
            allowed = {os.getenv('STAGING_HOST','').lower(),os.getenv('PRODUCTION_HOST','').lower(),'gateway','localhost','127.0.0.1'}
            host = request.headers.get('host','').split(':')[0].lower()
            if host not in allowed or not host:
                return JSONResponse({'detail':'Unrecognized host'},status_code=400)
        response: Response = await call_next(request)
        headers={
            'X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY',
            'Referrer-Policy':'no-referrer',
            'Permissions-Policy':'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy':("default-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
            'Cache-Control':'no-store',
            'Pragma':'no-cache',
        }
        if request.url.scheme == 'https' or os.getenv('TRUSTED_TLS_PROXY') == 'true':
            headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        for key,value in headers.items():
            response.headers[key]=value
        if 'server' in response.headers:
            del response.headers['server']
        return response


async def enforce_mfa_replay(cache, subject: str, code: str) -> None:
    """Disallow the same MFA token for another operation inside a two-step window.

    Fail closed when Redis fails or is unavailable. No raw code stored in cache keys.
    """
    digest=hashlib.sha256(f'{subject}:{code}'.encode()).hexdigest()
    try:
        fresh=await cache.set(f'mfa_once:{digest}','1',nx=True,ex=90)
    except Exception:
        raise HTTPException(503,'MFA verification temporarily unavailable') from None
    if not fresh:
        raise HTTPException(409,'This one-time code was already used')


def validate_deployment_environment(env: dict | None = None) -> None:
    cfg = env if env is not None else os.environ
    if cfg.get('ENABLE_LIVE_TRADING','false').lower() != 'false':
        raise RuntimeError('LIVE_TRADING_MUST_REMAIN_DISABLED')
    mode=cfg.get('DEPLOYMENT_MODE','development')
    if mode not in ('development','staging','production'):
        raise RuntimeError('Unsupported deployment mode; this release is not production-live-capable')
    if mode in ('staging','production'):
        if len(cfg.get('JWT_SECRET','')) < 48:
            raise RuntimeError('Staging/production require a random JWT_SECRET of 48+ characters')
        if cfg.get('STAGING_DB_ROLE') != 'trader_gateway':
            raise RuntimeError('Staging must use segregated gateway database credentials')
        if cfg.get('ENABLE_TESTNET_EXECUTION','false').lower() != 'false':
            raise RuntimeError('Staging defaults to read-only; use a separately reviewed test execution environment')
        for endpoint in ('BINANCE_SPOT_REST','BINANCE_FUTURES_REST'):
            if endpoint in cfg and urlsplit(cfg[endpoint]).scheme != 'https':
                raise RuntimeError('Staging/production market REST requires HTTPS')
        if cfg.get('TRUSTED_TLS_PROXY') != 'true':
            raise RuntimeError('Staging/production require verified TLS ingress proxy configuration')

    if mode == 'production':
        if cfg.get('LIVE_CONTROL_ENABLED','false').lower() != 'true':
            raise RuntimeError('Production must explicitly activate gateway live control policy')
        if not cfg.get('PRODUCTION_HOST') or cfg.get('TRUSTED_TLS_PROXY') != 'true':
            raise RuntimeError('Production domain and trusted TLS proxy are required')

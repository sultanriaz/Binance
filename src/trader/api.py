"""Authenticated read-only market-monitoring API. No exchange write methods."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import redis.asyncio as redis
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field

from trader.config import Settings
from trader.security import check_token, issue_token, password_verify, verify_totp
from trader.operations.security import SecurityHeadersMiddleware, enforce_mfa_replay, validate_deployment_environment
from trader.storage import Storage

LOG = logging.getLogger(__name__)
LIVE_UNKNOWN = Gauge('trader_live_unknown_order_intents', 'Live orders requiring investigation')
LIVE_OPEN_INCIDENTS = Gauge('trader_live_open_incidents', 'Live incidents still active')
LIVE_ARMED_ACCOUNTS = Gauge('trader_live_armed_accounts', 'Live accounts with new-entry permission')
OPERATIONS_DB_UP = Gauge('trader_operations_database_up', 'Operational PostgreSQL usable')
OPERATIONS_UNKNOWN = Gauge('trader_operations_unknown_exchange_orders', 'Exchange write intents with unknown status')
OPERATIONS_INCIDENTS = Gauge('trader_operations_open_incidents', 'Unresolved system or exchange incidents')
OPERATIONS_NO_GO = Gauge('trader_operations_readiness_no_go', 'Latest shadow review verdict is NO_GO')
AUTH_ATTEMPTS = Counter("trader_api_login_attempts_total", "Login attempts", ["outcome"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)
    totp: str = Field(min_length=6, max_length=6)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Settings.load()
    validate_deployment_environment()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO))
    db = Storage(cfg.database_url)
    await db.open()
    cache = redis.from_url(cfg.redis_url, socket_connect_timeout=2, socket_timeout=2)
    app.state.config = cfg
    app.state.db = db
    app.state.redis = cache
    try:
        yield
    finally:
        await cache.aclose()
        await db.close()


app = FastAPI(title="AegisTrade Controlled Live + Sandbox API", version="0.6.0", lifespan=lifespan,
              description="Module 6: separate live control plane, explicit approvals, risk-bound dedicated live worker.",
              docs_url=None, redoc_url=None)


app.add_middleware(SecurityHeadersMiddleware)


async def authorized(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = check_token(authorization[7:], request.app.state.config.jwt_secret)
        row = await request.app.state.db.user_by_id(int(claims["sub"]))
        if not row or not row[2] or row[1] != claims["role"]:
            raise ValueError("account inactive or changed")
        return claims
    except (ValueError, TypeError):
        raise HTTPException(401, "Invalid or expired session", headers={"WWW-Authenticate": "Bearer"}) from None


@app.get("/")
async def home():
    return FileResponse(Path("/app/web/index.html") if Path("/app/web/index.html").exists()
                        else Path(__file__).resolve().parents[2] / "web" / "index.html")


@app.get('/legacy')
async def legacy():
    return FileResponse(Path('/app/web/legacy.html') if Path('/app/web/legacy.html').exists()
                        else Path(__file__).resolve().parents[2] / 'web' / 'legacy.html')


@app.get('/terminal.js')
async def terminal_script():
    return FileResponse(Path('/app/web/terminal.js') if Path('/app/web/terminal.js').exists()
                        else Path(__file__).resolve().parents[2] / 'web' / 'terminal.js',
                        media_type='text/javascript')


@app.get("/healthz")
async def healthz():
    return {"status": "alive", "live_worker_status": "not_disclosed", "live_controls_enabled": __import__("os").getenv("LIVE_CONTROL_ENABLED", "false") == "true"}


@app.get("/readyz")
async def readyz(request: Request):
    try:
        await asyncio.wait_for(request.app.state.db.ping(), timeout=3)
        return {"status": "ready"}
    except Exception as exc:
        LOG.error("database not ready: %s", exc)
        raise HTTPException(503, "database unavailable") from None


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    try:
        async with request.app.state.db.pool.connection() as conn:
            row = await (await conn.execute("""SELECT
              (SELECT COUNT(*) FROM exchange_order_intents WHERE status IN ('UNKNOWN','SUBMITTING','CANCEL_PENDING')),
              (SELECT COUNT(*) FROM exchange_incidents WHERE acknowledged_at IS NULL)+
              (SELECT COUNT(*) FROM operations_incidents WHERE resolved_at IS NULL),
              (SELECT verdict FROM operations_readiness ORDER BY id DESC LIMIT 1)""")).fetchone()
        OPERATIONS_UNKNOWN.set(row[0])
        OPERATIONS_INCIDENTS.set(row[1])
        OPERATIONS_NO_GO.set(1 if row[2] != 'REVIEW_REQUIRED' else 0)
        try:
            async with request.app.state.db.pool.connection() as conn:
                live=await (await conn.execute("""SELECT
                  (SELECT COUNT(*) FROM live_order_intents WHERE status IN ('UNKNOWN','SUBMITTING','CANCEL_PENDING')),
                  (SELECT COUNT(*) FROM live_incidents WHERE acknowledged_at IS NULL),
                  (SELECT COUNT(*) FROM live_accounts WHERE armed=TRUE)""")).fetchone()
            LIVE_UNKNOWN.set(live[0]); LIVE_OPEN_INCIDENTS.set(live[1]); LIVE_ARMED_ACCOUNTS.set(live[2])
        except Exception:
            LIVE_UNKNOWN.set(1); LIVE_OPEN_INCIDENTS.set(1); LIVE_ARMED_ACCOUNTS.set(0)
        OPERATIONS_DB_UP.set(1)
    except Exception:
        OPERATIONS_DB_UP.set(0)
        LOG.exception('Operational metrics query failed')
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def enforce_login_limit(request: Request, username: str) -> None:
    """Fail closed for new logins if the throttling backend is unavailable."""
    cache = request.app.state.redis
    ip = request.client.host if request.client else "unknown"
    # Hash credentials in keys to avoid creating redis metadata with raw usernames.
    import hashlib
    key = "login_limit:" + hashlib.sha256(f"{ip}:{username.lower()}".encode()).hexdigest()
    try:
        n = await cache.incr(key)
        if n == 1:
            await cache.expire(key, 300)
        if n > 8:
            raise HTTPException(429, "Too many login attempts. Try again later.")
    except HTTPException:
        raise
    except Exception as exc:
        LOG.warning("login limiter unavailable: %s", exc)
        raise HTTPException(503, "Authentication temporarily unavailable") from None


@app.post("/v1/auth/login")
async def login(req: LoginRequest, request: Request):
    await enforce_login_limit(request, req.username)
    db: Storage = request.app.state.db
    row = await db.user_by_name(req.username)
    if (not row or not row[5] or not password_verify(req.password, row[2]) or
            not row[4] or not verify_totp(row[4], req.totp)):
        AUTH_ATTEMPTS.labels(outcome="failure").inc()
        # Avoid logging passwords/codes. Do not disclose which factor failed.
        await db.audit(req.username, "auth.login", "rejected")
        raise HTTPException(401, "Invalid credentials or one-time code")
    await enforce_mfa_replay(request.app.state.redis, str(row[0]), req.totp)
    AUTH_ATTEMPTS.labels(outcome="success").inc()
    await db.audit(req.username, "auth.login", "accepted")
    return {"access_token": issue_token(row[0], row[3], request.app.state.config.jwt_secret),
            "token_type": "bearer", "expires_in": 900, "role": row[3]}


@app.get("/v1/auth/me")
async def me(claims: Annotated[dict, Depends(authorized)]):
    return {"subject": claims["sub"], "role": claims["role"], "expires_at": claims["exp"]}


@app.get("/v1/market/latest")
async def latest(request: Request, claims: Annotated[dict, Depends(authorized)],
                 market: Literal["spot", "futures"] | None = None,
                 symbol: str | None = Query(default=None, min_length=5, max_length=20, pattern="^[A-Z0-9]+$")):
    events = await request.app.state.db.latest(market=market, symbol=symbol)
    now_ms = int(time.time() * 1000)
    for item in events:
        item["age_ms"] = now_ms - item["received_ts"]
        item["stale"] = item["age_ms"] > 15000
    return {"events": events, "server_time_ms": now_ms, "count": len(events)}


@app.get("/v1/market/streams")
async def streams(request: Request, claims: Annotated[dict, Depends(authorized)]):
    states = await request.app.state.db.streams()
    now_ms = int(time.time() * 1000)
    for s in states:
        s["stale"] = s["last_event_ts"] is None or now_ms - s["last_event_ts"] > 30000
        s["healthy"] = bool(s["connected"]) and not s["stale"]
    return {"streams": states, "server_time_ms": now_ms}


@app.get("/v1/market/data-quality")
async def data_quality(request: Request, claims: Annotated[dict, Depends(authorized)]):
    gaps = await request.app.state.db.unresolved_gaps()
    return {"unresolved_gaps": gaps,
            "quality": "DEGRADED" if gaps else "NO_KNOWN_AGGTRADE_GAPS",
            "warning": "Sequence checks cannot establish complete order-book continuity or historical completeness"}


@app.get("/v1/market/instruments")
async def instruments(request: Request, claims: Annotated[dict, Depends(authorized)],
                      market: Literal["spot", "futures"] | None = None):
    items = await request.app.state.db.instruments(market)
    return {"instruments": items}


@app.get("/v1/trading/capabilities")
async def capabilities(claims: Annotated[dict, Depends(authorized)]):
    return {"module": "D0+D1+D2+D3+D4+D5+D6", "spot_orders": False, "futures_orders": False,
            "paper_orders": True, "trade_execution": "VIRTUAL_ONLY",
            "live_trading_enabled": False, "testnet_supported": True, "notes": "Live account execution is separately gated in the isolated P29-P32 worker; this endpoint never submits production orders."}

# P09-P14: admin-only, virtual-funds endpoints. No live exchange order API.
from trader.paper.http_api import router as paper_router
app.include_router(paper_router)

# Module 3: separate admin-only exchange-test control plane, no production order route.
from trader.exchange.http_api import router as exchange_router
app.include_router(exchange_router)

# P20–P24: research, terminal, approval-only, never a production order executor.
from trader.governance.http_api import router as governance_router
from trader.intelligence.http_api import router as research_router
from trader.terminal import router as terminal_router
app.include_router(governance_router)
app.include_router(research_router)
app.include_router(terminal_router)

# P25–P28: administrative incident and shadow evidence, no exchange order side effects.
from trader.operations.http_api import router as operations_router
app.include_router(operations_router)

# P29-P32: production controls; routes independently guard live deployment mode.
from trader.live.http_api import router as live_router
app.include_router(live_router)

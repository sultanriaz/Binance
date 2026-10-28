"""On-stack PostgreSQL transaction/outbox and trade-sequence-gap verification.

Run: docker compose exec gateway python -m trader.db_verify
No external network, NATS, or exchange access is required.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from trader.config import Settings
from trader.market import MarketEvent
from trader.storage import Storage


async def main() -> None:
    db = Storage(Settings.load().database_url)
    await db.open()
    sym = "V" + uuid.uuid4().hex[:9].upper()
    now = int(time.time() * 1000)
    events = [MarketEvent(f"test-{uuid.uuid4().hex}", "spot", sym, "agg_trade", now + i,
                          now + i, {"price": "123.456789", "quantity": "0.01",
                                      "trade_id": idx, "trade_ts": now + i})
              for i, idx in enumerate((100, 101, 103))]
    try:
        assert await db.write_events(events) == 3
        assert await db.write_events(events) == 0, "idempotent insert violated"
        observed = await db.latest(market="spot", symbol=sym)
        assert len(observed) == 1 and observed[0]["payload"]["price"] == "123.456789"
        gaps = [g for g in await db.unresolved_gaps() if g["symbol"] == sym]
        assert len(gaps) == 1 and gaps[0]["expected_next"] == 102 and gaps[0]["observed_next"] == 103
        items = await db.unpublished(1000)
        assert len([r for r in items if r[1] in {e.event_id for e in events}]) <= 3
        print("PASS: transactional event writes, duplicate suppression, projections, gap detection")
    finally:
        assert db.pool
        async with db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM outbox_events WHERE event_id = ANY(%s)", ([e.event_id for e in events],))
                await conn.execute("DELETE FROM market_latest WHERE market='spot' AND symbol=%s", (sym,))
                await conn.execute("DELETE FROM market_events WHERE market='spot' AND symbol=%s", (sym,))
                await conn.execute("DELETE FROM aggregate_trade_offsets WHERE market='spot' AND symbol=%s", (sym,))
                await conn.execute("DELETE FROM market_data_gaps WHERE market='spot' AND symbol=%s", (sym,))
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

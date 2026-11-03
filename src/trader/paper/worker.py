"""Paper runner: process fresh live quotes and closed 1m candles transactionally.

No REST order call exists. Auto strategy defaults disabled. Stops on data gaps,
old quotes, and incomplete indicator history. Runs 1 worker in Compose; even if
scaled, row locks serialize account and strategy processing.
"""
from __future__ import annotations

import asyncio
import logging
import time

from trader.config import Settings
from trader.paper.domain import Account, Quote, dec
from trader.paper.engine import mark, place_order, process_quote
from trader.paper.repository import PaperRepository, policy_from_json
from trader.paper.risk import RiskRejected
from trader.paper.strategy import sma_signal, desired_order
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

LOG=logging.getLogger(__name__)


def quote_from_row(market: str,symbol: str,row) -> Quote | None:
    if row is None:
        return None
    payload,ts=row
    try:
        return Quote(market,symbol,dec(payload['best_bid']),dec(payload['best_ask']),
                     dec(payload['bid_qty']),dec(payload['ask_qty']),int(ts))
    except (KeyError,ValueError,TypeError):
        return None


async def _get_quote(conn,market:str,symbol:str) -> Quote | None:
    row=await (await conn.execute('''SELECT payload,received_ts FROM market_latest
        WHERE market=%s AND symbol=%s AND event_type='book_ticker' ''',
        (market,symbol))).fetchone()
    return quote_from_row(market,symbol,row)


async def update_limit_orders(storage:Storage) -> int:
    """Fresh book snapshots are each consumed once per account/instrument."""
    changed=0
    async with storage.pool.connection() as conn:
        rows=await (await conn.execute('SELECT id FROM paper_accounts ORDER BY id LIMIT 100')).fetchall()
    for (account_id,) in rows:
        async with storage.pool.connection() as conn:
            async with conn.transaction():
                row=await (await conn.execute('''SELECT state,policy FROM paper_accounts
                    WHERE id=%s FOR UPDATE SKIP LOCKED''',(account_id,))).fetchone()
                if row is None:
                    continue
                a=Account.from_dict(row[0])
                policy=policy_from_json(row[1])
                old=a.sequence
                watched={(o['market'],o['symbol']) for o in a.orders
                         if o['status'] in ('OPEN','PARTIALLY_FILLED') and o['type'] in ('LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET')}
                watched.update(('spot',sym) for sym,h in a.spot.items() if dec(h['qty'])>0)
                watched.update(('futures',sym) for sym,h in a.futures.items() if dec(h['qty'])!=0)
                for market,symbol in watched:
                    cur=await (await conn.execute('''SELECT event_id,payload,received_ts FROM market_latest
                      WHERE market=%s AND symbol=%s AND event_type='book_ticker' ''',(market,symbol))).fetchone()
                    if cur is None:
                        continue
                    event_id,payload,ts=cur
                    key=f'{market}:{symbol}'
                    if a.seen_quotes.get(key)==event_id or abs(int(time.time()*1000)-ts)>15000:
                        continue
                    quote=quote_from_row(market,symbol,(payload,ts))
                    if quote is None:
                        continue
                    process_quote(a,quote,policy)
                    a.seen_quotes[key]=event_id
                if a.sequence != old or a.seen_quotes != row[0].get('seen_quotes',{}):
                    await PaperRepository._save(conn,a,old)
                    changed+=1
    return changed


async def run_strategies(storage:Storage) -> int:
    from trader.paper.history import INTERVAL_MS
    processed=0
    async with storage.pool.connection() as conn:
        ids=await (await conn.execute('''SELECT id FROM paper_strategies WHERE enabled=TRUE
            ORDER BY created_at LIMIT 100''')).fetchall()
    for (strategy_id,) in ids:
        async with storage.pool.connection() as conn:
            async with conn.transaction():
                st=await (await conn.execute('''SELECT account_id,market,symbol,fast_window,
                    slow_window,allocation_fraction,last_closed_bar_ms
                    FROM paper_strategies WHERE id=%s AND enabled=TRUE FOR UPDATE SKIP LOCKED''',
                    (strategy_id,))).fetchone()
                if st is None:
                    continue
                account_id,market,symbol,fast,slow,allocation,last=st
                # Read only closed candles. Only act on recent latest bar.
                candle_rows=list(await (await conn.execute('''SELECT (payload->>'open_ts')::bigint,
                    (payload->>'close')::numeric FROM market_events
                    WHERE market=%s AND symbol=%s AND event_type='kline'
                      AND payload->>'interval'='1m' AND payload->>'closed'='true'
                    ORDER BY (payload->>'open_ts')::bigint DESC LIMIT %s''',
                    (market,symbol,slow))).fetchall())
                if len(candle_rows)!=slow:
                    continue
                candle_rows.reverse()
                start=candle_rows[0][0]
                if any(x[0]!=start+i*INTERVAL_MS['1m'] for i,x in enumerate(candle_rows)):
                    LOG.warning('paper strategy %s paused: candle gap',strategy_id)
                    continue
                closed_ms=candle_rows[-1][0]
                now_ms=int(time.time()*1000)
                if closed_ms<=last or not 0<=now_ms-(closed_ms+60000)<=90000:
                    continue
                quote=await _get_quote(conn,market,symbol)
                if quote is None or abs(now_ms-quote.ts_ms)>15000:
                    continue
                a_row=await (await conn.execute('''SELECT state,policy FROM paper_accounts
                    WHERE id=%s FOR UPDATE''',(account_id,))).fetchone()
                if a_row is None:
                    continue
                a=Account.from_dict(a_row[0])
                policy=policy_from_json(a_row[1])
                old=a.sequence
                mark(a,quote,policy)
                signal=sma_signal([dec(x[1]) for x in candle_rows],fast,slow)
                intended=desired_order(a,market=market,symbol=symbol,desired=signal,
                                       price=quote.mid,allocation=dec(allocation))
                if intended is not None:
                    side,quantity,reduce_only=intended
                    # Alpha Gate Check for opening BUY order
                    if side == 'BUY' and not reduce_only:
                        try:
                            canary = await (await conn.execute('''
                                SELECT id, artifact FROM research_models
                                WHERE market=%s AND symbol=%s AND status='CANARY_ACTIVE'
                                ORDER BY promoted_at DESC LIMIT 1''', (market, symbol))).fetchone()
                            if canary:
                                model_id, artifact = canary
                                from trader.intelligence.model import LinearProbabilityModel
                                from trader.paper.history import Bar
                                from trader.intelligence.features import make_features, extract_microstructure_context
                                b_rows = await (await conn.execute('''SELECT open_ms, close_ms, o, h, l, c, volume
                                    FROM historical_bars WHERE market=%s AND symbol=%s AND interval='1m' AND close_ms <= %s
                                    ORDER BY open_ms DESC LIMIT 100''', (market, symbol, closed_ms))).fetchall()
                                if len(b_rows) >= 25:
                                    b_rows = list(reversed(b_rows))
                                    bars_obj = [Bar(market, symbol, '1m', o, e, dec(o_p), dec(h_p), dec(l_p), dec(c_p), dec(v))
                                                for o, e, o_p, h_p, l_p, c_p, v in b_rows]
                                    depth_row = await (await conn.execute('''SELECT payload FROM market_latest
                                        WHERE market=%s AND symbol=%s AND event_type='depth20' ''', (market, symbol))).fetchone()
                                    ctx_row = await (await conn.execute('''SELECT payload FROM market_latest
                                        WHERE market=%s AND symbol=%s AND event_type='book_ticker' ''', (market, symbol))).fetchone()
                                    ctx = extract_microstructure_context(
                                        book_ticker_payload=ctx_row[0] if ctx_row else None,
                                        depth_payload=depth_row[0] if depth_row else None
                                    )
                                    model = LinearProbabilityModel.from_artifact(artifact)
                                    feat_rows = make_features(bars_obj, context=ctx, feature_names=model.feature_names)
                                    p_up = model.probability(feat_rows[-1])
                                    if p_up < 0.60:
                                        a.audit('ALPHA_MODEL_VETO', now_ms, strategy_id=str(strategy_id),
                                                model_id=str(model_id), p_up=round(p_up, 4), reason='prob_below_60pct')
                                        intended = None
                        except Exception:
                            pass
                    if intended is not None:
                        try:
                            place_order(a,quote,policy,side=side,quantity=quantity,
                                ts_ms=now_ms,reduce_only=reduce_only,
                                client_id=f'strategy:{strategy_id}:{closed_ms}')
                        except RiskRejected as exc:
                            a.audit('STRATEGY_REJECTED',now_ms,strategy_id=str(strategy_id),
                                    reason=exc.reason,closed_bar_ms=closed_ms)
                # Atomically advance cursor with virtual order state/ledger.
                await PaperRepository._save(conn,a,old)
                await conn.execute('''UPDATE paper_strategies SET last_closed_bar_ms=%s
                    WHERE id=%s''',(closed_ms,strategy_id))
                processed+=1
    return processed


async def main():
    cfg=Settings.load()
    logging.basicConfig(level=getattr(logging,cfg.log_level,logging.INFO))
    storage=Storage(cfg.database_url)
    await storage.open()
    try:
        while True:
            try:
                updated=await update_limit_orders(storage)
                executed=await run_strategies(storage)
                await OperationsRepository(storage).heartbeat('paper-worker')
                if updated or executed:
                    LOG.info('paper worker updated %d portfolios, evaluated %d strategies',updated,executed)
            except Exception:
                LOG.exception('paper worker iteration failed; retrying')
            await asyncio.sleep(3)
    finally:
        await storage.close()


if __name__=='__main__':
    asyncio.run(main())

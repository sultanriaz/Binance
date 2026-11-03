"""Sandbox-isolated research worker: bounded training and shadow observations.

Has zero Binance credentials; its only write access is research data. Does not
submit orders, change paper strategies, or bypass governance.
"""
from __future__ import annotations
import asyncio
import logging
import time
from trader.config import Settings
from trader.intelligence.features import make_features
from trader.intelligence.model import LinearProbabilityModel
from trader.intelligence.repository import ResearchRepo
from trader.intelligence.training import train_candidate
from trader.paper.repository import PaperRepository
from trader.storage import Storage
from trader.operations.repository import OperationsRepository

LOG=logging.getLogger('trader.intelligence.worker')


async def run_job(repo, paper, job):
    jid,market,symbol,interval,count,horizon,friction=job
    try:
        bars=await paper.bars(market,symbol,interval,count)
        if len(bars) != count:
            raise ValueError('MISSING_HISTORY')
        if bars[-1].close_ms >= int(time.time()*1000):
            raise ValueError('UNCLOSED_HISTORY')
        # Training executes off event loop, no exchanges.
        result=await asyncio.to_thread(train_candidate,bars,horizon=horizon,
                                       friction_bps=float(friction))
        await repo.finish(jid,result)
    except (ValueError,ImportError,RuntimeError) as exc:
        LOG.warning('Research job %s rejected: %s',jid,type(exc).__name__)
        await repo.failed(jid,type(exc).__name__.upper())
    except Exception:
        LOG.exception('Research job %s failed unexpectedly',jid)
        await repo.failed(jid,'UNEXPECTED_RESEARCH_FAILURE')


async def observe_shadow(repo, paper):
    async with repo.db.pool.connection() as conn:
        async with conn.transaction():
            rows = await (await conn.execute('''SELECT id,market,symbol,candle_interval,
              horizon,train_data_last_ms,artifact,status FROM research_models
              WHERE status IN ('SHADOW','CANARY_ACTIVE') ORDER BY promoted_at DESC LIMIT 30
              FOR UPDATE SKIP LOCKED''')).fetchall()
    for model_id,market,symbol,interval,horizon,last_train,artifact,current_status in rows:
        model=LinearProbabilityModel.from_artifact(artifact)
        # Enough historical context to calculate every newly arrived closed bar.
        bars=await paper.bars(market,symbol,interval,5000)
        if not bars:
            continue
        usable=[b for b in bars if b.close_ms < int(time.time()*1000)]
        if len(usable)<25:
            continue
        context = None
        try:
            async with repo.db.pool.connection() as conn:
                depth_row = await (await conn.execute('''SELECT payload FROM market_latest
                    WHERE market=%s AND symbol=%s AND event_type='depth20' ''',
                    (market, symbol))).fetchone()
                bt_row = await (await conn.execute('''SELECT payload FROM market_latest
                    WHERE market=%s AND symbol=%s AND event_type='book_ticker' ''',
                    (market, symbol))).fetchone()
                mp_row = await (await conn.execute('''SELECT payload FROM market_latest
                    WHERE market=%s AND symbol=%s AND event_type='mark_price' ''',
                    (market, symbol))).fetchone()
            from trader.intelligence.features import extract_microstructure_context
            context = extract_microstructure_context(
                book_ticker_payload=bt_row[0] if bt_row else None,
                mark_price_payload=mp_row[0] if mp_row else None,
                depth_payload=depth_row[0] if depth_row else None
            )
        except Exception:
            pass
        try:
            observations=make_features(usable, context=context, feature_names=model.feature_names)
        except ValueError:
            LOG.warning('Shadow input has missing or malformed candles: %s %s',market,symbol)
            continue

        latest_new=[r for r in observations if r.open_ms > last_train][-100:]
        bar_by_ms={b.open_ms:i for i,b in enumerate(usable)}
        async with repo.db.pool.connection() as conn:
            async with conn.transaction():
                for r in latest_new:
                    p=model.probability(r)
                    await conn.execute('''INSERT INTO shadow_predictions
                      (model_id,bar_open_ms,p_up,threshold,signal)
                      VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                      (model_id,r.open_ms,p,model.threshold,
                       'LONG' if p>=model.threshold else 'FLAT'))
                pending=await (await conn.execute('''SELECT bar_open_ms,p_up,signal
                  FROM shadow_predictions WHERE model_id=%s AND resolved_at IS NULL
                  ORDER BY bar_open_ms LIMIT 200''',(model_id,))).fetchall()
                for open_ms,p,signal in pending:
                    idx=bar_by_ms.get(open_ms)
                    if idx is None or idx + horizon >= len(usable):
                        continue
                    enter=float(usable[idx+1].o)
                    exit_price=float(usable[idx+horizon].c)
                    # Conservative fixed 15bps proxy, not a claim of live fill cost.
                    net=(exit_price/enter-1)-0.0015
                    await conn.execute('''UPDATE shadow_predictions SET
                        entry_open_ms=%s,resolved_net_return=%s,correct_label=%s,
                        resolved_at=now() WHERE model_id=%s AND bar_open_ms=%s
                        AND resolved_at IS NULL''',
                        (usable[idx+1].open_ms,net,bool((net>0)==(p>=.5)),model_id,open_ms))

        # Benchmark calculation and lifecycle evaluation
        async with repo.db.pool.connection() as conn:
            resolved_rows = await (await conn.execute('''SELECT p_up, signal, resolved_net_return
                FROM shadow_predictions WHERE model_id=%s AND resolved_at IS NOT NULL
                ORDER BY bar_open_ms ASC''', (model_id,))).fetchall()

        if not resolved_rows:
            continue

        resolved_count = len(resolved_rows)
        rolling_brier = sum((p - int(ret > 0))**2 for p, _, ret in resolved_rows) / resolved_count
        trades = [ret for _, sig, ret in resolved_rows if sig == 'LONG']
        trade_count = len(trades)

        if trade_count > 0:
            realized_win_rate = sum(1 for r in trades if r > 0) / trade_count
            compounded = 1.0
            peak = 1.0
            drawdown = 0.0
            for r in trades:
                compounded *= max(0.0, 1.0 + r)
                peak = max(peak, compounded)
                drawdown = max(drawdown, (peak - compounded) / peak)
            cumulative_return = compounded - 1.0
        else:
            realized_win_rate = sum(1 for _, _, r in resolved_rows if r > 0) / resolved_count
            compounded = 1.0
            peak = 1.0
            drawdown = 0.0
            for _, _, r in resolved_rows:
                compounded *= max(0.0, 1.0 + r)
                peak = max(peak, compounded)
                drawdown = max(drawdown, (peak - compounded) / peak)
            cumulative_return = compounded - 1.0

        await repo.update_benchmark(model_id, resolved_count, rolling_brier, realized_win_rate)

        # Autonomous promotion / retirement state machine when resolved trades/samples >= 100
        if resolved_count >= 100 or trade_count >= 100:
            if current_status == 'SHADOW':
                if rolling_brier <= 0.22 and realized_win_rate >= 0.55 and cumulative_return > 0:
                    promoted = await repo.promote_to_canary(
                        model_id, resolved_count, rolling_brier, realized_win_rate,
                        {'cumulative_return': cumulative_return, 'drawdown': drawdown, 'trade_count': trade_count}
                    )
                    if promoted:
                        LOG.info('Model %s auto-promoted from SHADOW to CANARY_ACTIVE (brier=%.4f, win_rate=%.4f, return=%.4f)',
                                 model_id, rolling_brier, realized_win_rate, cumulative_return)
                elif rolling_brier > 0.26 or drawdown > 0.08:
                    reason = 'brier_decay' if rolling_brier > 0.26 else 'drawdown_exceeded'
                    retired = await repo.auto_retire_model(
                        model_id, reason, resolved_count, rolling_brier, realized_win_rate,
                        {'cumulative_return': cumulative_return, 'drawdown': drawdown, 'trade_count': trade_count}
                    )
                    if retired:
                        LOG.info('Model %s auto-retired from SHADOW due to %s (brier=%.4f, drawdown=%.4f)',
                                 model_id, reason, rolling_brier, drawdown)
            elif current_status == 'CANARY_ACTIVE':
                if rolling_brier > 0.26 or drawdown > 0.08:
                    reason = 'canary_brier_decay' if rolling_brier > 0.26 else 'canary_drawdown_exceeded'
                    retired = await repo.auto_retire_model(
                        model_id, reason, resolved_count, rolling_brier, realized_win_rate,
                        {'cumulative_return': cumulative_return, 'drawdown': drawdown, 'trade_count': trade_count}
                    )
                    if retired:
                        LOG.info('Canary model %s auto-retired due to %s (brier=%.4f, drawdown=%.4f)',
                                 model_id, reason, rolling_brier, drawdown)


async def check_concept_drift(repo, paper) -> list[str]:
    """Evaluates Kolmogorov-Smirnov and Page-Hinkley drift on CANARY_ACTIVE models."""
    from trader.intelligence.drift import evaluate_model_drift
    from trader.intelligence.features import make_features, extract_microstructure_context
    from trader.intelligence.model import LinearProbabilityModel
    from trader.live.alerts import send_alert

    drifted_models = []
    async with repo.db.pool.connection() as conn:
        rows = await (await conn.execute('''
            SELECT id, market, symbol, candle_interval, horizon, train_data_last_ms, artifact
            FROM research_models
            WHERE status='CANARY_ACTIVE'
            FOR UPDATE SKIP LOCKED
        ''')).fetchall()

    for model_id, market, symbol, interval, horizon, train_last_ms, artifact in rows:
        model = LinearProbabilityModel.from_artifact(artifact)
        bars = await paper.bars(market, symbol, interval, 200)
        now_ms = int(time.time() * 1000)
        usable = [b for b in bars if b.close_ms < now_ms]
        if len(usable) < 60:
            continue

        current_bars = usable[-50:]
        ref_bars = [b for b in usable if b.close_ms <= train_last_ms]
        if len(ref_bars) < 20:
            ref_bars = usable[:50]

        context = None
        try:
            async with repo.db.pool.connection() as conn:
                bt_row = await (await conn.execute('''SELECT payload FROM market_latest
                    WHERE market=%s AND symbol=%s AND event_type='book_ticker' ''', (market, symbol))).fetchone()
                mp_row = await (await conn.execute('''SELECT payload FROM market_latest
                    WHERE market=%s AND symbol=%s AND event_type='mark_price' ''', (market, symbol))).fetchone()
            context = extract_microstructure_context(bt_row[0] if bt_row else None, mp_row[0] if mp_row else None)
        except Exception:
            pass

        try:
            ref_features = [r.values for r in make_features(ref_bars, feature_names=model.feature_names)]
            cur_features = [r.values for r in make_features(current_bars, context=context, feature_names=model.feature_names)]
        except Exception as exc:
            LOG.warning('Failed to compute drift features for %s %s: %s', market, symbol, exc)
            continue

        if not ref_features or not cur_features:
            continue

        async with repo.db.pool.connection() as conn:
            pred_rows = await (await conn.execute('''
                SELECT p_up, resolved_net_return FROM shadow_predictions
                WHERE model_id=%s AND resolved_at IS NOT NULL
                ORDER BY bar_open_ms DESC LIMIT 50
            ''', (model_id,))).fetchall()

        if pred_rows:
            errors = [abs(p - int(r > 0)) for p, r in reversed(pred_rows)]
        else:
            errors = [0.15] * len(cur_features)

        is_drift, reasons = evaluate_model_drift(
            ref_features, cur_features, errors, model.feature_names,
            ks_threshold_features=2, ks_alpha=0.01, ph_threshold=5.0
        )

        if is_drift:
            LOG.critical('Concept drift detected for model %s (%s %s): %s', model_id, market, symbol, reasons)
            await repo.mark_model_decaying(model_id, reasons)
            await repo.retire_canary_drift(model_id, reasons)

            alert_msg = f"CRITICAL: Concept Drift detected for {symbol}. Retiring model to flat cash to prevent regime decay losses."
            try:
                await send_alert(
                    title="CONCEPT_DRIFT_INTERVENTION",
                    message=alert_msg,
                    level="CRITICAL",
                    metadata={"model_id": str(model_id), "symbol": symbol, "market": market, "reasons": reasons}
                )
            except Exception as e:
                LOG.warning('Webhook alert dispatch failed: %s', e)

            try:
                job_id = await repo.trigger_retraining_job(market, symbol, interval)
                LOG.info('Automated retraining job triggered: %s', job_id)
            except Exception as e:
                LOG.warning('Retraining trigger failed: %s', e)

            drifted_models.append(str(model_id))

    return drifted_models


async def main():
    logging.basicConfig(level=logging.INFO)
    db=Storage(Settings.load().database_url)
    await db.open()
    repo=ResearchRepo(db)
    paper=PaperRepository(db)
    last_drift_check = 0.0
    try:
        async with db.pool.connection() as conn:
            # No automatic retry of interrupted training: operator must submit a new job.
            await conn.execute('''UPDATE research_jobs SET state='FAILED',
              error_code='WORKER_INTERRUPTED',finished_at=now() WHERE state='RUNNING' ''')
        while True:
            await repo.schedule_campaigns()
            job=await repo.claim()
            if job:
                await run_job(repo,paper,job)
                await OperationsRepository(db).heartbeat('research-worker')
            else:
                try:
                    await observe_shadow(repo,paper)
                    now_t = time.time()
                    if now_t - last_drift_check >= 300:
                        await check_concept_drift(repo, paper)
                        last_drift_check = now_t
                    await OperationsRepository(db).heartbeat('research-worker')
                except Exception:
                    LOG.exception('Shadow observation failed; continuing without trading')
                await asyncio.sleep(15)
    finally:
        await db.close()


if __name__=='__main__':
    asyncio.run(main())

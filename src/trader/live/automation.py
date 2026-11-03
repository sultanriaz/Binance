"""P30 spot automation with dynamic alpha model veto gate and Kelly sizing."""
from __future__ import annotations
from decimal import Decimal
from uuid import uuid4
from trader.paper.strategy import sma_signal
from trader.exchange.policy import D
from trader.governance.core import digest


def calculate_kelly_size(
    prob: float,
    payoff_ratio: float,
    max_order_usdt: Decimal,
    step_size: Decimal,
    min_notional: Decimal = Decimal('5.0'),
    price: Decimal | None = None
) -> Decimal:
    """Fractional Kelly position sizer with Conservative Quarter-Kelly scaling.

    - Full Kelly: f* = (p * b - (1 - p)) / b, with b = target_return / stop_distance
    - Conservative Quarter-Kelly: f_safe = 0.25 * f*
    - Strictly clamped between exchange minNotional and configured max_order_usdt canary cap
    - Quantized down to lot stepSize
    """
    if prob <= 0.0 or payoff_ratio <= 0.0 or max_order_usdt <= Decimal('0'):
        return Decimal('0')

    f_star = (prob * payoff_ratio - (1.0 - prob)) / payoff_ratio
    if f_star <= 0.0:
        return Decimal('0')

    f_safe = Decimal(str(round(0.25 * f_star, 8)))
    target_notional = max_order_usdt * f_safe
    clamped_notional = max(min_notional, min(max_order_usdt, target_notional))

    if price is not None and price > Decimal('0'):
        raw_qty = clamped_notional / price
        if step_size > Decimal('0'):
            qty = (raw_qty // step_size) * step_size
        else:
            qty = raw_qty
        # Safety invariant: notional must never exceed max_order_usdt
        if qty * price > max_order_usdt and step_size > Decimal('0'):
            qty = max(Decimal('0'), qty - step_size)
        return qty
    else:
        if step_size > Decimal('0'):
            return (clamped_notional // step_size) * step_size
        return clamped_notional


def decide(closes: list[Decimal], fast: int, slow: int, held: Decimal) -> str | None:
    if len(closes) < slow:
        return None
    signal = sma_signal(closes, fast, slow)
    if signal == 'LONG' and held <= 0:
        return 'BUY'
    if signal == 'FLAT' and held > 0:
        return 'SELL'
    return None


async def maybe_schedule_spot(repo, settings):
    """Worker-only, read-only research input with transactional cursor/order creation."""
    from psycopg.types.json import Jsonb
    if not settings.configured('spot-live'):
        return 'disabled'
    async with repo.db.pool.connection() as conn:
        async with conn.transaction():
            account = await (await conn.execute('''SELECT state,armed,automation_enabled,snapshot FROM live_accounts
                  WHERE code='spot-live' FOR UPDATE''')).fetchone()
            if not account or account[:3] != ('READY', True, True):
                return 'disabled'
            await repo.readiness(conn)
            conf = await (await conn.execute('''SELECT symbol,quantity,fast_window,slow_window,last_candle,
                config_sha FROM live_automation WHERE account_code='spot-live' FOR UPDATE''')).fetchone()
            if not conf:
                return 'no_config'
            symbol, quantity, fast, slow, last, config_sha = conf
            if symbol not in settings.symbols:
                return 'invalid_symbol'

            # Query enough bars to find a contiguous tail even if older historical gaps exist
            lookback = max((slow + 5) * 3, 100)
            bars_raw = await (await conn.execute('''SELECT open_ms,close_ms,o,h,l,c,volume FROM historical_bars
                 WHERE market='spot' AND symbol=%s AND interval='1m'
                 ORDER BY open_ms DESC LIMIT %s''', (symbol, lookback))).fetchall()

            # Only closed bars, with a delay to avoid in-flight bar revisions.
            import time
            now_cutoff = int(time.time() * 1000) - 2000
            bars = sorted([r for r in bars_raw if int(r[1]) < now_cutoff], key=lambda x: int(x[0]))
            if len(bars) < slow + 1:
                return 'insufficient_candles'

            # Find contiguous tail of strictly consecutive 1m bars ending at the latest closed bar
            start_idx = len(bars) - 1
            while start_idx > 0 and int(bars[start_idx][0]) - int(bars[start_idx - 1][0]) == 60000:
                start_idx -= 1
            contiguous = bars[start_idx:]
            if len(contiguous) < slow + 1:
                return 'candle_gap'

            ms = int(contiguous[-1][0])
            if ms <= last:
                return 'already_processed'

            balances = {x['asset']: D(x['free']) for x in account[3]['account'].get('balances', [])}
            # Resolve the configured base asset and filters from exchange's validated instruments metadata.
            instrument = await (await conn.execute('''SELECT base_asset, filters FROM instruments
               WHERE market='spot' AND symbol=%s AND status='TRADING' ''', (symbol,))).fetchone()
            if not instrument:
                return 'missing_instrument'
            base_asset, filters = instrument
            held = balances.get(base_asset, Decimal(0))
            filters_dict = {f['filterType']: f for f in (filters or []) if isinstance(f, dict)}
            lot = filters_dict.get('MARKET_LOT_SIZE') or filters_dict.get('LOT_SIZE')
            step = D(lot['stepSize']) if lot and lot.get('stepSize') else Decimal('0.0001')
            min_notional_f = filters_dict.get('MIN_NOTIONAL') or filters_dict.get('NOTIONAL')
            min_notional = D(min_notional_f['minNotional']) if min_notional_f and 'minNotional' in min_notional_f else Decimal('5.0')
            current_price = D(contiguous[-1][5])

            # Execution Interlocking: If model status is DECAYING, pause entries and revert to cash
            decaying = await (await conn.execute('''SELECT id FROM research_models
                WHERE market='spot' AND symbol=%s AND status='DECAYING'
                LIMIT 1''', (symbol,))).fetchone()
            if decaying is not None:
                if held > 0:
                    qty = held
                    if step > 0:
                        qty = (qty // step) * step
                    if qty > 0:
                        payload = {'symbol': symbol, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty),
                                   'reduce_only': False, 'reason': 'DRIFT_REVERT_TO_CASH'}
                        id_ = str(uuid4())
                        client = f'a{uuid4().hex[:25]}'
                        await conn.execute('''INSERT INTO live_order_intents(id,account_code,client_order_id,
                          payload,payload_sha,source) VALUES(%s,'spot-live',%s,%s,%s,'AUTOMATION')''',
                          (id_, client, Jsonb(payload), digest(payload)))
                        await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'QUEUED')", (id_,))
                        return 'SELL'
                return 'paused_by_drift_decay'

            side = decide([D(c[5]) for c in contiguous[-slow:]], fast, slow, held)
            await conn.execute("UPDATE live_automation SET last_candle=%s WHERE account_code='spot-live'", (ms,))
            if side is None:
                return 'hold'
            if await repo.has_unresolved(conn, 'spot-live'):
                return 'open_order_exists'
            today = await (await conn.execute('''SELECT COUNT(*) FROM live_order_intents
              WHERE account_code='spot-live' AND created_at>=date_trunc('day',now())
              AND source!='PROTECTIVE' AND status NOT IN ('QUEUED','FAILED_POLICY')''')).fetchone()
            if today[0] >= settings.max_daily_orders:
                return 'daily_limit'

            ai_attribution = None

            if side == 'BUY':
                # Dynamic Alpha Gate: Check for active CANARY_ACTIVE model for this symbol
                canary = await (await conn.execute('''SELECT id, artifact, horizon FROM research_models
                    WHERE market='spot' AND symbol=%s AND status='CANARY_ACTIVE'
                    ORDER BY promoted_at DESC LIMIT 1''', (symbol,))).fetchone()

                if canary is not None:
                    model_id, artifact, horizon = canary
                    from trader.intelligence.model import LinearProbabilityModel
                    from trader.intelligence.features import extract_microstructure_context, make_features
                    from trader.paper.history import Bar

                    model = LinearProbabilityModel.from_artifact(artifact)
                    depth_row = await (await conn.execute('''SELECT payload FROM market_latest
                        WHERE market='spot' AND symbol=%s AND event_type='depth20' ''', (symbol,))).fetchone()
                    bt_row = await (await conn.execute('''SELECT payload FROM market_latest
                        WHERE market='spot' AND symbol=%s AND event_type='book_ticker' ''', (symbol,))).fetchone()
                    mp_row = await (await conn.execute('''SELECT payload FROM market_latest
                        WHERE market=%s AND symbol=%s AND event_type='mark_price' ''', (symbol,))).fetchone()
                    ctx = extract_microstructure_context(
                        book_ticker_payload=bt_row[0] if bt_row else None,
                        mark_price_payload=mp_row[0] if mp_row else None,
                        depth_payload=depth_row[0] if depth_row else None
                    )

                    bars_obj = [
                        Bar('spot', symbol, '1m', int(r[0]), int(r[1]), D(r[2]), D(r[3]), D(r[4]), D(r[5]), D(r[6]))
                        for r in contiguous
                    ]
                    feat_rows = make_features(bars_obj, context=ctx, feature_names=model.feature_names)
                    latest_row = feat_rows[-1]
                    p_up = model.probability(latest_row)

                    # Directional Filter Veto: If BUY signal but P(Up) < 0.60, VETO order
                    if p_up < 0.60:
                        return 'vetoed_by_alpha_model'

                    # Fractional Kelly position sizing clamped to canary cap
                    qty = calculate_kelly_size(
                        prob=p_up,
                        payoff_ratio=1.5,
                        max_order_usdt=settings.max_order_usdt,
                        step_size=step,
                        min_notional=min_notional,
                        price=current_price
                    )
                    if qty <= Decimal('0'):
                        return 'vetoed_by_alpha_model'

                    # Top 3 linear feature contributions
                    contributions = sorted(
                        ((name, weight * (v - mean) / scale)
                         for name, weight, v, mean, scale in zip(model.feature_names, model.weights, latest_row.values, model.means, model.scales)),
                        key=lambda x: abs(x[1]),
                        reverse=True
                    )[:3]
                    top3 = [{'feature': k, 'contribution': round(float(v), 6)} for k, v in contributions]
                    ai_attribution = {
                        'model_id': str(model_id),
                        'forecast_p': round(float(p_up), 6),
                        'top_contributions': top3,
                    }
                else:
                    # Clean deterministic fallback to baseline SMA quantity
                    qty = D(quantity)
                    if step > 0:
                        qty = (qty // step) * step
            else:
                # SELL: Close held position
                qty = min(D(quantity), held)
                if step > 0:
                    qty = (qty // step) * step

            if qty <= 0:
                return 'nothing_to_sell'

            proposed_notional = qty * current_price
            if proposed_notional > settings.max_order_usdt:
                return 'configured_quantity_exceeds_cap'

            payload = {
                'symbol': symbol,
                'side': side,
                'type': 'MARKET',
                'quantity': str(qty),
                'reduce_only': False
            }
            if ai_attribution is not None:
                payload['ai_attribution'] = ai_attribution

            id_ = str(uuid4())
            client = f'a{uuid4().hex[:25]}'
            await conn.execute('''INSERT INTO live_order_intents(id,account_code,client_order_id,
              payload,payload_sha,source) VALUES(%s,'spot-live',%s,%s,%s,'AUTOMATION')''',
              (id_, client, Jsonb(payload), digest(payload)))
            await conn.execute("INSERT INTO live_order_events(intent_id,status) VALUES(%s,'QUEUED')", (id_,))
            return side

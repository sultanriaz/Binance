"""Pure safety predicates. The live engine checks these against a fresh exchange snapshot."""
from __future__ import annotations
from decimal import Decimal
from trader.exchange.policy import D, PolicyError, validate_order


def validate_live_order(code: str, payload: dict, info: dict, quote: dict, snapshot: dict,
                        settings, *, open_incidents: int = 0, funding_info: dict | None = None) -> dict:
    if code not in ('spot-live','futures-live') or not settings.configured(code):
        raise PolicyError('LIVE_ACCOUNT_UNAVAILABLE')
    if open_incidents:
        raise PolicyError('OPEN_LIVE_INCIDENTS')
    if payload.get('type') not in ('MARKET','LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET'):
        raise PolicyError('UNSUPPORTED_LIVE_ORDER')
    is_protect = payload.get('type') in ('STOP_MARKET','TAKE_PROFIT_MARKET')
    if code=='futures-live':
        if is_protect and not payload.get('reduce_only'):
            raise PolicyError('FUTURES_STOP_MUST_REDUCE')
        if not payload.get('reduce_only'):
            if payload.get('type')!='MARKET' or not payload.get('protective_stop'):
                raise PolicyError('FUTURES_ENTRY_REQUIRES_PROTECTIVE_STOP')
        if is_protect and payload.get('protective_stop'):
            raise PolicyError('PROTECTION_RECURSION_FORBIDDEN')
        if not payload.get('reduce_only'):
            positions=[p for p in snapshot['positions'] if D(p.get('positionAmt','0')) != 0]
            if positions:
                raise PolicyError('NO_NEW_FUTURES_EXPOSURE_WITH_EXISTING_POSITION')
            stop=D(payload['protective_stop'])
            bid,ask=D(quote['bidPrice']),D(quote['askPrice'])
            mid=(bid+ask)/2
            distance= (mid-stop)/mid if payload['side']=='BUY' else (stop-mid)/mid
            if distance < Decimal('.001') or distance > Decimal('.03'):
                raise PolicyError('PROTECTIVE_STOP_DISTANCE_MUST_BE_0_1_TO_3_PERCENT')
            if payload.get('take_profit'):
                tp=D(payload['take_profit'])
                tp_dist= (tp-mid)/mid if payload['side']=='BUY' else (mid-tp)/mid
                if tp_dist < Decimal('.001') or tp_dist > Decimal('.20'):
                    raise PolicyError('TAKE_PROFIT_DISTANCE_MUST_BE_0_1_TO_20_PERCENT')
            if funding_info:
                check_funding_rate_drift(payload['symbol'], payload['side'], funding_info)
    if code=='spot-live' and (payload.get('reduce_only') or is_protect):
        raise PolicyError('INVALID_LIVE_SPOT_PAYLOAD')
    # The inherited policy only distinguishes spot-testnet vs futures-demo. Pass a
    # canonical market identity; no sandbox credentials or hostnames are involved.
    safety_code='spot-testnet' if code=='spot-live' else 'futures-demo'
    checked=validate_order(safety_code, payload, info, quote, snapshot,
                           symbols=settings.symbols,max_notional=settings.max_order_usdt,
                           max_leverage=settings.max_leverage)
    return {**checked,'exchange':code}


def check_funding_rate_drift(symbol: str, side: str, funding_info: dict | None,
                             max_adverse_rate: Decimal = Decimal('0.0005')) -> None:
    """Pre-trade funding check: reject opening positions into severe adverse funding rates."""
    if not funding_info:
        return
    rate_str = funding_info.get('lastFundingRate')
    if not rate_str:
        return
    rate = D(rate_str)
    if side == 'BUY' and rate > max_adverse_rate:
        raise PolicyError(f'ADVERSE_FUNDING_RATE_TOO_HIGH: {rate} > {max_adverse_rate}')
    if side == 'SELL' and rate < -max_adverse_rate:
        raise PolicyError(f'ADVERSE_FUNDING_RATE_TOO_HIGH: {rate} < -{max_adverse_rate}')


def check_liquidation_proximity(pos: dict, min_safe_distance: Decimal = Decimal('0.03')) -> bool:
    """Liquidation watchdog check: returns True if mark price is dangerously close to liquidation price."""
    amt = D(pos.get('positionAmt', '0'))
    if amt == 0:
        return False
    liq_price_str = pos.get('liquidationPrice')
    mark_price_str = pos.get('markPrice')
    if not liq_price_str or not mark_price_str:
        return False
    liq = D(liq_price_str)
    mark = D(mark_price_str)
    if liq <= 0 or mark <= 0:
        return False
    dist = (mark - liq) / mark if amt > 0 else (liq - mark) / mark
    return dist < min_safe_distance


def check_daily_drawdown(equity, baseline, max_loss_pct) -> None:
    equity,baseline=D(equity),D(baseline)
    if baseline<=0 or equity<=0:
        raise PolicyError('INVALID_ACCOUNT_EQUITY')
    if equity < baseline*(Decimal('1') - D(max_loss_pct)/100):
        raise PolicyError('DAILY_DRAWDOWN_LIMIT')


def account_equity(code, snapshot):
    account=snapshot['account']
    if code=='spot-live':
        # No reliable USDT-denominated equity without marking all holdings.
        # Expose spot balance but do NOT misrepresent it as aggregate equity.
        return None
    return D(account['totalMarginBalance'])


def check_bnb_fee_buffer(balances: list[dict], min_bnb: Decimal = Decimal('0.05')) -> dict:
    """Check if account holds enough BNB to pay trading fees with 25% discount."""
    bnb_balance = Decimal('0')
    for b in balances:
        if b.get('asset') == 'BNB':
            bnb_balance = D(b.get('free', '0'))
            break
    sufficient = bnb_balance >= min_bnb
    return {
        'bnb_free': str(bnb_balance),
        'min_recommended': str(min_bnb),
        'sufficient_fee_buffer': sufficient,
        'discount_active': sufficient,
        'warning': None if sufficient else f'BNB balance {bnb_balance} < {min_bnb}. Fees will be deducted in base/quote asset, creating fractional dust.'
    }


async def verify_api_restrictions(adapter, code: str) -> dict:
    """Production key permissions enforcement via Binance GET /sapi/v1/account/apiRestrictions.
    Refuses live trading if withdrawals are enabled or IP restrictions are absent.
    """
    restrictions = await adapter.get_api_restrictions()
    if not isinstance(restrictions, dict):
        raise RuntimeError("Failed to retrieve authoritative Binance API restrictions")

    if restrictions.get('enableWithdrawals') is True:
        raise RuntimeError("CRITICAL_SECURITY_BREACH: Binance API key has WITHDRAWALS ENABLED! Trading keys must NEVER have withdrawal permissions.")

    if restrictions.get('ipRestrict') is not True:
        raise RuntimeError("CRITICAL_SECURITY_BREACH: Binance API key is NOT IP-restricted! Live trading requires IP restriction.")

    if code == 'spot-live' and not restrictions.get('enableSpotAndMarginTrading', False):
        raise RuntimeError("Binance API key lacks spot trading permissions (enableSpotAndMarginTrading is False).")

    if code == 'futures-live' and not restrictions.get('enableFutures', False):
        raise RuntimeError("Binance API key lacks futures trading permissions (enableFutures is False).")

    return restrictions


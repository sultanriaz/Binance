"""Pure safety predicates. The live engine checks these against a fresh exchange snapshot."""
from __future__ import annotations
from decimal import Decimal
from trader.exchange.policy import D, PolicyError, validate_order


def validate_live_order(code: str, payload: dict, info: dict, quote: dict, snapshot: dict,
                        settings, *, open_incidents: int = 0) -> dict:
    if code not in ('spot-live','futures-live') or not settings.configured(code):
        raise PolicyError('LIVE_ACCOUNT_UNAVAILABLE')
    if open_incidents:
        raise PolicyError('OPEN_LIVE_INCIDENTS')
    if payload.get('type') not in ('MARKET','LIMIT','STOP_MARKET'):
        raise PolicyError('UNSUPPORTED_LIVE_ORDER')
    is_protect = payload.get('type')=='STOP_MARKET'
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
    if code=='spot-live' and (payload.get('reduce_only') or is_protect):
        raise PolicyError('INVALID_LIVE_SPOT_PAYLOAD')
    # The inherited policy only distinguishes spot-testnet vs futures-demo. Pass a
    # canonical market identity; no sandbox credentials or hostnames are involved.
    safety_code='spot-testnet' if code=='spot-live' else 'futures-demo'
    checked=validate_order(safety_code, payload, info, quote, snapshot,
                           symbols=settings.symbols,max_notional=settings.max_order_usdt,
                           max_leverage=settings.max_leverage)
    return {**checked,'exchange':code}


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

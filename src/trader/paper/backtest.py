"""P14 backtesting: signals at candle close, orders at NEXT candle open.

OHLC fills do not model intrabar queue priority/liquidation precisely. Use for
baseline research, not a claim that the strategy will be profitable live.
"""
from __future__ import annotations

import math
from decimal import Decimal
from trader.paper.domain import Account, Policy, Quote, ZERO, ONE, dec, ds
from trader.paper.engine import place_order, process_bar
from trader.paper.history import Bar, INTERVAL_MS, validate
from trader.paper.risk import RiskRejected
from trader.paper.strategy import sma_signal, desired_order


def simulate(bars: list[Bar], *, starting_cash: Decimal = Decimal('10000'),
             fast: int = 10, slow: int = 30, allocation: Decimal = Decimal('.10'),
             policy: Policy | None = None, account_id: str = 'BACKTEST',
             leverage: int = 1) -> dict:
    validate(bars)
    if len(bars) < slow+2:
        raise ValueError('dataset too short for warmup and next-bar execution')
    if len(bars) > 50000:
        raise ValueError('maximum 50,000 bars per backtest')
    if leverage != 1:  # futures leverage research is intentionally conservative here
        raise ValueError('baseline strategy uses 1x futures; experiment with leverage separately')
    policy = policy or Policy()
    a = Account.create(account_id, starting_cash)
    closes: list[Decimal] = []
    pending: tuple[str, Decimal, bool] | None = None
    equity_curve: list[dict] = []
    rejections: list[dict] = []
    for i, bar in enumerate(bars):
        # PREVIOUS candle's decision executes on current candle's open.
        if pending is not None:
            side, qty, reduce_only = pending
            spread = Decimal('.0002')
            q = Quote(bar.market, bar.symbol,
                      bar.o * (ONE - spread),bar.o * (ONE + spread),
                      bar.volume/2,bar.volume/2,bar.open_ms,True)
            try:
                place_order(a,q,policy,side=side,quantity=qty,ts_ms=bar.open_ms,
                            order_type='MARKET',reduce_only=reduce_only,
                            client_id=f'backtest:{account_id}:{bar.open_ms}')
            except RiskRejected as exc:
                rejections.append({'open_ms':bar.open_ms, 'reason':exc.reason})
        pending = None
        process_bar(a,market=bar.market,symbol=bar.symbol,open_price=bar.o,
                    high=bar.h,low=bar.l,close=bar.c,volume=bar.volume,
                    ts_ms=bar.open_ms,policy=policy)
        equity_curve.append({'open_ms':bar.open_ms,'equity':ds(a.equity())})
        closes.append(bar.c)
        signal=sma_signal(closes,fast,slow)
        # This signal is not eligible until the NEXT bar.
        if i < len(bars)-1:
            pending=desired_order(a,market=bar.market,symbol=bar.symbol,
                                  desired=signal,price=bar.c,allocation=allocation)
    initial=starting_cash
    ending=a.equity()
    peak=initial
    max_dd=ZERO
    returns=[]
    previous=initial
    for point in equity_curve:
        current=dec(point['equity'])
        if current > peak:
            peak=current
        if peak:
            max_dd=max(max_dd,(peak-current)/peak)
        if previous > ZERO:
            returns.append(float(current/previous-ONE))
        previous=current
    vol = math.sqrt(sum((x-sum(returns)/len(returns))**2 for x in returns)/(len(returns)-1)) if len(returns)>1 else 0.0
    annual_bars=365*86400000/INTERVAL_MS[bars[0].interval]
    sharpe=((sum(returns)/len(returns))/vol*math.sqrt(annual_bars)) if vol>1e-12 else None
    fills=[e for e in a.ledger if e['type']=='FILL']
    # Gross realized for closures only (open fills do not count as profits).
    realized=dec(a.realized_pnl)
    result={'market':bars[0].market,'symbol':bars[0].symbol,'interval':bars[0].interval,
            'bar_count':len(bars),'first_open_ms':bars[0].open_ms,'last_open_ms':bars[-1].open_ms,
            'start_equity':ds(initial),'end_equity':ds(ending),
            'total_return_fraction':ds((ending-initial)/initial),
            'max_drawdown_fraction':ds(max_dd),'sharpe_approx':sharpe,
            'fee_total':ds(a.fees),'realized_pnl_gross':ds(realized),
            'fills':len(fills),'rejected_orders':rejections,'equity_curve':equity_curve,
            'final_state':a.to_dict(),
            'assumptions':['signals at close and market execution next open',
                           'fixed bid-ask spread and slippage bps',
                           'candle volume used as approximate top-of-book liquidity',
                           'no intrabar liquidation/stop model or actual order queue',
                           'funding not applied without historical funding-rate dataset',
                           'fees charged on every simulated fill',
                           'partial market order remainder cancelled (IOC)']}
    return result

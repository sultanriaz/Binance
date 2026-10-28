import asyncio
from decimal import Decimal as D
import httpx
import pytest

from trader.paper.history import Bar, download, load_jsonl, save_jsonl, validate
from trader.paper.backtest import simulate


def bars(n=80):
    out=[]
    for i in range(n):
        price=D('100') + D(i % 25) / D('2')
        out.append(Bar.parse(dict(market='spot',symbol='BTCUSDT',interval='1m',
            open_ms=i*60000,close_ms=i*60000+59999,o=str(price),h=str(price+2),
            l=str(price-2),c=str(price),volume='200')))
    return out


def test_manifest_roundtrip_and_integrity(tmp_path):
    path=tmp_path/'demo.jsonl'
    saved=save_jsonl(path,bars(),source='unit_test')
    loaded,manifest=load_jsonl(path)
    assert len(loaded)==80
    assert manifest['sha256']==saved['sha256']
    path.write_bytes(path.read_bytes()+b' \n')
    with pytest.raises(ValueError,match='checksum'):
        load_jsonl(path)


def test_missing_or_duplicate_candle_rejected():
    b=bars()
    with pytest.raises(ValueError,match='missing candle'):
        validate(b[:10]+b[11:])
    with pytest.raises(ValueError,match='duplicate'):
        validate(b[:10]+[b[9]]+b[10:])


def test_lookahead_safe_and_reproducible_backtest():
    a=simulate(bars(),fast=5,slow=10)
    b=simulate(bars(),fast=5,slow=10)
    assert a==b
    assert a['bar_count']==80
    assert a['start_equity']=='10000'
    assert 'max_drawdown_fraction' in a
    assert all(e['ts_ms'] >= 10*60000 for e in a['final_state']['ledger'] if e['type']=='FILL')
    assert all(e['type']!='SIMULATED_LIQUIDATION' for e in a['final_state']['ledger'])


def test_download_paginates_using_strict_continuity():
    now=1_800_000_000_000
    requests=[]
    def handler(req):
        requests.append(req)
        start=int(req.url.params['startTime'])
        limit=int(req.url.params['limit'])
        end=int(req.url.params['endTime'])
        rows=[]
        for ts in range(start,end+1,60000):
            rows.append([ts,'100','101','99','100','1',ts+59999])
        return httpx.Response(200,json=rows)
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async def action():
        return await download('spot','BTCUSDT','1m',0,1001*60000,
                              client=client)
    result=asyncio.run(action())
    asyncio.run(client.aclose())
    assert len(result)==1001
    assert len(requests)==2


def test_untrustworthy_ohlc_rejected():
    with pytest.raises(ValueError,match='invalid candle'):
        Bar.parse(dict(market='spot',symbol='BTCUSDT',interval='1m',
                       open_ms=0,close_ms=59999,o='100',h='90',l='80',c='100',volume='1'))


def test_paper_and_public_market_modules_remain_exchange_write_free():
    import pathlib
    sources = [*pathlib.Path('src/trader/paper').rglob('*.py'),
               pathlib.Path('src/trader/api.py'),
               pathlib.Path('src/trader/binance_rest.py'),
               pathlib.Path('src/trader/collector.py')]
    for file in sources:
        txt=file.read_text()
        assert "'/api/v3/order'" not in txt
        assert "'/fapi/v1/order'" not in txt
    # Exchange module owns every signed order write, and pins ONLY demo/testnet hosts.
    from trader.exchange.settings import HOSTS
    assert len(HOSTS) == 2
    assert 'api.binance.com' not in [url for url, _ in HOSTS.values()]

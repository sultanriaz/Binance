"""Account/order REST facades for Binance's segregated test environments.

All methods call a transport pinned to a test environment. Neither gateway nor AI
can provide a URL; the worker alone holds credentials.
"""
from __future__ import annotations

from trader.exchange.transport import SandboxTransport


class SandboxAdapter:
    def __init__(self, transport: SandboxTransport):
        self.t = transport
        self.code = transport.code
        self.market = 'spot' if self.code == 'spot-testnet' else 'futures'
        self.prefix = '/api/v3' if self.market == 'spot' else '/fapi/v1'

    async def instruments(self):
        return await self.t.request('GET', self.prefix + '/exchangeInfo')

    async def quote(self, symbol):
        return await self.t.request('GET', self.prefix + '/ticker/bookTicker', {'symbol': symbol})

    async def snapshot(self):
        if self.market == 'spot':
            account = await self.t.request('GET', '/api/v3/account', signed=True)
            orders = await self.t.request('GET', '/api/v3/openOrders', signed=True)
            return {'account': account, 'orders': orders, 'positions': [], 'algos': [], 'position_mode': None}
        account = await self.t.request('GET', '/fapi/v3/account', signed=True)
        orders = await self.t.request('GET', '/fapi/v1/openOrders', signed=True)
        algos = await self.t.request('GET', '/fapi/v1/openAlgoOrders', signed=True)
        positions = await self.t.request('GET', '/fapi/v3/positionRisk', signed=True)
        mode = await self.t.request('GET', '/fapi/v1/positionSide/dual', signed=True)
        return {'account': account, 'orders': orders, 'algos': algos,
                'positions': positions, 'position_mode': mode}

    def is_algo(self, payload: dict) -> bool:
        return self.market == 'futures' and payload['type'] in ('STOP_MARKET', 'TAKE_PROFIT_MARKET')

    async def submit(self, payload: dict, client_id: str):
        values = {'symbol': payload['symbol'], 'side': payload['side'],
                  'type': payload['type'], 'quantity': payload['quantity']}
        if payload['type'] == 'LIMIT':
            values.update(price=payload['price'], timeInForce='GTC')
        if self.is_algo(payload):
            values.update(algoType='CONDITIONAL', triggerPrice=payload['trigger_price'],
                          workingType='MARK_PRICE', clientAlgoId=client_id, reduceOnly='true')
            return await self.t.request('POST', '/fapi/v1/algoOrder', values, signed=True)
        if self.market == 'spot':
            if payload['type'] in ('STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT'):
                values.update(price=payload['price'], stopPrice=payload['trigger_price'], timeInForce='GTC')
            values['newClientOrderId'] = client_id
            return await self.t.request('POST', '/api/v3/order', values, signed=True)
        values['newClientOrderId'] = client_id
        values['positionSide'] = 'BOTH'
        if payload.get('reduce_only'):
            values['reduceOnly'] = 'true'
        return await self.t.request('POST', '/fapi/v1/order', values, signed=True)

    async def query(self, symbol: str, client_id: str, *, algo=False):
        if self.market == 'spot':
            return await self.t.request('GET', '/api/v3/order',
                                        {'symbol': symbol, 'origClientOrderId': client_id}, signed=True)
        if algo:
            return await self.t.request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client_id}, signed=True)
        return await self.t.request('GET', '/fapi/v1/order',
                                    {'symbol': symbol, 'origClientOrderId': client_id}, signed=True)

    async def query_regular_by_id(self, symbol: str, order_id: int):
        if self.market != 'futures':
            raise ValueError('Futures regular order lookup only')
        return await self.t.request('GET','/fapi/v1/order',
                                    {'symbol':symbol,'orderId':int(order_id)}, signed=True)

    async def cancel(self, symbol: str, client_id: str, *, algo=False):
        if self.market == 'spot':
            return await self.t.request('DELETE', '/api/v3/order',
                                        {'symbol': symbol, 'origClientOrderId': client_id}, signed=True)
        if algo:
            return await self.t.request('DELETE', '/fapi/v1/algoOrder',
                                        {'clientAlgoId': client_id}, signed=True)
        return await self.t.request('DELETE', '/fapi/v1/order',
                                    {'symbol': symbol, 'origClientOrderId': client_id}, signed=True)

    async def trades(self, symbol: str, from_id: int | None = None):
        params = {'symbol': symbol, 'limit': 500}
        if from_id is not None:
            params['fromId'] = from_id
        return await self.t.request('GET', '/api/v3/myTrades' if self.market == 'spot'
                                    else '/fapi/v1/userTrades', params, signed=True)

    async def start_futures_stream(self):
        if self.market != 'futures':
            raise ValueError('Futures user stream only')
        return (await self.t.request('POST', '/fapi/v1/listenKey', key_only=True))['listenKey']

    async def keepalive_futures_stream(self, listen_key: str):
        return await self.t.request('PUT', '/fapi/v1/listenKey', {'listenKey':listen_key}, key_only=True)

    async def set_one_way_mode(self):
        if self.market != 'futures':
            return {'code': 200, 'msg': 'Spot market; one-way mode not applicable'}
        from trader.exchange.transport import ExchangeError
        try:
            return await self.t.request('POST', '/fapi/v1/positionSide/dual', {'dualSidePosition': 'false'}, signed=True)
        except ExchangeError as exc:
            if exc.code == -4059 or 'No need to change' in str(exc):
                return {'code': 200, 'msg': 'Already in one-way mode'}
            raise

    async def set_isolated_margin(self, symbol: str):
        if self.market != 'futures':
            return {'code': 200, 'msg': 'Spot market; margin type not applicable'}
        from trader.exchange.transport import ExchangeError
        try:
            return await self.t.request('POST', '/fapi/v1/marginType', {'symbol': symbol, 'marginType': 'ISOLATED'}, signed=True)
        except ExchangeError as exc:
            if exc.code == -4046 or 'No need to change' in str(exc):
                return {'code': 200, 'msg': f'{symbol} already isolated'}
            raise

    async def set_leverage(self, symbol: str, leverage: int):
        if self.market != 'futures':
            return {'code': 200, 'msg': 'Spot market; leverage not applicable'}
        return await self.t.request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage}, signed=True)

    async def leverage_brackets(self, symbol: str | None = None):
        if self.market != 'futures':
            return []
        params = {'symbol': symbol} if symbol else {}
        return await self.t.request('GET', '/fapi/v1/leverageBracket', params, signed=True)

    async def funding_rate(self, symbol: str):
        if self.market != 'futures':
            return None
        return await self.t.request('GET', '/fapi/v1/premiumIndex', {'symbol': symbol})

    async def submit_spot_oco(self, payload: dict, client_id: str):
        if self.market != 'spot':
            raise ValueError('OCO orders are supported on spot only')
        values = {
            'symbol': payload['symbol'],
            'side': payload['side'],
            'quantity': payload['quantity'],
            'price': payload['price'],
            'stopPrice': payload['stop_price'],
            'stopLimitPrice': payload.get('stop_limit_price', payload['stop_price']),
            'stopLimitTimeInForce': 'GTC',
            'listClientOrderId': client_id,
        }
        return await self.t.request('POST', '/api/v3/orderList/oco', values, signed=True)

    async def query_dust_assets(self):
        if self.market != 'spot':
            return {'details': []}
        return await self.t.request('GET', '/sapi/v1/asset/dust-btc', signed=True)

    async def convert_dust_to_bnb(self, assets: list[str]):
        if self.market != 'spot' or not assets:
            return {'code': 200, 'msg': 'No assets to convert'}
        return await self.t.request('POST', '/sapi/v1/asset/dust-btc', {'asset': assets}, signed=True)

    async def cancel_all_symbol_orders(self, symbol: str):
        if self.market == 'spot':
            return await self.t.request('DELETE', '/api/v3/openOrders', {'symbol': symbol}, signed=True)
        return await self.t.request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol}, signed=True)

    async def get_api_restrictions(self):
        return await self.t.request('GET', '/sapi/v1/account/apiRestrictions', signed=True)



"""Typed testnet order request without web/runtime dependencies."""
from pydantic import BaseModel, Field, model_validator
from typing import Literal

class ExchangeOrder(BaseModel):
    account: Literal['spot-testnet','futures-demo']
    client_order_id: str = Field(pattern=r'^[a-zA-Z0-9_.:/-]{8,36}$')
    symbol: str = Field(pattern=r'^[A-Z0-9]{5,20}$')
    side: Literal['BUY','SELL']
    type: Literal['MARKET','LIMIT','STOP_LOSS_LIMIT','TAKE_PROFIT_LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET']
    quantity: str
    price: str | None = None
    trigger_price: str | None = None
    reduce_only: bool = False

    @model_validator(mode='after')
    def valid_shape(self):
        from trader.exchange.policy import D
        if D(self.quantity) <= 0:
            raise ValueError('Quantity must be positive')
        if self.price is not None and D(self.price) <= 0:
            raise ValueError('Price must be positive')
        if self.trigger_price is not None and D(self.trigger_price) <= 0:
            raise ValueError('Trigger must be positive')
        if self.account == 'spot-testnet' and self.type not in ('MARKET','LIMIT','STOP_LOSS_LIMIT','TAKE_PROFIT_LIMIT'):
            raise ValueError('Unsupported Spot Testnet order')
        if self.account == 'futures-demo' and self.type not in ('MARKET','LIMIT','STOP_MARKET','TAKE_PROFIT_MARKET'):
            raise ValueError('Unsupported Futures Demo order')
        if self.type == 'LIMIT' and not self.price:
            raise ValueError('LIMIT requires price')
        if self.type in ('STOP_LOSS_LIMIT','TAKE_PROFIT_LIMIT') and not (self.price and self.trigger_price):
            raise ValueError('Spot conditional limit requires price and trigger_price')
        if self.type in ('STOP_MARKET','TAKE_PROFIT_MARKET') and not (self.trigger_price and self.reduce_only):
            raise ValueError('Futures conditional orders require trigger_price and reduce_only')
        return self


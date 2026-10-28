"""Live order contracts; intentionally smaller than the sandbox feature set."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from trader.exchange.policy import D

class LiveOrder(BaseModel):
    account: Literal['spot-live','futures-live']
    client_order_id: str = Field(pattern=r'^[a-zA-Z0-9_.-]{8,30}$')
    symbol: str = Field(pattern=r'^[A-Z0-9]{5,20}$')
    side: Literal['BUY','SELL']
    type: Literal['MARKET','LIMIT'] = 'MARKET'
    quantity: str
    price: str | None = None
    reduce_only: bool = False
    protective_stop: str | None = None

    @model_validator(mode='after')
    def safe_shape(self):
        if D(self.quantity) <= 0:
            raise ValueError('Quantity must be positive')
        if self.price is not None and D(self.price) <= 0:
            raise ValueError('Invalid limit price')
        if self.type=='LIMIT' and not self.price:
            raise ValueError('LIMIT requires price')
        if self.type=='MARKET' and self.price:
            raise ValueError('MARKET must not specify price')
        if self.account=='spot-live':
            if self.reduce_only or self.protective_stop:
                raise ValueError('Spot does not accept reduce_only or futures protection')
        else:
            if self.type!='MARKET':
                raise ValueError('Futures live canary supports only MARKET entries/exits')
            if self.reduce_only and self.protective_stop:
                raise ValueError('Reduce-only exit must not add a new protective order')
            if not self.reduce_only and (not self.protective_stop or D(self.protective_stop)<=0):
                raise ValueError('Mandatory futures protective stop is required for new exposure')
        return self

class AutomationConfig(BaseModel):
    symbol: str = Field(pattern=r'^[A-Z0-9]{5,20}$')
    quantity: str
    fast_window: int = Field(default=8, ge=5, le=20)
    slow_window: int = Field(default=30, ge=21, le=100)
    @model_validator(mode='after')
    def validate_strategy(self):
        if D(self.quantity)<=0 or self.fast_window>=self.slow_window:
            raise ValueError('Invalid SMA configuration')
        return self

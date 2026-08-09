"""Synthetic order flow for the live simulation.

Uninformed noise: uniform limit orders around a base price. There is no drift
and no informed flow, so a maker quoting into this stream faces essentially no
adverse selection — worth remembering before reading anything into maker PnL.
"""

from __future__ import annotations

import random
from typing import List, Tuple

from orderbook import Side
from sim.exchange import Exchange
from sim.market_maker import create_market_maker


class RandomTrader:
    """Submits random limit orders near ``base_price``."""

    def __init__(
        self,
        exchange: Exchange,
        base_price: float = 100.0,
        jitter: float = 1.0,
        rng: random.Random = None,
    ):
        self.exchange = exchange
        self.base_price = base_price
        self.jitter = jitter
        self.rng = rng or random

    def submit_random_limit(self):
        side = Side.BUY if self.rng.random() < 0.5 else Side.SELL
        quantity = self.rng.randint(1, 40)
        price = round(self.base_price + self.rng.uniform(-self.jitter, self.jitter), 2)
        return self.exchange.submit_new(side, quantity, price)


def new_simulation(
    mm_type: str = "simple", quote_size: int = 10
) -> Tuple[Exchange, object, List[RandomTrader]]:
    """Exchange + one maker + three traders staggered around 100."""
    exchange = Exchange()
    maker = create_market_maker(exchange, mm_type=mm_type, quote_size=quote_size)
    traders = [
        RandomTrader(exchange, base_price=99.0, jitter=1.0),
        RandomTrader(exchange, base_price=100.0, jitter=1.0),
        RandomTrader(exchange, base_price=101.0, jitter=1.0),
    ]
    return exchange, maker, traders

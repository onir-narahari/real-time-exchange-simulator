/// Trade output — the engine's product.
///
/// A trade always prints at the *resting* order's price, so an aggressive
/// limit receives price improvement rather than paying its own limit.
/// Mirrors orderbook/trade.py.

#pragma once

#include <cstdint>

#include "orderbook/order.hpp"

namespace orderbook {

struct Trade {
    std::int64_t trade_id;
    double price;                  ///< the resting order's price
    int quantity;
    Side aggressor_side;
    std::int64_t aggressor_order_id;
    std::int64_t resting_order_id;

    double notional() const { return price * quantity; }

    std::int64_t buy_order_id() const {
        return aggressor_side == Side::BUY ? aggressor_order_id : resting_order_id;
    }
    std::int64_t sell_order_id() const {
        return aggressor_side == Side::SELL ? aggressor_order_id : resting_order_id;
    }
};

}  // namespace orderbook

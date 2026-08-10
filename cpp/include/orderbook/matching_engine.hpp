/// Matching engine: turns an incoming order into trades.
///
/// Price-time priority
/// -------------------
/// An aggressor sweeps the opposing side best price first; within a price,
/// orders fill in arrival order (FIFO). Every trade prints at the *resting*
/// order's price, so an aggressive limit receives price improvement rather
/// than paying its own limit.
///
/// Order handling
/// --------------
/// - LIMIT  — sweeps every crossing level, then rests any remainder.
/// - MARKET — sweeps until filled or the book is dry; the remainder expires
///   rather than resting (a market order has no price to rest at).
/// - CANCEL — OrderBook::cancel removes a resting order in O(1).
///
/// Mirrors orderbook/matching_engine.py. Stateless apart from the trade-id
/// sequence.

#pragma once

#include <optional>
#include <vector>

#include "orderbook/order.hpp"
#include "orderbook/order_book.hpp"
#include "orderbook/trade.hpp"

namespace orderbook {

/// Outcome of one submission: the order, the trades it caused, where it landed.
struct MatchResult {
    Order order;
    std::vector<Trade> trades;
    bool resting;

    OrderStatus status() const { return order.status; }
    int filled_quantity() const { return order.filled(); }

    double traded_notional() const {
        double total = 0.0;
        for (const Trade& t : trades) total += t.notional();
        return total;
    }
    /// Volume-weighted average price, or nullopt if nothing traded.
    std::optional<double> average_price() const {
        if (trades.empty()) return std::nullopt;
        return traded_notional() / filled_quantity();
    }
};

class MatchingEngine {
public:
    explicit MatchingEngine(std::int64_t first_trade_id = 1)
        : next_trade_id_(first_trade_id) {}

    std::int64_t trades_generated() const { return next_trade_id_ - 1; }

    /// Match `order` against `book`, then rest or expire the remainder.
    MatchResult submit(Order order, OrderBook& book);

    /// Pull a resting order. Returns std::nullopt if it was already gone.
    std::optional<Order> cancel(std::int64_t order_id, OrderBook& book) {
        return book.cancel(order_id);
    }

private:
    std::int64_t next_trade_id_;
};

}  // namespace orderbook

#include "orderbook/matching_engine.hpp"

#include <algorithm>

namespace orderbook {

MatchResult MatchingEngine::submit(Order order, OrderBook& book) {
    std::vector<Trade> trades;
    const Side opposing = opposite(order.side);
    OrderBook::LevelMap& opposing_levels = book.levels_for(opposing);

    // The sweep: best price first, FIFO within a level, every trade at the
    // resting order's price. The loop operates on the level and order
    // iterators it already holds — consume_level does fill + unlink +
    // empty-level collection with no id lookup and no re-validation.
    while (order.remaining > 0 && !opposing_levels.empty()) {
        // Both maps are ascending: best ask is first, best bid is last.
        auto level_it = opposing == Side::BUY ? std::prev(opposing_levels.end())
                                              : opposing_levels.begin();
        const double resting_price = level_it->first;
        if (!order.crosses(resting_price)) break;

        auto node = level_it->second.orders.begin();
        Order& resting = *node;
        const int quantity = std::min(order.remaining, resting.remaining);

        trades.push_back(Trade{next_trade_id_,
                               resting_price,
                               quantity,
                               order.side,
                               order.order_id,
                               resting.order_id});
        ++next_trade_id_;

        order.remaining -= quantity;
        book.consume_level(level_it, node, quantity);
    }

    if (order.remaining == 0) {
        order.status = OrderStatus::FILLED;
        return MatchResult{std::move(order), std::move(trades), false};
    }
    if (order.order_type == OrderType::MARKET) {
        // Nothing left to trade against and no price to rest at.
        order.status = OrderStatus::EXPIRED;
        return MatchResult{std::move(order), std::move(trades), false};
    }

    order.status = trades.empty() ? OrderStatus::RESTING : OrderStatus::PARTIALLY_FILLED;
    book.add(order);
    return MatchResult{std::move(order), std::move(trades), true};
}

}  // namespace orderbook

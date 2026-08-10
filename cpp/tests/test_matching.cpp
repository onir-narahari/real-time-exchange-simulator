/// Matching behavior: sweeps, locked/crossed books, market orders, views,
/// and one conservation property over randomized limit flow.

#include <random>
#include <vector>

#include "orderbook/matching_engine.hpp"
#include "check.h"

using namespace orderbook;

namespace {

bool multi_level_sweep_walks_in_price_order() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 101.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 5, 102.0), book);

    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 12, 102.0), book);
    CHECK(r.trades.size() == 3);
    CHECK(r.trades[0].price == 100.0 && r.trades[0].quantity == 5);
    CHECK(r.trades[0].resting_order_id == 2);
    CHECK(r.trades[1].price == 101.0 && r.trades[1].quantity == 5);
    CHECK(r.trades[1].resting_order_id == 1);
    CHECK(r.trades[2].price == 102.0 && r.trades[2].quantity == 2);  // partial at the end
    CHECK(r.trades[2].resting_order_id == 3);
    CHECK(r.order.is_filled());
    CHECK(r.status() == OrderStatus::FILLED);
    CHECK(book.ask_depth() == 3);
    book.validate();
    return true;
}

bool locked_book_trades_at_the_touch() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    // Incoming bid at exactly the ask: a locked print, executed at 100.
    MatchResult r = engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 5, 100.0), book);
    CHECK(r.trades.size() == 1);
    CHECK(r.trades[0].price == 100.0);
    CHECK(r.order.is_filled());
    CHECK(book.size() == 0);
    book.validate();
    return true;
}

bool crossed_incoming_limit_gets_improvement_then_rests_uncrossed() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 101.0), book);
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 5, 103.0), book);

    // Willing to pay 102: sweeps 100 and 101 (price improvement), then the
    // remainder rests at 102 — inside the spread, never crossing 103.
    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 20, 102.0), book);
    CHECK(r.trades.size() == 2);
    CHECK(r.trades[0].price == 100.0);
    CHECK(r.trades[1].price == 101.0);
    CHECK(r.resting);
    CHECK(r.order.remaining == 10);
    CHECK(r.status() == OrderStatus::PARTIALLY_FILLED);
    CHECK(book.best_bid() == std::optional<double>(102.0));
    CHECK(book.best_ask() == std::optional<double>(103.0));
    book.validate();  // would throw on a crossed book
    return true;
}

bool sweep_stops_at_the_limit_price() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 101.0), book);

    MatchResult r = engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 10, 100.0), book);
    CHECK(r.trades.size() == 1);
    CHECK(r.trades[0].price == 100.0);
    CHECK(r.resting);
    CHECK(r.order.remaining == 5);
    CHECK(book.best_bid() == std::optional<double>(100.0));
    CHECK(book.best_ask() == std::optional<double>(101.0));
    book.validate();
    return true;
}

bool market_order_exhaustion_expires_the_remainder() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 101.0), book);

    MatchResult r = engine.submit(Order(3, Side::BUY, OrderType::MARKET, 20), book);
    CHECK(r.trades.size() == 2);
    CHECK(r.trades[0].price == 100.0);  // best first
    CHECK(r.trades[1].price == 101.0);
    CHECK(r.order.remaining == 10);
    CHECK(!r.resting);
    CHECK(r.status() == OrderStatus::EXPIRED);
    CHECK(book.level_count(Side::SELL) == 0);
    CHECK(!book.best_ask().has_value());
    book.validate();
    return true;
}

bool market_order_on_an_empty_book_expires_immediately() {
    OrderBook book;
    MatchingEngine engine;

    MatchResult r = engine.submit(Order(1, Side::BUY, OrderType::MARKET, 10), book);
    CHECK(r.trades.empty());
    CHECK(r.order.remaining == 10);
    CHECK(!r.resting);
    CHECK(r.status() == OrderStatus::EXPIRED);
    CHECK(book.size() == 0);
    book.validate();
    return true;
}

bool limit_remainder_rests_without_crossing() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    MatchResult r = engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 10, 100.0), book);
    CHECK(r.trades.size() == 1);
    CHECK(r.resting);
    CHECK(r.order.remaining == 5);
    CHECK(book.best_bid() == std::optional<double>(100.0));
    CHECK(!book.best_ask().has_value());
    book.validate();
    return true;
}

bool trade_ids_are_sequential_across_submissions() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    MatchResult r1 = engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 3, 100.0), book);
    MatchResult r2 = engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 2, 100.0), book);

    CHECK(r1.trades.size() == 1 && r1.trades[0].trade_id == 1);
    CHECK(r2.trades.size() == 1 && r2.trades[0].trade_id == 2);
    CHECK(engine.trades_generated() == 2);
    book.validate();
    return true;
}

bool l2_aggregates_and_caps_depth_best_first() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 3, 100.0), book);
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 7, 101.0), book);
    engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 5, 98.0), book);
    engine.submit(Order(5, Side::BUY, OrderType::LIMIT, 5, 99.0), book);

    BookSnapshot snap = book.l2();
    CHECK(snap.asks.size() == 2);
    CHECK(snap.asks[0].price == 100.0 && snap.asks[0].quantity == 8 &&
          snap.asks[0].order_count == 2);
    CHECK(snap.asks[1].price == 101.0 && snap.asks[1].quantity == 7);
    CHECK(snap.bids.size() == 2);
    CHECK(snap.bids[0].price == 99.0);  // best bid first
    CHECK(snap.bids[1].price == 98.0);

    BookSnapshot capped = book.l2(1);
    CHECK(capped.asks.size() == 1 && capped.asks[0].price == 100.0);
    CHECK(capped.bids.size() == 1 && capped.bids[0].price == 99.0);
    CHECK(snap.best_bid() == std::optional<double>(99.0));
    CHECK(snap.best_ask() == std::optional<double>(100.0));
    book.validate();
    return true;
}

bool l3_is_strict_price_time_on_both_sides() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::BUY, OrderType::LIMIT, 5, 99.0), book);
    engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(4, Side::SELL, OrderType::LIMIT, 5, 102.0), book);
    engine.submit(Order(5, Side::SELL, OrderType::LIMIT, 5, 101.0), book);
    engine.submit(Order(6, Side::SELL, OrderType::LIMIT, 5, 101.0), book);

    auto bids = book.l3(Side::BUY);
    CHECK(bids.size() == 3);
    CHECK(bids[0].order_id == 2 && bids[0].price == 100.0);
    CHECK(bids[1].order_id == 3 && bids[1].price == 100.0);
    CHECK(bids[2].order_id == 1 && bids[2].price == 99.0);

    auto asks = book.l3(Side::SELL);
    CHECK(asks.size() == 3);
    CHECK(asks[0].order_id == 5 && asks[0].price == 101.0);
    CHECK(asks[1].order_id == 6 && asks[1].price == 101.0);
    CHECK(asks[2].order_id == 4 && asks[2].price == 102.0);
    book.validate();
    return true;
}

bool top_of_book_spread_mid_and_depth() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::BUY, OrderType::LIMIT, 5, 99.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 101.0), book);

    CHECK(book.best_bid() == std::optional<double>(99.0));
    CHECK(book.best_ask() == std::optional<double>(101.0));
    CHECK(book.spread() == std::optional<double>(2.0));
    CHECK(book.mid_price() == std::optional<double>(100.0));
    CHECK(book.bid_depth() == 5 && book.ask_depth() == 5);
    CHECK(book.depth_at(Side::BUY, 99.0) == 5);
    CHECK(book.depth_at(Side::BUY, 50.0) == 0);

    engine.submit(Order(3, Side::SELL, OrderType::MARKET, 2), book);
    CHECK(book.bid_depth() == 3);
    CHECK(book.depth_at(Side::BUY, 99.0) == 3);
    book.validate();
    return true;
}

bool status_transitions_match_the_engine_contract() {
    OrderBook book;
    MatchingEngine engine;

    MatchResult rested = engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 10, 100.0), book);
    CHECK(rested.status() == OrderStatus::RESTING);

    MatchResult partial = engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 10, 105.0), book);
    CHECK(partial.status() == OrderStatus::RESTING);

    // Crosses partially, then rests.
    MatchResult r = engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 15, 100.0), book);
    CHECK(r.status() == OrderStatus::PARTIALLY_FILLED && r.resting);

    // Fully filled: sweeps order 2 at 105 in a single trade.
    MatchResult f = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 5, 105.0), book);
    CHECK(f.status() == OrderStatus::FILLED && !f.resting);
    CHECK(f.trades.size() == 1);
    CHECK(f.trades[0].price == 105.0 && f.trades[0].resting_order_id == 2);

    book.validate();
    return true;
}

bool quantity_is_conserved_under_random_limit_flow() {
    // Limit-only flow (no expiry, no cancels): every submitted share is
    // either traded or still resting. Each trade fills both sides, so
    //   submitted == 2 * traded + resting depth.
    OrderBook book;
    MatchingEngine engine;
    std::mt19937 rng(42);

    long submitted = 0, traded = 0;
    for (int i = 0; i < 2000; ++i) {
        Side side = (rng() % 2 == 0) ? Side::BUY : Side::SELL;
        int qty = 1 + static_cast<int>(rng() % 40);
        // Bids 99.90..100.00, asks 100.00..100.10: the books cross at 100.
        double price = side == Side::BUY
                           ? 99.90 + 0.01 * static_cast<double>(rng() % 11)
                           : 100.00 + 0.01 * static_cast<double>(rng() % 11);
        MatchResult r = engine.submit(Order(i + 1, side, OrderType::LIMIT, qty, price), book);
        submitted += qty;
        for (const Trade& t : r.trades) traded += t.quantity;
    }

    CHECK(submitted == 2 * traded + book.bid_depth() + book.ask_depth());
    book.validate();
    return true;
}

}  // namespace

std::vector<testing::Case> matching_cases() {
    return {
        {"multi_level_sweep_walks_in_price_order", multi_level_sweep_walks_in_price_order},
        {"locked_book_trades_at_the_touch", locked_book_trades_at_the_touch},
        {"crossed_incoming_limit_gets_improvement_then_rests_uncrossed",
         crossed_incoming_limit_gets_improvement_then_rests_uncrossed},
        {"sweep_stops_at_the_limit_price", sweep_stops_at_the_limit_price},
        {"market_order_exhaustion_expires_the_remainder", market_order_exhaustion_expires_the_remainder},
        {"market_order_on_an_empty_book_expires_immediately", market_order_on_an_empty_book_expires_immediately},
        {"limit_remainder_rests_without_crossing", limit_remainder_rests_without_crossing},
        {"trade_ids_are_sequential_across_submissions", trade_ids_are_sequential_across_submissions},
        {"l2_aggregates_and_caps_depth_best_first", l2_aggregates_and_caps_depth_best_first},
        {"l3_is_strict_price_time_on_both_sides", l3_is_strict_price_time_on_both_sides},
        {"top_of_book_spread_mid_and_depth", top_of_book_spread_mid_and_depth},
        {"status_transitions_match_the_engine_contract", status_transitions_match_the_engine_contract},
        {"quantity_is_conserved_under_random_limit_flow", quantity_is_conserved_under_random_limit_flow},
    };
}

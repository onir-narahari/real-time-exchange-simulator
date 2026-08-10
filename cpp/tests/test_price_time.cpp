/// Price-time priority, stated as executable cases.
///
/// The rules under test:
/// - best price first, always — arrival order never beats price
/// - FIFO within a price — earlier sequence has priority, and a partial fill
///   does not cost an order its place
/// - sequence numbers are assigned by the book at rest time, not at submit

#include <vector>

#include "orderbook/matching_engine.hpp"
#include "check.h"

using namespace orderbook;

namespace {

bool same_price_fifo() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 7, 100.0), book);
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 9, 100.0), book);

    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 12, 100.0), book);
    CHECK(r.trades.size() == 2);
    CHECK(r.trades[0].resting_order_id == 1);
    CHECK(r.trades[1].resting_order_id == 2);
    CHECK(r.trades[1].quantity == 7);

    // Order 3 was never touched and sits alone at the level.
    auto l3 = book.l3(Side::SELL);
    CHECK(l3.size() == 1);
    CHECK(l3[0].order_id == 3 && l3[0].remaining == 9);
    book.validate();
    return true;
}

bool better_price_wins_over_earlier_arrival_asks() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 101.0), book);  // earlier, worse
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 100.0), book);  // later, better

    MatchResult r = engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 5, 101.0), book);
    CHECK(r.trades.size() == 1);
    CHECK(r.trades[0].resting_order_id == 2);
    CHECK(r.trades[0].price == 100.0);
    CHECK(book.l3(Side::SELL).front().order_id == 1);  // order 1 still there
    book.validate();
    return true;
}

bool better_price_wins_over_earlier_arrival_bids() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::BUY, OrderType::LIMIT, 5, 99.0), book);   // earlier, worse
    engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 5, 100.0), book);  // later, better

    MatchResult r = engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 5, 99.0), book);
    CHECK(r.trades.size() == 1);
    CHECK(r.trades[0].resting_order_id == 2);
    CHECK(r.trades[0].price == 100.0);
    book.validate();
    return true;
}

bool partial_fill_preserves_priority() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 10, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 4, 100.0), book);
    // Order 1 is down to 6 but still ahead of order 2.
    CHECK(book.l3(Side::SELL).front().order_id == 1);

    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 10, 100.0), book);
    CHECK(r.trades.size() == 2);
    CHECK(r.trades[0].resting_order_id == 1);
    CHECK(r.trades[0].quantity == 6);
    CHECK(r.trades[1].resting_order_id == 2);
    CHECK(r.trades[1].quantity == 4);
    book.validate();
    return true;
}

bool full_fill_advances_to_the_next_order() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    engine.submit(Order(3, Side::BUY, OrderType::LIMIT, 5, 100.0), book);
    CHECK(!book.contains(1));

    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 1, 100.0), book);
    CHECK(r.trades.size() == 1);
    CHECK(r.trades[0].resting_order_id == 2);
    book.validate();
    return true;
}

bool a_later_order_never_jumps_ahead() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.cancel(2, book);  // leave a hole in the middle of the queue

    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 7, 100.0), book);
    CHECK(r.trades.size() == 2);
    CHECK(r.trades[0].resting_order_id == 1);
    CHECK(r.trades[1].resting_order_id == 3);  // id 3 fills only after id 1
    CHECK(r.trades[1].quantity == 2);
    book.validate();
    return true;
}

bool sequence_is_assigned_at_rest_time() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 10, 100.0), book);

    // Order 2 trades first, rests second: its sequence comes after order 1's
    // even though it was submitted later and immediately became aggressive.
    MatchResult r = engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 15, 100.0), book);
    CHECK(r.resting);
    CHECK(r.order.remaining == 5);
    CHECK(r.status() == OrderStatus::PARTIALLY_FILLED);

    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 5, 101.0), book);

    CHECK(book.get(1) == nullptr);  // fully filled, gone from the book
    const Order* o2 = book.get(2);
    const Order* o3 = book.get(3);
    CHECK(o2 != nullptr && o3 != nullptr);
    CHECK(o2->sequence > 0 && o3->sequence > o2->sequence);
    CHECK(o2->status == OrderStatus::PARTIALLY_FILLED);
    CHECK(o3->status == OrderStatus::RESTING);
    book.validate();
    return true;
}

}  // namespace

std::vector<testing::Case> price_time_cases() {
    return {
        {"same_price_fifo", same_price_fifo},
        {"better_price_wins_over_earlier_arrival_asks", better_price_wins_over_earlier_arrival_asks},
        {"better_price_wins_over_earlier_arrival_bids", better_price_wins_over_earlier_arrival_bids},
        {"partial_fill_preserves_priority", partial_fill_preserves_priority},
        {"full_fill_advances_to_the_next_order", full_fill_advances_to_the_next_order},
        {"a_later_order_never_jumps_ahead", a_later_order_never_jumps_ahead},
        {"sequence_is_assigned_at_rest_time", sequence_is_assigned_at_rest_time},
    };
}

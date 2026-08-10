/// Cancellation: position in the queue must not matter, misses must be
/// distinguishable, and a book under heavy submit/cancel churn must end
/// provably empty.

#include <random>
#include <vector>

#include "orderbook/matching_engine.hpp"
#include "check.h"

using namespace orderbook;

namespace {

bool cancel_head() {
    OrderBook book;
    MatchingEngine engine;
    for (int i = 1; i <= 3; ++i)
        engine.submit(Order(i, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    CHECK(engine.cancel(1, book).has_value());
    auto l3 = book.l3(Side::SELL);
    CHECK(l3.size() == 2 && l3[0].order_id == 2 && l3[1].order_id == 3);

    MatchResult r = engine.submit(Order(4, Side::BUY, OrderType::LIMIT, 1, 100.0), book);
    CHECK(r.trades[0].resting_order_id == 2);  // order 2 inherited the head
    book.validate();
    return true;
}

bool cancel_middle() {
    OrderBook book;
    MatchingEngine engine;
    for (int i = 1; i <= 3; ++i)
        engine.submit(Order(i, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    CHECK(engine.cancel(2, book).has_value());
    auto l3 = book.l3(Side::SELL);
    CHECK(l3.size() == 2 && l3[0].order_id == 1 && l3[1].order_id == 3);
    CHECK(book.ask_depth() == 10);
    book.validate();
    return true;
}

bool cancel_tail() {
    OrderBook book;
    MatchingEngine engine;
    for (int i = 1; i <= 3; ++i)
        engine.submit(Order(i, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    CHECK(engine.cancel(3, book).has_value());
    auto l3 = book.l3(Side::SELL);
    CHECK(l3.size() == 2 && l3[0].order_id == 1 && l3[1].order_id == 2);
    book.validate();
    return true;
}

bool cancel_returns_the_order_with_cancelled_status() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    auto cancelled = engine.cancel(1, book);
    CHECK(cancelled.has_value());
    CHECK(cancelled->order_id == 1);
    CHECK(cancelled->remaining == 5);
    CHECK(cancelled->status == OrderStatus::CANCELLED);
    CHECK(book.size() == 0);
    book.validate();
    return true;
}

bool cancel_misses_on_filled_and_unknown_orders() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 5, 100.0), book);  // fills 1

    CHECK(!engine.cancel(1, book).has_value());   // filled: miss
    CHECK(!engine.cancel(42, book).has_value());  // never existed: miss
    CHECK(book.size() == 0);
    book.validate();
    return true;
}

bool double_cancel_is_a_miss() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);

    CHECK(engine.cancel(1, book).has_value());
    CHECK(!engine.cancel(1, book).has_value());
    book.validate();
    return true;
}

bool cancel_after_partial_fill_keeps_the_remainder() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 10, 100.0), book);
    engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 4, 100.0), book);

    auto cancelled = engine.cancel(1, book);
    CHECK(cancelled.has_value());
    CHECK(cancelled->remaining == 6);
    CHECK(book.ask_depth() == 0);
    CHECK(book.level_count(Side::SELL) == 0);  // level collected
    CHECK(book.level_removes() == 1);
    book.validate();
    return true;
}

bool emptied_level_is_collected_and_reusable() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    CHECK(book.level_creates() == 1);

    engine.submit(Order(2, Side::BUY, OrderType::LIMIT, 5, 100.0), book);
    CHECK(book.level_count(Side::SELL) == 0);
    CHECK(book.level_removes() == 1);

    // The same price accepts new orders afterwards.
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 7, 100.0), book);
    CHECK(book.level_creates() == 2);
    CHECK(book.best_ask() == std::optional<double>(100.0));
    CHECK(book.ask_depth() == 7);
    CHECK(book.depth_at(Side::SELL, 100.0) == 7);
    book.validate();
    return true;
}

bool cancel_updates_depth_and_index() {
    OrderBook book;
    MatchingEngine engine;
    engine.submit(Order(1, Side::SELL, OrderType::LIMIT, 5, 100.0), book);
    engine.submit(Order(2, Side::SELL, OrderType::LIMIT, 3, 100.0), book);
    engine.submit(Order(3, Side::SELL, OrderType::LIMIT, 7, 101.0), book);

    engine.cancel(2, book);
    CHECK(book.ask_depth() == 12);
    CHECK(book.size() == 2);
    CHECK(!book.contains(2));
    CHECK(book.get(2) == nullptr);
    CHECK(book.depth_at(Side::SELL, 100.0) == 5);

    engine.cancel(1, book);  // empties the 100 level
    CHECK(book.depth_at(Side::SELL, 100.0) == 0);
    CHECK(book.level_count(Side::SELL) == 1);
    book.validate();
    return true;
}

bool book_survives_heavy_submit_cancel_churn() {
    // 4,000 mixed ops over a tight crossing grid, with validate() after every
    // op; then cancel everything and require a provably empty book.
    // mt19937 with modulo keeps the flow deterministic across platforms
    // (uniform_int_distribution is not required to be).
    OrderBook book;
    MatchingEngine engine;
    std::mt19937 rng(7);

    std::vector<std::int64_t> live;
    std::int64_t next_id = 1;

    for (int i = 0; i < 4000; ++i) {
        if (!live.empty() && rng() % 10 < 3) {
            std::size_t idx = rng() % live.size();
            engine.cancel(live[idx], book);
            live[idx] = live.back();
            live.pop_back();
        } else {
            Side side = (rng() % 2 == 0) ? Side::BUY : Side::SELL;
            int qty = 1 + static_cast<int>(rng() % 8);
            // Both sides draw from 99.98..100.02, so crossing is constant.
            double price = 99.98 + 0.01 * static_cast<double>(rng() % 5);
            if (rng() % 10 == 0) {
                engine.submit(Order(next_id, side, OrderType::MARKET, qty), book);
            } else {
                engine.submit(Order(next_id, side, OrderType::LIMIT, qty, price), book);
            }
            live.push_back(next_id);
            ++next_id;
        }
        book.validate();
    }

    // Cancel every id ever submitted; most are already gone — those misses
    // are part of the exercise.
    for (std::int64_t id = 1; id < next_id; ++id) engine.cancel(id, book);

    CHECK(book.size() == 0);
    CHECK(book.bid_depth() == 0 && book.ask_depth() == 0);
    CHECK(book.level_count(Side::BUY) == 0 && book.level_count(Side::SELL) == 0);
    CHECK(book.level_creates() == book.level_removes());
    CHECK(!book.best_bid().has_value() && !book.best_ask().has_value());
    book.validate();
    return true;
}

}  // namespace

std::vector<testing::Case> cancellation_cases() {
    return {
        {"cancel_head", cancel_head},
        {"cancel_middle", cancel_middle},
        {"cancel_tail", cancel_tail},
        {"cancel_returns_the_order_with_cancelled_status", cancel_returns_the_order_with_cancelled_status},
        {"cancel_misses_on_filled_and_unknown_orders", cancel_misses_on_filled_and_unknown_orders},
        {"double_cancel_is_a_miss", double_cancel_is_a_miss},
        {"cancel_after_partial_fill_keeps_the_remainder", cancel_after_partial_fill_keeps_the_remainder},
        {"emptied_level_is_collected_and_reusable", emptied_level_is_collected_and_reusable},
        {"cancel_updates_depth_and_index", cancel_updates_depth_and_index},
        {"book_survives_heavy_submit_cancel_churn", book_survives_heavy_submit_cancel_churn},
    };
}

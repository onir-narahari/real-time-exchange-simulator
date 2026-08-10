/// Limit order book: price levels, best-price access, and L2 aggregation.
///
/// Layout
/// ------
/// Each side keeps a std::map<double, PriceLevel>, ascending. The best ask is
/// `asks.begin()`, the best bid is `std::prev(bids.end())` — O(1) top of
/// book, O(log n) level insertion. This replaces Python's hand-maintained
/// {price: level} dict + sorted price array; std::map provides the same
/// guarantees (sorted, deduplicated, stable iterators) with less machinery.
///
/// `index_` maps an order id straight to (level iterator, order iterator),
/// so cancels and fills are O(1) with no scan — the same role as Python's
/// orders_by_id -> BookNode. std::map/std::list iterators stay valid across
/// other insertions and erasures, which is what makes storing them safe.
///
/// Depth and order counts are maintained incrementally; the book never
/// flattens itself to answer a question.

#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <vector>

#include "orderbook/order.hpp"
#include "orderbook/price_level.hpp"

namespace orderbook {

/// Raised when a book invariant is violated (by validate(), or by add()
/// refusing an order that cannot legally rest).
class BookIntegrityError : public std::logic_error {
public:
    using std::logic_error::logic_error;
};

/// One aggregated row of an L2 snapshot.
struct LevelView {
    double price;
    long quantity;
    std::size_t order_count;
};

/// L2 view: bids descending, asks ascending, both best-first.
struct BookSnapshot {
    std::vector<LevelView> bids;
    std::vector<LevelView> asks;

    std::optional<double> best_bid() const {
        return bids.empty() ? std::nullopt : std::optional<double>(bids.front().price);
    }
    std::optional<double> best_ask() const {
        return asks.empty() ? std::nullopt : std::optional<double>(asks.front().price);
    }
};

/// One row of the L3 view: a resting order in strict price-time priority.
struct L3Row {
    std::int64_t order_id;
    double price;
    int remaining;
};

class OrderBook {
public:
    using LevelMap = std::map<double, PriceLevel>;  // ascending, both sides
    using OrderList = PriceLevel::List;
    using LevelIterator = LevelMap::iterator;
    using ListIterator = OrderList::iterator;

    // ------------------------------------------------------------------
    // Mutation
    // ------------------------------------------------------------------

    /// Rest `order` at the back of its price level (time priority).
    ///
    /// Assigns the order's sequence number and moves its status NEW ->
    /// RESTING (an engine-set PARTIALLY_FILLED is preserved), then stores a
    /// copy. Throws BookIntegrityError if the order cannot legally rest.
    void add(Order& order);

    /// Remove a resting order in O(1). Returns it (status CANCELLED), or
    /// std::nullopt if it isn't on the book — a miss is a valid outcome.
    std::optional<Order> cancel(std::int64_t order_id);

    /// Apply `quantity` of execution to a known order in a known level.
    ///
    /// The matching engine's hot path, mirroring the Python primitive of the
    /// same name: the sweep already holds the level and the order iterator,
    /// so this does no id lookup and no re-validation. The caller guarantees
    /// `0 < quantity <= node->remaining`.
    ///
    /// A fully consumed order leaves the book (its final state lives in the
    /// trade record); its level is collected if it emptied. A partially
    /// consumed order keeps its place at the head.
    void consume_level(LevelIterator level, ListIterator node, int quantity);

    // ------------------------------------------------------------------
    // Top of book
    // ------------------------------------------------------------------

    std::optional<double> best_bid() const;
    std::optional<double> best_ask() const;
    std::optional<double> spread() const;
    std::optional<double> mid_price() const;

    /// The side's level map; the sweep reads the best level from it directly
    /// (begin() for asks, prev(end()) for bids) instead of going through an
    /// accessor that re-derives what it knows.
    LevelMap& levels_for(Side side) { return side == Side::BUY ? bids_ : asks_; }
    const LevelMap& levels_for(Side side) const {
        return side == Side::BUY ? bids_ : asks_;
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    /// Aggregated book, best-first. `depth` caps levels per side.
    BookSnapshot l2(std::optional<std::size_t> depth = std::nullopt) const;

    /// Every resting order on `side` in strict price-time priority.
    std::vector<L3Row> l3(Side side) const;

    long depth_at(Side side, double price) const;
    std::size_t level_count(Side side) const { return levels_for(side).size(); }

    bool contains(std::int64_t order_id) const { return index_.count(order_id) != 0; }
    const Order* get(std::int64_t order_id) const;

    std::size_t size() const { return index_.size(); }

    long bid_depth() const { return bid_depth_; }
    long ask_depth() const { return ask_depth_; }
    long level_creates() const { return level_creates_; }
    long level_removes() const { return level_removes_; }

    // ------------------------------------------------------------------
    // Invariants (tests / debugging — not the hot path)
    // ------------------------------------------------------------------

    /// Assert every book invariant. O(n) — for tests and debugging only.
    /// Sortedness and key dedup are guaranteed by std::map and not re-checked;
    /// everything Python's validate() checks beyond that is checked here.
    void validate() const;

private:
    LevelMap bids_;  // ascending; best bid is the LAST element
    LevelMap asks_;  // ascending; best ask is the FIRST element

    struct Location {
        Side side;
        LevelIterator level;
        ListIterator order;
    };
    std::unordered_map<std::int64_t, Location> index_;

    std::int64_t next_sequence_ = 1;
    long bid_depth_ = 0;
    long ask_depth_ = 0;
    long level_creates_ = 0;
    long level_removes_ = 0;

    long& depth_for(Side side) { return side == Side::BUY ? bid_depth_ : ask_depth_; }
};

}  // namespace orderbook

#include "orderbook/order_book.hpp"

#include <cassert>
#include <string>

namespace orderbook {

// ----------------------------------------------------------------------
// Mutation
// ----------------------------------------------------------------------

void OrderBook::add(Order& order) {
    if (!order.price.has_value()) {
        throw BookIntegrityError("cannot rest an order without a price");
    }
    if (order.remaining <= 0) {
        throw BookIntegrityError(
            "cannot rest order " + std::to_string(order.order_id) + " with remaining=" +
            std::to_string(order.remaining));
    }
    if (contains(order.order_id)) {
        throw BookIntegrityError("duplicate order id " + std::to_string(order.order_id));
    }

    order.sequence = next_sequence_++;
    if (order.status == OrderStatus::NEW) {
        order.status = OrderStatus::RESTING;
    }

    LevelMap& levels = levels_for(order.side);
    auto [level_it, inserted] = levels.try_emplace(*order.price, *order.price, order.side);
    if (inserted) ++level_creates_;

    PriceLevel& level = level_it->second;
    level.orders.push_back(order);  // the book owns the resting copy
    ListIterator node = std::prev(level.orders.end());
    level.total_quantity += order.remaining;
    index_.emplace(order.order_id, Location{order.side, level_it, node});
    depth_for(order.side) += order.remaining;
}

std::optional<Order> OrderBook::cancel(std::int64_t order_id) {
    auto it = index_.find(order_id);
    if (it == index_.end()) return std::nullopt;

    Location loc = it->second;
    index_.erase(it);

    Order removed = *loc.order;  // copy out before unlinking
    PriceLevel& level = loc.level->second;

    level.total_quantity -= removed.remaining;
    depth_for(removed.side) -= removed.remaining;
    level.orders.erase(loc.order);

    if (level.orders.empty()) {
        levels_for(removed.side).erase(loc.level);
        ++level_removes_;
    }

    removed.status = OrderStatus::CANCELLED;
    return removed;
}

void OrderBook::consume_level(LevelIterator level_it, ListIterator node, int quantity) {
    PriceLevel& level = level_it->second;
    Order& resting = *node;
    assert(quantity > 0 && quantity <= resting.remaining);

    level.total_quantity -= quantity;
    depth_for(resting.side) -= quantity;

    if (quantity < resting.remaining) {
        // Partial fill: the order keeps its place at the head of the queue.
        resting.remaining -= quantity;
        resting.status = OrderStatus::PARTIALLY_FILLED;
        return;
    }

    // Full fill: unlink by iterator (O(1)) and collect the level if it emptied.
    index_.erase(resting.order_id);
    level.orders.erase(node);
    if (level.orders.empty()) {
        levels_for(level.side).erase(level_it);
        ++level_removes_;
    }
}

// ----------------------------------------------------------------------
// Top of book
// ----------------------------------------------------------------------

std::optional<double> OrderBook::best_bid() const {
    if (bids_.empty()) return std::nullopt;
    return bids_.rbegin()->first;
}

std::optional<double> OrderBook::best_ask() const {
    if (asks_.empty()) return std::nullopt;
    return asks_.begin()->first;
}

std::optional<double> OrderBook::spread() const {
    auto bid = best_bid(), ask = best_ask();
    if (!bid || !ask) return std::nullopt;
    return *ask - *bid;
}

std::optional<double> OrderBook::mid_price() const {
    auto bid = best_bid(), ask = best_ask();
    if (!bid || !ask) return std::nullopt;
    return (*bid + *ask) / 2;
}

// ----------------------------------------------------------------------
// Views
// ----------------------------------------------------------------------

BookSnapshot OrderBook::l2(std::optional<std::size_t> depth) const {
    BookSnapshot snapshot;

    for (auto it = bids_.rbegin(); it != bids_.rend(); ++it) {
        if (depth && snapshot.bids.size() >= *depth) break;
        snapshot.bids.push_back(
            LevelView{it->first, it->second.total_quantity, it->second.order_count()});
    }
    for (auto it = asks_.begin(); it != asks_.end(); ++it) {
        if (depth && snapshot.asks.size() >= *depth) break;
        snapshot.asks.push_back(
            LevelView{it->first, it->second.total_quantity, it->second.order_count()});
    }
    return snapshot;
}

std::vector<L3Row> OrderBook::l3(Side side) const {
    std::vector<L3Row> rows;
    const LevelMap& levels = levels_for(side);
    if (side == Side::BUY) {
        for (auto it = levels.rbegin(); it != levels.rend(); ++it) {
            for (const Order& o : it->second.orders) {
                rows.push_back(L3Row{o.order_id, it->first, o.remaining});
            }
        }
    } else {
        for (const auto& [price, level] : levels) {
            for (const Order& o : level.orders) {
                rows.push_back(L3Row{o.order_id, price, o.remaining});
            }
        }
    }
    return rows;
}

long OrderBook::depth_at(Side side, double price) const {
    const LevelMap& levels = levels_for(side);
    auto it = levels.find(price);
    return it == levels.end() ? 0 : it->second.total_quantity;
}

const Order* OrderBook::get(std::int64_t order_id) const {
    auto it = index_.find(order_id);
    return it == index_.end() ? nullptr : &*it->second.order;
}

// ----------------------------------------------------------------------
// Invariants
// ----------------------------------------------------------------------

void OrderBook::validate() const {
    long walked_bid = 0, walked_ask = 0;
    std::size_t indexed = 0;

    for (Side side : {Side::BUY, Side::SELL}) {
        const LevelMap& levels = levels_for(side);
        const char* label = to_string(side);

        for (const auto& [price, level] : levels) {
            if (level.empty()) {
                throw BookIntegrityError(std::string("empty ") + label + " level");
            }

            long counted_qty = 0;
            for (const Order& order : level.orders) {
                if (order.remaining <= 0) {
                    throw BookIntegrityError(
                        "order " + std::to_string(order.order_id) +
                        " rests with remaining=" + std::to_string(order.remaining));
                }
                if (!order.price || *order.price != price || order.side != side) {
                    throw BookIntegrityError(
                        "order " + std::to_string(order.order_id) +
                        " is filed at the wrong level");
                }
                auto it = index_.find(order.order_id);
                if (it == index_.end() || &*it->second.order != &order) {
                    throw BookIntegrityError(
                        "order " + std::to_string(order.order_id) +
                        " is linked but not indexed");
                }
                counted_qty += order.remaining;
            }

            if (counted_qty != level.total_quantity) {
                throw BookIntegrityError(
                    std::string(label) + " level quantity cache is " +
                    std::to_string(level.total_quantity) + ", walked " +
                    std::to_string(counted_qty));
            }

            if (side == Side::BUY) walked_bid += counted_qty;
            else walked_ask += counted_qty;
            indexed += level.orders.size();
        }
    }

    if (walked_bid != bid_depth_ || walked_ask != ask_depth_) {
        throw BookIntegrityError(
            "depth cache mismatch: bid " + std::to_string(bid_depth_) + " vs " +
            std::to_string(walked_bid) + ", ask " + std::to_string(ask_depth_) +
            " vs " + std::to_string(walked_ask));
    }
    if (indexed != index_.size()) {
        throw BookIntegrityError(
            "index holds " + std::to_string(index_.size()) + " orders, book holds " +
            std::to_string(indexed));
    }

    auto bid = best_bid(), ask = best_ask();
    if (bid && ask && *bid >= *ask) {
        throw BookIntegrityError("crossed book");
    }
}

}  // namespace orderbook

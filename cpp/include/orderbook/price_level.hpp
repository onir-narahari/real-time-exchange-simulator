/// A single price point in the book: a FIFO queue of orders.
///
/// Python uses an intrusive doubly-linked list so a cancel can unlink in O(1)
/// given the node. The C++-idiomatic equivalent is std::list<Order>: node
/// stability and O(1) erase-by-iterator, with the OrderBook's index holding
/// an iterator straight to each order. Same guarantees, no hand-rolled links.
///
/// `total_quantity` is maintained incrementally by the OrderBook on every
/// add, fill, and cancel — an L2 view never walks the queue.

#pragma once

#include <list>

#include "orderbook/order.hpp"

namespace orderbook {

struct PriceLevel {
    using List = std::list<Order>;

    double price;
    Side side;
    List orders;          ///< front of the list has time priority
    long total_quantity = 0;

    PriceLevel(double price, Side side) : price(price), side(side) {}

    bool empty() const { return orders.empty(); }
    std::size_t order_count() const { return orders.size(); }

    /// Front of the queue — the order with time priority at this price.
    Order& head() { return orders.front(); }
    const Order& head() const { return orders.front(); }
};

}  // namespace orderbook

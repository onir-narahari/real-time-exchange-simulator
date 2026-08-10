#include "orderbook/order.hpp"

#include <stdexcept>

namespace orderbook {

const char* to_string(Side side) {
    return side == Side::BUY ? "buy" : "sell";
}

const char* to_string(OrderType type) {
    return type == OrderType::LIMIT ? "limit" : "market";
}

const char* to_string(OrderStatus status) {
    switch (status) {
        case OrderStatus::NEW: return "new";
        case OrderStatus::RESTING: return "resting";
        case OrderStatus::PARTIALLY_FILLED: return "partially_filled";
        case OrderStatus::FILLED: return "filled";
        case OrderStatus::CANCELLED: return "cancelled";
        case OrderStatus::EXPIRED: return "expired";
    }
    return "unknown";
}

Side side_from_string(const std::string& s) {
    if (s == "buy") return Side::BUY;
    if (s == "sell") return Side::SELL;
    throw std::invalid_argument("unknown side: " + s);
}

OrderType order_type_from_string(const std::string& s) {
    if (s == "limit") return OrderType::LIMIT;
    if (s == "market") return OrderType::MARKET;
    throw std::invalid_argument("unknown order type: " + s);
}

Order::Order(std::int64_t order_id,
             Side side,
             OrderType order_type,
             int quantity,
             std::optional<double> price)
    : order_id(order_id),
      side(side),
      order_type(order_type),
      quantity(quantity),
      remaining(quantity),
      price(price),
      sequence(0),
      status(OrderStatus::NEW) {
    if (quantity <= 0) {
        throw std::invalid_argument("quantity must be positive");
    }
    if (order_type == OrderType::LIMIT) {
        if (!price.has_value()) {
            throw std::invalid_argument("limit orders require a price");
        }
        if (*price <= 0) {
            throw std::invalid_argument("price must be positive");
        }
    } else {
        // A market order takes whatever the book offers; a price is meaningless.
        this->price = std::nullopt;
    }
}

bool Order::crosses(double resting_price) const {
    if (order_type == OrderType::MARKET) return true;
    if (side == Side::BUY) return *price >= resting_price;
    return *price <= resting_price;
}

}  // namespace orderbook

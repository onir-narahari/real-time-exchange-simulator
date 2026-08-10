/// Core order types.
///
/// Mirrors orderbook/order.py: an Order is the single input to the engine and
/// carries only what the book and matching engine need to establish
/// price-time priority. Validation happens at construction, as in Python.
///
/// Differences from Python that do not change observable behavior:
/// - `price` is std::optional<double>; market orders hold std::nullopt where
///   Python stores None.
/// - `sequence` is 0 until the book assigns one at rest time (Python uses
///   None; real sequences start at 1, so 0 is an unambiguous sentinel).

#pragma once

#include <cstdint>
#include <optional>
#include <string>

namespace orderbook {

enum class Side { BUY, SELL };
enum class OrderType { LIMIT, MARKET };
enum class OrderStatus {
    NEW,
    RESTING,
    PARTIALLY_FILLED,
    FILLED,
    CANCELLED,
    /// Market order remainder that found no more liquidity.
    EXPIRED,
};

constexpr Side opposite(Side side) {
    return side == Side::BUY ? Side::SELL : Side::BUY;
}

/// String forms are byte-identical to the Python enums' values — the
/// cross-language test vectors depend on it.
const char* to_string(Side side);
const char* to_string(OrderType type);
const char* to_string(OrderStatus status);

Side side_from_string(const std::string& s);
OrderType order_type_from_string(const std::string& s);

struct Order {
    std::int64_t order_id;
    Side side;
    OrderType order_type;
    int quantity;            ///< original size; never changes
    int remaining;           ///< what is still live
    std::optional<double> price;
    std::int64_t sequence;   ///< assigned by the book when the order rests
    OrderStatus status;

    Order(std::int64_t order_id,
          Side side,
          OrderType order_type,
          int quantity,
          std::optional<double> price = std::nullopt);

    int filled() const { return quantity - remaining; }
    bool is_filled() const { return remaining == 0; }
    bool is_limit() const { return order_type == OrderType::LIMIT; }

    /// True if this order is willing to trade at `resting_price`.
    bool crosses(double resting_price) const;
};

}  // namespace orderbook

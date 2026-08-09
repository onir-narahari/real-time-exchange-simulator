"""A single price point in the book: a FIFO queue of orders.

Orders are held in an intrusive doubly-linked list so that an arbitrary order
can be unlinked in O(1) given its node — which is what makes cancellation
cheap. Aggregate quantity and order count are maintained incrementally, so an
L2 view never walks the queue.
"""

from __future__ import annotations

from typing import Iterator, Optional

from orderbook.order import Order


class BookNode:
    """Linked-list slot holding one resting order."""

    __slots__ = ("order", "prev", "next", "level")

    def __init__(self, order: Order, level: "PriceLevel"):
        self.order = order
        self.prev: Optional[BookNode] = None
        self.next: Optional[BookNode] = None
        self.level = level


class PriceLevel:
    """FIFO queue of resting orders at one price."""

    __slots__ = ("price", "side", "head", "tail", "total_quantity", "order_count")

    def __init__(self, price: float, side):
        self.price = price
        self.side = side
        self.head: Optional[BookNode] = None
        self.tail: Optional[BookNode] = None
        self.total_quantity = 0
        self.order_count = 0

    def is_empty(self) -> bool:
        return self.head is None

    def peek(self) -> Optional[Order]:
        """Front of the queue — the order with time priority at this price."""
        return self.head.order if self.head is not None else None

    def append(self, order: Order) -> BookNode:
        node = BookNode(order, self)
        if self.tail is None:
            self.head = self.tail = node
        else:
            node.prev = self.tail
            self.tail.next = node
            self.tail = node
        self.total_quantity += order.remaining
        self.order_count += 1
        return node

    def remove(self, node: BookNode) -> Order:
        """Unlink ``node`` in O(1) and return its order."""
        self.total_quantity -= node.order.remaining
        self.order_count -= 1

        if node.prev is not None:
            node.prev.next = node.next
        else:
            self.head = node.next
        if node.next is not None:
            node.next.prev = node.prev
        else:
            self.tail = node.prev

        node.prev = None
        node.next = None
        return node.order

    def reduce(self, node: BookNode, quantity: int) -> None:
        """Decrement a resting order's size after a partial fill."""
        node.order.remaining -= quantity
        self.total_quantity -= quantity

    def __iter__(self) -> Iterator[Order]:
        node = self.head
        while node is not None:
            yield node.order
            node = node.next

    def __len__(self) -> int:
        return self.order_count

    def __repr__(self) -> str:
        return (
            f"PriceLevel({self.price} {self.side}, "
            f"{self.total_quantity} across {self.order_count})"
        )

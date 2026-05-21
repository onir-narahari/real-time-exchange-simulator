class Order:
    def __init__(self, order_id, side, price, quantity, order_type="limit"):
        self.order_id = order_id
        self.side = side
        self.price = price
        self.quantity = quantity
        self.order_type = order_type


if __name__ == "__main__":
    order = Order(1, "buy", 100, 10, "limit")
    print(order.order_id)
    print(order.side)
    print(order.price)
    print(order.quantity)
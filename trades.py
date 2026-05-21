class Trade:
    def __init__(self, trade_id, price,quantity,timestamp,side):
        self.trade_id = trade_id
        self.side = side
        self.timestamp = timestamp
        self.price = price
        self.quantity = quantity
    def show_trade(self):
        print(f"Trade #{self.trade_id}")
        print(f"{self.side.upper()} AGGRESSOR")
        print(f"{self.quantity} @ {self.price}")
        print(f"{self.timestamp}")
        print("-" * 30)

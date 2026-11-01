"""有限事件窗口；客户端落后时明确要求重新连接，避免重播旧语音。"""

from collections import deque
import threading
import time


class EventLog:
    def __init__(self, capacity=256):
        self.items = deque(maxlen=capacity)
        self.sequence = 0
        self.cleared_through = 0
        self.condition = threading.Condition()

    def publish(self, kind, **data):
        with self.condition:
            self.sequence += 1
            event = {"seq": self.sequence, "kind": kind, **data}
            self.items.append(event)
            self.condition.notify_all()
            return event

    def read(self, after, timeout=15):
        with self.condition:
            deadline = time.monotonic() + min(timeout, 15)
            while self.sequence <= after and time.monotonic() < deadline:
                self.condition.wait(max(0, deadline - time.monotonic()))
            gap = bool(self.items and after < self.items[0]["seq"] - 1)
            intentional_clear = bool(gap and self.cleared_through > 0 and
                                     self.items[0]["seq"] == self.cleared_through + 1)
            if gap and not intentional_clear:
                return {"reset_required": True, "cursor": self.sequence, "events": []}
            return {"reset_required": False, "history_cleared": intentional_clear, "cursor": self.sequence,
                    "events": [dict(item) for item in self.items if item["seq"] > after]}

    def clear(self):
        with self.condition:
            self.items.clear()
            self.cleared_through = self.sequence
            # 保留递增序号；删除记忆时不允许旧游标读取被清除的内容。
            self.sequence += 1
            self.items.append({"seq": self.sequence, "kind": "history_cleared"})
            self.condition.notify_all()

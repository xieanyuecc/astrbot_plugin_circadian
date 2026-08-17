"""
CircadianClock — 时间计算模块
v0.3.0 重构：从"单点 + 模糊窗口"改为"区间"模型。

核心概念：
- 入睡窗口 [sleep_window_start, sleep_window_end)：进入后 LLM 回复逐渐"变困"，到终点兜底强制入睡
- 起床窗口 [wake_window_start, wake_window_end)：窗口内每天随机选一个时刻醒来（持久化）
- 跨午夜支持：入睡窗口跨天（如 23:00 - 00:00）；起床窗口不跨天
"""
from dataclasses import dataclass
from datetime import time, datetime
import random


@dataclass
class CircadianClock:
    # 入睡窗口：起点 → 终点（兜底强制入睡）
    sleep_window_start: time
    sleep_window_end: time
    # 起床窗口：起点 → 终点（窗口内随机唤醒）
    wake_window_start: time
    wake_window_end: time
    # 半醒窗口（分钟）：起床后多少分钟内处于半醒
    semi_awake_window: int = 30

    # ── 静态解析 ──

    @staticmethod
    def parse_time(t: str) -> time:
        """解析 HH:MM 格式时间字符串"""
        parts = t.strip().split(":")
        return time(int(parts[0]), int(parts[1]))

    @staticmethod
    def current_time_obj() -> time:
        now = datetime.now()
        return time(now.hour, now.minute)

    # ── 内部助手 ──

    @staticmethod
    def _to_mins(t: time) -> int:
        return t.hour * 60 + t.minute

    def _now_mins(self, current: datetime) -> int:
        return current.hour * 60 + current.minute

    # ── 入睡窗口 ──

    def in_sleep_window(self, current: datetime) -> bool:
        """
        当前是否处于入睡窗口内。
        - 23:00 - 00:00（跨天）：now ∈ [23:00, 24:00) ∪ [00:00, 00:00) 即 [23:00, 00:00)
        - 02:00 - 10:00（不跨天）：now ∈ [02:00, 10:00)
        - 异常配置（start == end）：恒 False
        """
        start = self._to_mins(self.sleep_window_start)
        end = self._to_mins(self.sleep_window_end)
        if start == end:
            return False
        now = self._now_mins(current)
        if start < end:
            # 不跨天
            return start <= now < end
        # 跨天（如 23:00 - 00:00）：now >= start OR now < end
        return now >= start or now < end

    def sleep_progress(self, current: datetime) -> float:
        """
        返回入睡窗口内的"渐困进度" 0-1。
        窗口外返回 0.0；起点 0.0；终点 1.0；中间线性。
        """
        if not self.in_sleep_window(current):
            return 0.0
        start = self._to_mins(self.sleep_window_start)
        end = self._to_mins(self.sleep_window_end)
        now = self._now_mins(current)
        if start < end:
            window = end - start
            elapsed = now - start
        else:
            # 跨天：从 start 到次日 end
            window = (24 * 60 - start) + end
            elapsed = (now - start) if now >= start else (24 * 60 - start) + now
        if window <= 0:
            return 1.0
        return min(1.0, max(0.0, elapsed / window))

    def should_force_sleep(self, current: datetime) -> bool:
        """
        是否到达强制入睡兜底点（窗口终点时刻）。

        - 不跨天（02:00-10:00）：now >= end
        - 跨天（23:00-00:00）：now 处于次日 [0, end]，即 now < start AND now <= end
          （end=0 时仅 00:00 那一分钟触发；end=180 时次日 00:00-02:59 触发）
        """
        start = self._to_mins(self.sleep_window_start)
        end = self._to_mins(self.sleep_window_end)
        if start == end:
            return False
        now = self._now_mins(current)
        if start < end:
            return now >= end
        # 跨天：now 在 [start, 24:00) 还在窗口内；now < start 时是次日，应 force
        if now >= start:
            return False
        return now <= end

    # ── 起床窗口 ──

    def in_wake_window(self, current: datetime) -> bool:
        """当前是否处于起床窗口内。起床窗口不跨天。"""
        start = self._to_mins(self.wake_window_start)
        end = self._to_mins(self.wake_window_end)
        if start >= end:
            return False
        now = self._now_mins(current)
        return start <= now < end

    def random_wake_time(self) -> time:
        """在起床窗口内随机选一个时刻。"""
        start = self._to_mins(self.wake_window_start)
        end = self._to_mins(self.wake_window_end)
        if start >= end:
            return self.wake_window_start
        # 包含起点但不包含终点（窗口终点那一刻视为"超了"）
        rand_mins = random.randint(start, end - 1)
        return time(rand_mins // 60, rand_mins % 60)

    def has_wake_time_passed(self, wake_at: time, current: datetime) -> bool:
        """给定的随机唤醒时刻是否已到（now >= wake_at）。"""
        wake_mins = self._to_mins(wake_at)
        now_mins = self._now_mins(current)
        return now_mins >= wake_mins

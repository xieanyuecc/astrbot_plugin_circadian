"""
CircadianClock — 时间计算模块
管理睡眠时间、起床时间、模糊窗口
"""
from dataclasses import dataclass
from datetime import time, datetime, timedelta
from typing import Optional


@dataclass
class CircadianClock:
    sleep_time: time
    wake_time: time
    fuzzy_window_minutes: int = 30
    """状态切换模糊窗口（分钟），在此窗口内 AI 自主决定是否切换"""

    def in_fuzzy_window_sleep(self, current: datetime) -> bool:
        """检查当前是否处于睡眠模糊窗口（wake_time 前 fuzzy_window 分钟内）"""
        # 模糊窗口起点 = wake_time - fuzzy_window，用分钟精度比较避免跨天 datetime 计算
        wake_mins = self.wake_time.hour * 60 + self.wake_time.minute
        fuzzy_start_mins = (wake_mins - self.fuzzy_window_minutes) % (24 * 60)
        current_mins = current.hour * 60 + current.minute

        if fuzzy_start_mins <= wake_mins:
            # 窗口不跨天
            return fuzzy_start_mins <= current_mins < wake_mins
        # 窗口跨天（例如 wake_time 00:30，fuzzy_window 60分钟，fuzzy_start 23:30）
        return current_mins >= fuzzy_start_mins or current_mins < wake_mins

    def in_fuzzy_window_wake(self, current: datetime) -> bool:
        """检查当前是否处于起床模糊窗口（sleep_time 后 fuzzy_window 分钟内）"""
        sleep_mins = self.sleep_time.hour * 60 + self.sleep_time.minute
        fuzzy_end_mins = (sleep_mins + self.fuzzy_window_minutes) % (24 * 60)
        current_mins = current.hour * 60 + current.minute

        if sleep_mins + self.fuzzy_window_minutes < 24 * 60:
            # 窗口不跨天
            return sleep_mins <= current_mins < sleep_mins + self.fuzzy_window_minutes
        # 窗口跨天：sleep_mins 之后 OR fuzzy_end_mins 之前（次日）
        return current_mins >= sleep_mins or current_mins < fuzzy_end_mins

    @staticmethod
    def parse_time(t: str) -> time:
        """解析 HH:MM 格式时间字符串"""
        parts = t.strip().split(":")
        return time(int(parts[0]), int(parts[1]))

    @staticmethod
    def current_time_obj() -> time:
        """获取当前本地时间（time 对象）"""
        now = datetime.now()
        return time(now.hour, now.minute)

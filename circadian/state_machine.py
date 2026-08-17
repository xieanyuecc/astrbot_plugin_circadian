"""
CircadianStateMachine — 状态机模块
v0.3.0 重构：渐困进度 + 兜底强制 + 每日定时随机唤醒

三态：AWAKE / SLEEPING / SEMI_AWAKE
进入入睡窗口后状态仍是 AWAKE，但 progress 会从 0→1；到终点兜底切 SLEEPING。
入睡后每天固定时刻随机唤醒，重启不变；跨过凌晨会重新随机。
"""
from enum import Enum
from dataclasses import dataclass
from datetime import datetime, time, date
from typing import Optional

from .clock import CircadianClock


class CircadianState(Enum):
    AWAKE = "awake"
    SLEEPING = "sleeping"
    SEMI_AWAKE = "semi_awake"


# 睡眠中检测到这些会"感知渗透"（轻微 arousal）
IMPORTANT_MESSAGE_KEYWORDS = [
    "紧急", "出事", "生病", "危险", "报警",
    "我在", "你还在吗", "醒醒", "救命",
]


@dataclass
class CircadianStateData:
    state: CircadianState = CircadianState.AWAKE
    last_transition: float = 0.0
    last_state_check: float = 0.0
    # 今天随机唤醒时刻（持久化）
    wake_random_time_iso: Optional[str] = None  # "HH:MM"
    wake_random_date: Optional[str] = None      # "YYYY-MM-DD"
    # 用户说"再睡会儿"延迟入睡分钟数
    sleep_delay_minutes: int = 0


class CircadianStateMachine:
    def __init__(
        self,
        clock: CircadianClock,
        initial_state: CircadianState = CircadianState.AWAKE,
    ):
        self.clock = clock
        self._state = initial_state
        self._data = CircadianStateData(
            last_transition=datetime.now().timestamp(),
            last_state_check=datetime.now().timestamp(),
        )

    @property
    def state(self) -> CircadianState:
        return self._state

    def get_data(self) -> CircadianStateData:
        return self._data

    def _update_check_time(self):
        self._data.last_state_check = datetime.now().timestamp()

    def _transition_to(self, new_state: CircadianState, current: datetime):
        self._state = new_state
        self._data.last_transition = current.timestamp()

    # ── 主循环 ──

    def tick(self, current: Optional[datetime] = None) -> tuple[float, Optional[str]]:
        """
        每分钟调一次。返回 (sleep_progress, signal)。
        signal:
          - None：无操作
          - "force_sleep"：到兜底点，状态切到 SLEEPING
          - "should_wake"：到今日随机唤醒时刻，状态切到 SEMI_AWAKE
          - "should_rain_wake_semi" / "should_rain_wake_awake"：雨声唤醒触发，由 main.py 调用 trigger_rain_wake()
        """
        if current is None:
            current = datetime.now()
        self._update_check_time()

        if self._state == CircadianState.AWAKE:
            # 计算渐困进度
            progress = self.clock.sleep_progress(current)
            if self.clock.should_force_sleep(current):
                self._transition_to(CircadianState.SLEEPING, current)
                return progress, "force_sleep"
            return progress, None

        elif self._state == CircadianState.SLEEPING:
            # 是否到今日随机唤醒时刻
            if self._data.wake_random_time_iso:
                wake_t = self.clock.parse_time(self._data.wake_random_time_iso)
                if self.clock.has_wake_time_passed(wake_t, current):
                    self._transition_to(CircadianState.SEMI_AWAKE, current)
                    return 0.0, "should_wake"
            return 0.0, None

        # SEMI_AWAKE 由外部事件驱动（用户发消息 / 用户说早安）
        return 0.0, None

    # ── 每日随机唤醒时刻 ──

    def set_today_wake_time(self, wake_t: time, today_date: Optional[date] = None):
        """
        设置今日随机唤醒时刻。
        如果传入日期与已存日期不同（跨天），覆盖；相同则保留。
        """
        if today_date is None:
            today_date = date.today()
        today_iso = today_date.isoformat()
        if self._data.wake_random_date != today_iso:
            self._data.wake_random_date = today_iso
            self._data.wake_random_time_iso = wake_t.strftime("%H:%M")

    def needs_wake_time_roll(self, today_date: Optional[date] = None) -> bool:
        """是否需要为今天重新随机一个唤醒时刻（跨天或缺失）。"""
        if today_date is None:
            today_date = date.today()
        return (
            self._data.wake_random_date != today_date.isoformat()
            or self._data.wake_random_time_iso is None
        )

    # ── 外部事件触发 ──

    def trigger_sleep(self, delay_minutes: int = 0):
        """用户说"晚安"，立刻切到 SLEEPING（绕过窗口）。"""
        self._data.sleep_delay_minutes = delay_minutes
        self._transition_to(CircadianState.SLEEPING, datetime.now())

    def trigger_wake(self):
        """用户说"早安"，跳过 SEMI_AWAKE 直接到 AWAKE。"""
        self._transition_to(CircadianState.AWAKE, datetime.now())

    def trigger_semi_awake(self):
        """内部用：从 SLEEPING 进入 SEMI_AWAKE。"""
        self._transition_to(CircadianState.SEMI_AWAKE, datetime.now())

    def wake_to_awake(self):
        """半醒收到消息后切 AWAKE。"""
        self._transition_to(CircadianState.AWAKE, datetime.now())

    def trigger_rain_wake(self, pass_through_semi: bool = True) -> bool:
        """
        雨声唤醒：SLEEPING → AWAKE 或 SEMI_AWAKE。
        默认走 SEMI_AWAKE（被吵醒不等于清醒，先梦再醒）。
        pass_through_semi=False 时直接 AWAKE。
        只在 SLEEPING 状态下生效。
        """
        if self._state != CircadianState.SLEEPING:
            return False
        target = CircadianState.SEMI_AWAKE if pass_through_semi else CircadianState.AWAKE
        self._transition_to(target, datetime.now())
        return True

    def delay_sleep(self, minutes: int):
        self._data.sleep_delay_minutes = minutes

    def check_important_message(self, message_str: str) -> bool:
        if self._state != CircadianState.SLEEPING:
            return False
        return any(kw in message_str for kw in IMPORTANT_MESSAGE_KEYWORDS)

    # ── 序列化（持久化） ──

    def to_dict(self) -> dict:
        return {
            "state": self._state.value,
            "last_transition": self._data.last_transition,
            "last_state_check": self._data.last_state_check,
            "wake_random_time_iso": self._data.wake_random_time_iso,
            "wake_random_date": self._data.wake_random_date,
            "sleep_delay_minutes": self._data.sleep_delay_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict, clock: CircadianClock) -> "CircadianStateMachine":
        sm = cls(clock, CircadianState(data["state"]))
        sm._data.last_transition = data.get("last_transition", 0.0)
        sm._data.last_state_check = data.get("last_state_check", 0.0)
        sm._data.wake_random_time_iso = data.get("wake_random_time_iso")
        sm._data.wake_random_date = data.get("wake_random_date")
        sm._data.sleep_delay_minutes = data.get("sleep_delay_minutes", 0)
        return sm

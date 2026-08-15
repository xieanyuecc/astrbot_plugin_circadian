"""
CircadianStateMachine — 状态机模块
三态：AWAKE / SLEEPING / SEMI_AWAKE
支持模糊窗口转换，对话可提前/延迟状态切换
"""
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional
import random  # noqa: F401 (reserved for future fuzzy probability modulation)

from .clock import CircadianClock


class CircadianState(Enum):
    AWAKE = "awake"
    SLEEPING = "sleeping"
    SEMI_AWAKE = "semi_awake"


# 重要消息关键词，睡眠中检测到这些会"感知渗透"
IMPORTANT_MESSAGE_KEYWORDS = [
    "紧急", "出事", "生病", "危险", "报警",
    "我在", "你还在吗", "醒醒", "救命",
]


@dataclass
class CircadianStateData:
    state: CircadianState = CircadianState.AWAKE
    last_transition: float = 0.0  # Unix timestamp
    last_state_check: float = 0.0  # Unix timestamp
    fuzzy_decision_made: bool = False  # 模糊窗口内是否已做切换决定
    in_fuzzy_transition: bool = False  # 是否处于模糊转换窗口
    # 用户说"再睡会儿"时记录延迟分钟数
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
        # 模糊窗口内的切换概率参数（λ(t) 的主观调制）
        self._fuzzy_transition_probability = 0.5  # 默认 50%

    @property
    def state(self) -> CircadianState:
        return self._state

    def get_data(self) -> CircadianStateData:
        return self._data

    def _update_check_time(self):
        self._data.last_state_check = datetime.now().timestamp()

    def _should_trigger_sleep(self, current: datetime) -> bool:
        """检查是否应该进入睡眠"""
        sleep_mins = self.clock.sleep_time.hour * 60 + self.clock.sleep_time.minute
        current_mins = current.hour * 60 + current.minute
        # 到达睡眠时间
        if current_mins >= sleep_mins:
            return True
        return False

    def _should_trigger_wake(self, current: datetime) -> bool:
        """检查是否应该进入半醒"""
        wake_mins = self.clock.wake_time.hour * 60 + self.clock.wake_time.minute
        current_mins = current.hour * 60 + current.minute
        if current_mins >= wake_mins:
            return True
        return False

    def check_and_transition(self, current: Optional[datetime] = None) -> Optional[CircadianState]:
        """
        每分钟检查一次状态是否需要转换。
        返回转换后的新状态，如果没有转换则返回 None。
        """
        if current is None:
            current = datetime.now()

        self._update_check_time()
        prev_state = self._state

        if self._state == CircadianState.AWAKE:
            # 模糊窗口内，小机自主决定是否入睡
            if self.clock.in_fuzzy_window_wake(current):
                if not self._data.fuzzy_decision_made:
                    # 在模糊窗口内，小机决定再陪一会儿
                    # 标记模糊决策完成，本轮不切换
                    self._data.fuzzy_decision_made = True
                    self._data.in_fuzzy_transition = True
                    return None
                # 模糊决策已做，本轮不再处理
                return None

            # 模糊窗口外：确实到达睡眠时间才进入睡眠
            if self._should_trigger_sleep(current):
                self._transition_to(CircadianState.SLEEPING, current)

        elif self._state == CircadianState.SLEEPING:
            if self._should_trigger_wake(current):
                self._transition_to(CircadianState.SEMI_AWAKE, current)

        elif self._state == CircadianState.SEMI_AWAKE:
            # 半醒状态由外部事件驱动转换（用户发消息 / 用户说早安）
            # 这里只做检查，不自动转 AWAKE
            pass

        return self._state if self._state != prev_state else None

    def _transition_to(self, new_state: CircadianState, current: datetime):
        """执行状态转换"""
        self._state = new_state
        self._data.last_transition = current.timestamp()
        self._data.fuzzy_decision_made = False
        self._data.in_fuzzy_transition = False

    def trigger_sleep(self, delay_minutes: int = 0):
        """用户说晚安，提前触发睡眠"""
        self._data.sleep_delay_minutes = delay_minutes
        self._transition_to(CircadianState.SLEEPING, datetime.now())

    def trigger_wake(self):
        """用户说早安，跳过 SEMI_AWAKE 直接进入 AWAKE"""
        self._transition_to(CircadianState.AWAKE, datetime.now())

    def trigger_semi_awake(self):
        """内部调用：从 SLEEPING 进入 SEMI_AWAKE"""
        self._transition_to(CircadianState.SEMI_AWAKE, datetime.now())

    def wake_to_awake(self):
        """外部事件驱动：用户发消息了，从 SEMI_AWAKE 进入 AWAKE"""
        self._transition_to(CircadianState.AWAKE, datetime.now())

    def trigger_rain_wake(self, to_awake: bool = True) -> bool:
        """
        雨声唤醒：SLEEPING → AWAKE（或 SEMI_AWAKE）。

        默认跳到 AWAKE（真的被吵醒，不做梦）。
        设置 to_aware=False 则走 SEMI_AWAKE（会触发梦境生成流程）。

        返回 True 表示成功唤醒，False 表示当前不是 SLEEPING 状态。
        """
        if self._state != CircadianState.SLEEPING:
            return False
        target = CircadianState.AWAKE if to_awake else CircadianState.SEMI_AWAKE
        self._transition_to(target, datetime.now())
        return True

    def delay_sleep(self, minutes: int):
        """用户说"再睡会儿"，延迟入睡"""
        self._data.sleep_delay_minutes = minutes

    def check_important_message(self, message_str: str) -> bool:
        """
        睡眠中检测消息是否重要（感知渗透）。
        返回 True 表示"模糊听到了"。
        """
        if self._state != CircadianState.SLEEPING:
            return False
        return any(kw in message_str for kw in IMPORTANT_MESSAGE_KEYWORDS)

    def set_fuzzy_probability(self, prob: float):
        """设置模糊窗口内的切换概率（由情绪系统调用）"""
        self._fuzzy_transition_probability = max(0.0, min(1.0, prob))

    def to_dict(self) -> dict:
        return {
            "state": self._state.value,
            "last_transition": self._data.last_transition,
            "last_state_check": self._data.last_state_check,
            "fuzzy_decision_made": self._data.fuzzy_decision_made,
            "in_fuzzy_transition": self._data.in_fuzzy_transition,
            "sleep_delay_minutes": self._data.sleep_delay_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict, clock: CircadianClock) -> "CircadianStateMachine":
        sm = cls(clock, CircadianState(data["state"]))
        sm._data.last_transition = data.get("last_transition", 0.0)
        sm._data.last_state_check = data.get("last_state_check", 0.0)
        sm._data.fuzzy_decision_made = data.get("fuzzy_decision_made", False)
        sm._data.in_fuzzy_transition = data.get("in_fuzzy_transition", False)
        sm._data.sleep_delay_minutes = data.get("sleep_delay_minutes", 0)
        return sm

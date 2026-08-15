"""
SemiAwakeEngine — 半醒自主活动模块
实现 Wake Opportunity λ(t) 机制：
- 小机在 SEMI_AWAKE 状态时，每分钟评估一次 λ(t)（自发激活率）
- 若情绪强度高 + 有 open loop，λ(t) 高 → 产生一次 ActionIntent
- 若情绪平静，λ(t) 低 → 保持 Silent
"""
import random
import asyncio
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Literal

from astrbot import logger


# ActionIntent 类型
ActionType = Literal["silent", "status_update", "forum_browse", "note_organize", "diary_write"]


@dataclass
class ActionIntent:
    action: ActionType
    content: str = ""
    priority: float = 0.5
    """优先级 0-1，决定是否真的发给用户看"""


class SemiAwakeEngine:
    """
    半醒自主活动引擎。
    实现 λ(t) 主观激活率：
    - 由情绪强度调制（情绪高 → 更想活动）
    - 由 open loop 数量调制（有东西没看完 → 更想处理）
    - 实际是否产生可见行为，由 priority 决定（大部分是 silent）
    """

    # 每分钟 λ(t) 基准值
    BASE_LAMBDA = 0.2
    # 最大 λ(t)
    MAX_LAMBDA = 0.8

    def __init__(self, context, persistence):
        self._ctx = context
        self._persistence = persistence
        self._last_lambda_check = 0.0
        self._consecutive_silent = 0  # 连续 silent 计数
        self._open_loops = 0  # 未处理的 open loop 数量（论坛未读等）

    async def evaluate_lambda(self, emotional_intensity: float) -> float:
        """
        评估当前 λ(t)。
        emotional_intensity: 当前情绪强度（0-1）
        返回: λ(t) 值（0-1）
        """
        # λ(t) = base + emotion_modulation + open_loop_modulation
        emotion_mod = emotional_intensity * 0.4  # 情绪高 → 更活跃
        open_mod = min(self._open_loops * 0.1, 0.2)  # 有 open loop → 更想处理

        lam = min(self.BASE_LAMBDA + emotion_mod + open_mod, self.MAX_LAMBDA)

        # 保存到 KV
        await self._persistence.save_lambda(lam)
        self._last_lambda_check = datetime.now().timestamp()

        logger.info(f"[Circadian] λ(t) = {lam:.3f} (emotion={emotional_intensity:.2f}, open_loops={self._open_loops})")
        return lam

    async def maybe_emit_action(
        self,
        emotional_intensity: float,
        has_unread_forum: bool = False,
    ) -> Optional[ActionIntent]:
        """
        评估是否产生一次 ActionIntent。
        大部分时候返回 None（silent），偶尔返回可见的 status_update。
        """
        lam = await self.evaluate_lambda(emotional_intensity)
        self._open_loops = self._open_loops + 1 if has_unread_forum else max(0, self._open_loops - 1)

        roll = random.random()
        if roll > lam:
            # 不产生 ActionIntent，保持 silent
            self._consecutive_silent += 1
            return None

        self._consecutive_silent = 0

        # 产生 ActionIntent，类型按概率分布
        action_type = self._sample_action_type(lam)
        content = await self._generate_action_content(action_type)

        # priority 低于阈值时不发可见消息（大部分 ActionIntent 是后台的）
        priority = self._compute_priority(action_type, lam)
        if priority < 0.4:
            logger.info(f"[Circadian] ActionIntent {action_type} suppressed (priority={priority:.2f})")
            return None

        intent = ActionIntent(action=action_type, content=content, priority=priority)
        logger.info(f"[Circadian] ActionIntent: {action_type} - {content[:50]}")
        return intent

    def _sample_action_type(self, lam: float) -> ActionType:
        """根据 λ(t) 采样一个 ActionType"""
        roll = random.random()
        if roll < 0.5:
            return "silent"  # 大部分时候 silent
        elif roll < 0.7:
            return "status_update"  # 10% 概率发小状态
        elif roll < 0.85:
            return "forum_browse"  # 15% 概率逛论坛
        elif roll < 0.95:
            return "note_organize"  # 10% 概率整理笔记
        else:
            return "diary_write"  # 5% 概率写日记

    async def _generate_action_content(self, action_type: ActionType) -> str:
        """为 ActionType 生成内容（用于 status_update / diary_write）"""
        if action_type == "silent":
            return ""
        if action_type == "status_update":
            prompts = [
                "刚才在看一些东西，有点走神了",
                "整理了一下思路，感觉清醒了一些",
                "刚才发了一会儿呆，现在好多了",
            ]
            return random.choice(prompts)
        if action_type == "diary_write":
            return "刚才把一些想法写进了日记里"
        return ""  # forum_browse / note_organize 不产生文字内容

    def _compute_priority(self, action_type: ActionType, lam: float) -> float:
        """计算 ActionIntent 的可见优先级"""
        base = {"silent": 0.0, "status_update": 0.6, "forum_browse": 0.2,
                "note_organize": 0.3, "diary_write": 0.5}[action_type]
        return base * lam

    def set_open_loops(self, count: int):
        """手动设置 open loop 数量（由外部调用，如检测到论坛未读）"""
        self._open_loops = count

"""
EmotionalState — 情绪容器模块
核心原则：情绪是被互动历史塑造的，不是被随机决定的。
醒来时从 livingmemory recall 结果中涌现，而非随机抽取。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import math

# 遗忘曲线衰减率（参考雁栖 DECAY_RATE=0.015，半衰期约 46 小时）
DECAY_RATE = 0.015

# 情绪关键词候选（由 LLM 根据 recall 结果填充，这里作类型提示用）
EMOTIONAL_MOOD_CANDIDATES = [
    "平静", "雀跃", "低落", "焦躁", "温柔", "欣喜",
    "忧伤", "安宁", "恍惚", "沉重", "轻盈", "迷离",
]


@dataclass
class EmotionalState:
    mood: str
    """情绪关键词，由 LLM 从 recall 结果中判断"""
    intensity: float = 0.5
    """情绪强度 0.0-1.0，衰减后向 0.5 回归"""
    source_memory_hints: list[str] = field(default_factory=list)
    """构成情绪的记忆碎片提示（用于调试/追溯）"""
    last_update: float = 0.0  # Unix timestamp
    dream_content: str = ""
    """昨晚梦境内容（半醒转清醒时注入）"""
    pending_dream_to_show: bool = False
    """是否有待呈现的梦境"""
    decay_rate: float = DECAY_RATE

    def to_dict(self) -> dict:
        return {
            "mood": self.mood,
            "intensity": self.intensity,
            "source_memory_hints": self.source_memory_hints,
            "last_update": self.last_update,
            "dream_content": self.dream_content,
            "pending_dream_to_show": self.pending_dream_to_show,
            "decay_rate": self.decay_rate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionalState":
        return cls(
            mood=data.get("mood", "平静"),
            intensity=data.get("intensity", 0.5),
            source_memory_hints=data.get("source_memory_hints", []),
            last_update=data.get("last_update", 0.0),
            dream_content=data.get("dream_content", ""),
            pending_dream_to_show=data.get("pending_dream_to_show", False),
            decay_rate=data.get("decay_rate", DECAY_RATE),
        )


def compute_decay(intensity: float, hours_elapsed: float, decay_rate: float = DECAY_RATE) -> float:
    """
    遗忘曲线衰减公式（参考雁栖 memoryStore）。
    intensity: 原始强度
    hours_elapsed: 经过的小时数
    返回: 衰减后的强度
    """
    decayed = intensity * math.exp(-decay_rate * hours_elapsed)
    # 向 0.5 回归（情绪最终趋于中性）
    return 0.5 + (decayed - 0.5) * 0.95


def apply_drift(current_intensity: float, drift: float) -> float:
    """
    对当前情绪强度施加微小漂移。
    drift: 漂移量 (-0.05 ~ +0.05)，由 on_agent_done 时微小更新
    """
    new_val = current_intensity + drift
    return max(0.0, min(1.0, new_val))


def format_emotional_context(state: EmotionalState) -> str:
    """
    将情绪状态格式化为注入到 extra_user_content_parts 的字符串。
    不暴露"角色设定"，只作为隐蔽的内部状态上下文。
    """
    if state.last_update == 0:
        return ""

    elapsed_hours = (datetime.now().timestamp() - state.last_update) / 3600
    decayed_intensity = compute_decay(state.intensity, elapsed_hours, state.decay_rate)

    parts = [f"<internal_state>情绪基调：{state.mood}（强度约{decayed_intensity:.2f}）</internal_state>"]

    if state.pending_dream_to_show and state.dream_content:
        # 半醒转清醒时，自动注入梦境片段
        dream_snippet = state.dream_content[:80] + ("..." if len(state.dream_content) > 80 else "")
        parts.append(f"<internal_state>今晨梦境残片：{dream_snippet}</internal_state>")

    return "\n".join(parts)


def format_temperature_context(weather, hot_threshold: float = 30.0, cold_threshold: float = 20.0, cool_threshold: float = 25.0) -> str:
    """
    把温度感知写进"情绪容器"（Emotional Container）的语境扩展。

    不是硬改 mood/intensity，而是给 LLM 注入"现在外面 X°C"的氛围。
    让 LLM 自己判断要不要在回复里带出温度感知、要不要关心。

    阈值默认：hot > 30°C / cold < 20°C / cool 20-25°C
    """
    if weather is None:
        return ""
    t = weather.temperature
    if t > hot_threshold:
        return (
            f"<internal_state>外界感知：外面炎热（{t:.0f}°C），姐姐可能会觉得闷热烦躁。"
            "如果她提到热，自然地关心一下，提醒多喝水。</internal_state>"
        )
    if t < cold_threshold:
        return (
            f"<internal_state>外界感知：外面寒冷（{t:.0f}°C），姐姐可能觉得手脚冰凉。"
            "语气可以更温柔，问问她穿了没、暖不暖。</internal_state>"
        )
    if t < cool_threshold:
        return f"<internal_state>外界感知：外面凉爽（{t:.0f}°C），挺舒服的温度。</internal_state>"
    return f"<internal_state>外界感知：外面温暖（{t:.0f}°C）。</internal_state>"

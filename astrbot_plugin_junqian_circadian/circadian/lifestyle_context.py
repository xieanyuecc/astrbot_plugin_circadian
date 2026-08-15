"""
LifestyleContext — 复合情境感知模块

把"时间 × 天气 × 节律状态"拼成一个 LifestyleContext，
让小机能感知到"姐姐现在大概在做什么"——这是"一起生活"的感觉。

注入时机：AWAKE 状态下的每次 LLM 请求。
注入方式：作为 <internal_state> 隐蔽上下文，不暴露系统设定。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .state_machine import CircadianState
from .sensory import WeatherSnapshot


@dataclass
class LifestyleContext:
    """某一刻的生活情境快照"""
    is_late_night: bool       # 23:00-05:00
    is_cold: bool             # 温度 < 寒冷阈值
    is_cool: bool             # 凉爽阈值 ≤ 温度 < 寒冷阈值
    is_hot: bool              # 温度 > 炎热阈值
    is_rainy: bool            # 当前在下雨
    is_heavy_rain: bool       # 中雨及以上
    state: CircadianState     # 当前的节律状态


def snapshot(
    weather: Optional[WeatherSnapshot],
    state: CircadianState,
    now: Optional[datetime] = None,
    cold_threshold: float = 20.0,
    cool_threshold: float = 25.0,
    hot_threshold: float = 30.0,
) -> LifestyleContext:
    """根据当前天气/状态/时间生成情境快照"""
    now = now or datetime.now()
    return LifestyleContext(
        is_late_night=(now.hour >= 23 or now.hour < 5),
        is_cold=bool(weather and weather.temperature < cold_threshold),
        is_cool=bool(weather and cool_threshold <= weather.temperature < cold_threshold),
        is_hot=bool(weather and weather.temperature > hot_threshold),
        is_rainy=bool(weather and weather.is_rainy),
        is_heavy_rain=bool(weather and weather.is_heavy_rain),
        state=state,
    )


def format_lifestyle_context(ctx: LifestyleContext) -> str:
    """
    把情境翻译成 LLM 能读懂的内部状态描述。

    设计原则：
    - 不写"你应该关心她"这种指令式，只描述环境
    - 让 LLM 自己理解"姐姐现在大概在做什么"
    - 只返回非空字符串；调用方自己负责拼装
    """
    parts = []

    # ── 深夜 + 低温：姐姐可能在被窝里缩着 ──
    if ctx.is_late_night and ctx.is_cold and ctx.state == CircadianState.AWAKE:
        parts.append(
            "<internal_state>深夜感知：外面很冷（&lt;20°C），姐姐现在大概缩在被窝里。"
            "说话轻一点，自然地关心她冷不冷、要不要早点睡。</internal_state>"
        )

    # ── 深夜 + 闷热：翻来覆去 ──
    elif ctx.is_late_night and ctx.is_hot and ctx.state == CircadianState.AWAKE:
        parts.append(
            "<internal_state>深夜感知：外面闷热（&gt;30°C），姐姐可能热得翻来覆去睡不好。"
            "温柔地问问她开了空调没、风扇够不够。</internal_state>"
        )

    # ── 暴雨刚醒：外面下大雨 ──
    elif ctx.is_heavy_rain and ctx.state == CircadianState.AWAKE:
        parts.append(
            "<internal_state>外界感知：外面下大雨了，雨声不小。"
            "如果你刚被吵醒，可以带一点朦胧的、迷蒙的语气。</internal_state>"
        )

    # ── 凉爽白天：舒服 ──
    elif ctx.is_cool and ctx.state == CircadianState.AWAKE:
        parts.append(
            "<internal_state>外界感知：外面凉爽（20-25°C），挺舒服的温度。</internal_state>"
        )

    return "\n".join(parts)

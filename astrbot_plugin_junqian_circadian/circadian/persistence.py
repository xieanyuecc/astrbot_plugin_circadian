"""
CircadianPersistence — 持久化模块
封装 KV 存储键的读写操作
"""
from typing import Optional, Any
import json

from .. import logger

# KV 存储键名
CIRCADIAN_STATE_KEY = "circadian_state"
EMOTIONAL_STATE_KEY = "emotional_state"
DREAM_MEMORY_KEY = "dream_memory"
PENDING_DREAM_KEY = "pending_dream"
SEMI_AWAKE_LAMBDA_KEY = "semi_awake_lambda"
WEATHER_SNAPSHOT_KEY = "weather_snapshot"
LOCATION_KEY = "location"


class CircadianPersistence:
    """封装插件 KV 存储操作"""

    def __init__(self, star_instance):
        self._star = star_instance

    # --- 状态机状态 ---
    async def save_state(self, state_data: dict):
        await self._put(CIRCADIAN_STATE_KEY, state_data)

    async def load_state(self) -> Optional[dict]:
        return await self._get(CIRCADIAN_STATE_KEY)

    # --- 情绪状态 ---
    async def save_emotional_state(self, emotional_data: dict):
        await self._put(EMOTIONAL_STATE_KEY, emotional_data)

    async def load_emotional_state(self) -> Optional[dict]:
        return await self._get(EMOTIONAL_STATE_KEY)

    # --- 梦境 ---
    async def save_dream(self, dream_text: str):
        await self._put(DREAM_MEMORY_KEY, dream_text)

    async def load_dream(self) -> Optional[str]:
        return await self._get(DREAM_MEMORY_KEY)

    async def save_pending_dream(self, pending: bool):
        await self._put(PENDING_DREAM_KEY, pending)

    async def load_pending_dream(self) -> bool:
        val = await self._get(PENDING_DREAM_KEY)
        return bool(val) if val is not None else False

    # --- 半醒 λ(t) ---
    async def save_lambda(self, lambda_value: float):
        await self._put(SEMI_AWAKE_LAMBDA_KEY, lambda_value)

    async def load_lambda(self) -> float:
        val = await self._get(SEMI_AWAKE_LAMBDA_KEY)
        return float(val) if val is not None else 0.3

    # --- 天气快照 ---
    async def save_weather(self, weather_dict: dict):
        await self._put(WEATHER_SNAPSHOT_KEY, weather_dict)

    async def load_weather(self) -> Optional[dict]:
        return await self._get(WEATHER_SNAPSHOT_KEY)

    # --- 所在地 ---
    async def save_location(self, location: str):
        await self._put(LOCATION_KEY, location)

    async def load_location(self) -> Optional[str]:
        val = await self._get(LOCATION_KEY)
        return val if isinstance(val, str) else None

    # --- 内部方法 ---
    async def _put(self, key: str, value: Any):
        try:
            await self._star.put_kv_data(key, value)
        except Exception as e:
            logger.error(f"[Circadian] Failed to save {key}: {e}")

    async def _get(self, key: str, default: Any = None) -> Any:
        try:
            val = await self._star.get_kv_data(key, default)
            return val
        except Exception as e:
            logger.error(f"[Circadian] Failed to load {key}: {e}")
            return default

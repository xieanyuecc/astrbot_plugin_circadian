"""
DreamLog — 梦境日志（v0.4.2 梦境记忆隔离）
三层隔离的物理层：梦境内容单独存盘（独立 KV key），与 livingmemory / 互动缓冲物理隔离。

三层隔离原则（与主人敲定的 v0.4 方向）：
1. 物理层：dream_log 独立存储，不进 livingmemory，不进互动缓冲
2. 逻辑层：每条记录带 source="dream"，注入 prompt 时标明"这是梦"
3. 用途层：梦境只影响情绪氛围，不作为事实被检索使用

回忆频率：人每晚都做梦，但大部分醒来就忘了。
每个梦生成时掷骰（roll_recall）决定醒来是否记得；
没被记得的只留在日志里，不注入 prompt、不呈现给用户。
"""
import random
from datetime import date
from typing import Dict, List, Optional

SOURCE_DREAM = "dream"


def roll_recall(recall_rate: float) -> bool:
    """醒来时是否记得这个梦。recall_rate 会被夹到 [0, 1]。"""
    rate = max(0.0, min(1.0, recall_rate))
    return random.random() < rate


class DreamLog:
    """梦境日志容器（与 MemoryBuffer 对称：load / append / 滚动截断）"""

    def __init__(self, persistence, max_items: int = 30):
        self._persistence = persistence
        self._max_items = max(1, int(max_items))
        self._items: List[Dict] = []

    async def load(self):
        data = await self._persistence.load_dream_log()
        self._items = list(data) if isinstance(data, list) else []

    async def append(self, dream_text: str, recalled: bool, day: str = "") -> Dict:
        """追加一条梦境记录（day 缺省用今天），滚动截断到 max_items。"""
        entry = {
            "date": day or date.today().isoformat(),
            "text": dream_text,
            "source": SOURCE_DREAM,  # 逻辑层标记：恒为 dream
            "recalled": bool(recalled),
        }
        self._items.append(entry)
        if len(self._items) > self._max_items:
            self._items = self._items[-self._max_items:]
        await self._persistence.save_dream_log(self._items)
        return entry

    def latest(self) -> Optional[Dict]:
        """最近一条梦境记录（调试 / 状态展示用）"""
        return self._items[-1] if self._items else None

    def __len__(self):
        return len(self._items)

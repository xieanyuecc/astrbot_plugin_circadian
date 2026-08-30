"""
MemoryBuffer — 近期互动缓冲
滚动保存最近 N 轮对话（用户说了什么 + AI 回了什么），
作为梦境生成和"醒来心情"的真实素材。

设计动机：情绪和梦应当从真实互动中长出来，而不是 LLM 凭空编造。

v0.4.2 梦境隔离：prompt 被注入过梦境残片的那轮对话记为 source="dream"，
recent_text 默认将其过滤——梦的复述不能变成下一晚的梦素材（防止梦自我繁殖）。
"""
from typing import List, Dict
from datetime import datetime

MAX_ITEMS = 40          # 缓冲区上限（约 40 轮）
MAX_TEXT_LEN = 120      # 每条文本截断长度


class MemoryBuffer:
    def __init__(self, persistence):
        self._persistence = persistence
        self._items: List[Dict] = []

    async def load(self):
        data = await self._persistence.load_memory()
        self._items = list(data) if isinstance(data, list) else []

    async def append(self, user_text: str, reply_text: str, source: str = "real"):
        """记录一轮对话。空消息跳过。
        source: real=真实互动 / dream=梦境注入轮的复述（recent_text 默认过滤）"""
        user_text = (user_text or "").strip()
        reply_text = (reply_text or "").strip()
        if not user_text and not reply_text:
            return
        self._items.append({
            "t": datetime.now().strftime("%m-%d %H:%M"),
            "u": user_text[:MAX_TEXT_LEN],
            "r": reply_text[:MAX_TEXT_LEN],
            "s": source,
        })
        if len(self._items) > MAX_ITEMS:
            self._items = self._items[-MAX_ITEMS:]
        await self._persistence.save_memory(self._items)

    def recent_text(self, n: int = 8, include_dream: bool = False) -> str:
        """最近 n 轮**真实**对话的纯文本，供 LLM 当素材。
        默认过滤 source=dream 的条目（无 s 字段的旧记录视为真实互动）；
        include_dream=True 时不过滤。"""
        items = [
            it for it in self._items
            if include_dream or it.get("s", "real") != "dream"
        ]
        lines = []
        for item in items[-n:]:
            if item.get("u"):
                lines.append(f"[{item['t']}] 用户：{item['u']}")
            if item.get("r"):
                lines.append(f"[{item['t']}] AI：{item['r']}")
        return "\n".join(lines)

"""
LivingMemoryBridge — 桥接 livingmemory 插件的 FunctionTool

机制说明（v0.2.0 重写）：
livingmemory 插件通过 `context.add_llm_tools()` 注册了两个 AstrBot 原生 LLM 工具：
- `recall_long_term_memory`：根据 query 检索长期记忆
- `memorize_long_term_memory`：把内容写入长期记忆

正确的调用方式：
1. `context.get_llm_tool_manager().get_func(name)` 拿到 FunctionTool 对象
2. 构造 `ContextWrapper(context=AstrAgentContext(context=context, event=event))`
3. `await tool.call(wrapper, **kwargs)` 同步拿到结果（JSON 字符串）

注意事项：
- livingmemory 内部依赖 event（决定 session/persona 作用域），无 event 时无法工作
- livingmemory 未安装时工具未注册，方法返回 False / 空串，调用方需做降级
- 背景任务（如时钟 ticker 触发梦境）没有 event，需要使用最近一次缓存的 event
"""
import json
from typing import Optional, List

from astrbot import logger


class LivingMemoryBridge:
    """桥接 livingmemory 插件的 recall/memorize 工具"""

    RECALL_TOOL = "recall_long_term_memory"
    MEMORIZE_TOOL = "memorize_long_term_memory"

    def __init__(self, context):
        self._ctx = context

    # ── 工具查找 ──

    def _get_tool(self, name: str):
        """从 AstrBot 拿到已注册的 FunctionTool，未注册返回 None"""
        try:
            mgr = self._ctx.get_llm_tool_manager()
            return mgr.get_func(name)
        except Exception as e:
            logger.warning(f"[Circadian] get_llm_tool_manager failed: {e}")
            return None

    @staticmethod
    def _make_wrapper(context, event):
        """构造 livingmemory 工具所需的 ContextWrapper"""
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.astr_agent_context import AstrAgentContext
        agent_ctx = AstrAgentContext(context=context, event=event)
        return ContextWrapper(context=agent_ctx)

    def is_available(self) -> bool:
        """livingmemory recall 工具是否已注册（用户是否安装了 livingmemory）"""
        return self._get_tool(self.RECALL_TOOL) is not None

    # ── 召回 ──

    async def recall(self, query: str, k: int = 5, event=None) -> str:
        """
        调 livingmemory recall_long_term_memory。

        event: 必需，livingmemory 内部需要 event 决定 session/persona 作用域。
        返回压缩后的记忆摘要文本，给下游 LLM 直接读。
        """
        if event is None:
            logger.warning("[Circadian] recall skipped: no event (background call)")
            return ""

        tool = self._get_tool(self.RECALL_TOOL)
        if tool is None:
            logger.warning("[Circadian] recall skipped: livingmemory not installed")
            return ""

        wrapper = self._make_wrapper(self._ctx, event)
        try:
            raw = await tool.call(wrapper, query=query, k=k, include_source=False)
            text = _extract_tool_text(raw)
            summary = _summarize_recall(text, k)
            logger.info(
                f"[Circadian] livingmemory recall: k={k}, {len(summary)} chars, "
                f"preview={summary[:120]}"
            )
            return summary
        except Exception as e:
            logger.error(f"[Circadian] livingmemory recall failed: {e}", exc_info=True)
            return ""

    async def recall_for_emotional_context(self, event=None) -> str:
        """为情绪涌现做的 recall：查询近期与情绪/互动氛围相关的记忆"""
        return await self.recall(
            query="用户最近的情绪状态、互动的氛围、有没有特别的事情发生",
            k=8,
            event=event,
        )

    async def recall_for_dream(self, event=None) -> str:
        """为梦境生成做的 recall：查询近期值得记住的片段"""
        return await self.recall(
            query="最近发生的事、用户的情绪波动、值得记住的片段",
            k=12,
            event=event,
        )

    # ── 记忆写入 ──

    async def memorize(
        self,
        memory: str,
        topics: Optional[List[str]] = None,
        importance: float = 0.7,
        sentiment: str = "neutral",
        reason: str = "",
        event=None,
    ) -> bool:
        """调 livingmemory memorize_long_term_memory 写入一条记忆"""
        if event is None:
            logger.warning("[Circadian] memorize skipped: no event")
            return False

        tool = self._get_tool(self.MEMORIZE_TOOL)
        if tool is None:
            logger.warning("[Circadian] memorize skipped: livingmemory not installed")
            return False

        wrapper = self._make_wrapper(self._ctx, event)
        try:
            await tool.call(
                wrapper,
                memory=memory,
                topics=topics or [],
                importance=importance,
                sentiment=sentiment,
                reason=reason or "君迁生理节律自动归档",
            )
            logger.info(f"[Circadian] livingmemory memorize: {memory[:80]}")
            return True
        except Exception as e:
            logger.error(f"[Circadian] livingmemory memorize failed: {e}", exc_info=True)
            return False


# ── 工具结果解析 ──

def _extract_tool_text(raw) -> str:
    """工具返回可能是 str 或 MCP CallToolResult，统一转成字符串"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    # MCP CallToolResult: content 是 list[TextContent | ImageContent | ...]
    content = getattr(raw, "content", None)
    if content:
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return str(raw)


def _summarize_recall(raw: str, k: int) -> str:
    """
    recall 工具返回的 JSON 形如
    `{"query", "count", "results": [{"id", "content", "score", ...}]}`
    转成 LLM 易读的紧凑文本。
    """
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 不是 JSON，原样返回
        return raw

    if isinstance(data, dict) and data.get("error"):
        logger.warning(f"[Circadian] recall returned error: {data['error']}")
        return ""

    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return "(无相关记忆)"

    lines = []
    for i, item in enumerate(results[:k], 1):
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        score = item.get("score")
        prefix = f"[{i}]"
        if score is not None:
            try:
                prefix += f"(score={float(score):.2f})"
            except (TypeError, ValueError):
                pass
        if content:
            lines.append(f"{prefix} {content}")
    return "\n".join(lines) if lines else "(无相关记忆)"
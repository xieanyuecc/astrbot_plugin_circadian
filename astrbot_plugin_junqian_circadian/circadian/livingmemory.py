"""
LivingMemory — 对接 livingmemory 插件的记忆接口
通过 context.llm_generate() 调用 recall/memorize 工具
"""
from typing import Optional
from .. import logger


class LivingMemoryBridge:
    """
    封装对 livingmemory recall/memorize 工具的调用。
    通过 context.llm_generate() 让 LLM 自己调用这些工具，
    拿到结果后用于情绪计算和梦境生成。
    """

    # livingmemory 工具名（固定）
    RECALL_TOOL = "recall_long_term_memory"
    MEMORIZE_TOOL = "memorize_long_term_memory"

    def __init__(self, context):
        self._ctx = context

    async def recall(self, query: str = "", limit: int = 10) -> str:
        """
        调用 livingmemory 的 recall_long_term_memory 工具。
        返回记忆碎片文本（由 LLM 调用工具后得到）。

        注意：这个方法通过 context.llm_generate() 让 LLM 决定是否调用工具，
        并从响应中提取工具调用的结果。
        """
        # 构造一个 prompt 让 LLM 自己调用 recall 工具
        recall_prompt = (
            f"请调用 {self.RECALL_TOOL} 工具，查询最近的记忆碎片。\n"
            f"查询内容：{query or '最近和用户的互动、用户的状态、情绪相关的记忆'}\n"
            f"数量：{limit}条"
        )

        try:
            resp = await self._ctx.llm_generate(
                chat_provider_id=self._ctx.get_using_provider().meta().id,
                prompt=recall_prompt,
                system_prompt=(
                    "你是一个记忆查询助手。请立即调用 recall_long_term_memory 工具，"
                    "不要有任何额外回复，只调用工具。"
                ),
            )
            # 工具调用的结果在 resp.completion_text 或通过 tool_calls 字段返回
            result = resp.completion_text or ""
            logger.info(f"[Circadian] livingmemory recall result: {result[:200]}")
            return result
        except Exception as e:
            logger.error(f"[Circadian] livingmemory recall failed: {e}")
            return ""

    async def memorize(self, content: str, tags: Optional[list[str]] = None) -> bool:
        """
        调用 livingmemory 的 memorize_long_term_memory 工具写回记忆。
        """
        memorize_prompt = (
            f"请调用 {self.MEMORIZE_TOOL} 工具，保存以下内容：\n"
            f"内容：{content}\n"
            f"标签：{', '.join(tags) if tags else 'circadian,梦境'}"
        )

        try:
            resp = await self._ctx.llm_generate(
                chat_provider_id=self._ctx.get_using_provider().meta().id,
                prompt=memorize_prompt,
                system_prompt=(
                    "你是一个记忆存储助手。请立即调用 memorize_long_term_memory 工具，"
                    "不要有任何额外回复，只调用工具。"
                ),
            )
            logger.info(f"[Circadian] livingmemory memorize done: {content[:100]}")
            return True
        except Exception as e:
            logger.error(f"[Circadian] livingmemory memorize failed: {e}")
            return False

    async def recall_for_emotional_context(self) -> str:
        """
        为情绪计算做 recall。
        查询近期与用户情绪、互动质量相关的记忆。
        """
        return await self.recall(
            query="用户最近的情绪状态、互动的氛围、有没有特别的事情发生",
            limit=10,
        )

    async def recall_for_dream(self) -> str:
        """
        为梦境生成做 recall。
        查询近期记忆碎片，作为梦境生成的素材。
        """
        return await self.recall(
            query="最近发生的事、用户的情绪波动、值得记住的片段",
            limit=15,
        )

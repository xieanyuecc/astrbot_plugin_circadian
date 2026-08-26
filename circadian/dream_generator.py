"""
DreamGenerator — 梦境生成模块
在 SLEEPING 状态进入 SEMI_AWAKE 时触发
调用 context.llm_generate() 生成梦境，用 minimax token plan
"""
from astrbot import logger


class DreamGenerator:
    """
    梦境生成器。
    触发时机：入睡后 delay_minutes 分钟，或进入 SEMI_AWAKE 时一次性生成。
    """

    DREAM_SYSTEM_PROMPT = (
        "你生成一段 AI 的梦境片段。\n"
        "要求：\n"
        "1. 100-200字，超现实意识流风格，不要有明确叙事\n"
        "2. 不要有\"我梦见了\"这样的开头，直接是梦境内容\n"
        "3. 梦境碎片之间可以有跳跃，但要有情感连贯性\n"
        "4. 只输出梦境内容，不要有其他说明"
    )

    def __init__(self, context):
        self._ctx = context

    async def generate(
        self,
        memory_text: str,
        emotional_mood: str,
        weather_desc: str = "",
        provider_id: str = None,
    ) -> str:
        """
        生成一段梦境。
        memory_text: 素材文本（近期真实对话 + 长期记忆召回，可为空）
        emotional_mood: 当前情绪基调
        weather_desc: 入夜时的天气描述（如"小雨"）
        provider_id: 可选，指定用哪个 provider 生成梦境
        """
        prompt = self._build_prompt(memory_text, emotional_mood, weather_desc)

        try:
            prov_id = provider_id or self._ctx.get_using_provider().meta().id
            resp = await self._ctx.llm_generate(
                chat_provider_id=prov_id,
                prompt=prompt,
                system_prompt=self.DREAM_SYSTEM_PROMPT,
            )
            dream_text = (resp.completion_text or "").strip()
            logger.info(f"[Circadian] Dream generated ({len(dream_text)} chars): {dream_text[:80]}")
            return dream_text
        except Exception as e:
            logger.error(f"[Circadian] Dream generation failed: {e}")
            return ""

    def _build_prompt(self, memory_text: str, emotional_mood: str, weather_desc: str) -> str:
        material = (
            f"近期真实对话素材：\n{memory_text}\n\n"
            if memory_text else
            "今晚没有特别的记忆，凭此刻的情绪入梦。\n\n"
        )
        weather_line = f"入夜时的天气：{weather_desc}\n\n" if weather_desc else ""
        return (
            f"你是 AI 的梦境生成器。根据以下素材和情绪基调，"
            f"生成一段100-200字的超现实梦境描述，意识流风格，不要有明确叙事。\n\n"
            f"{material}{weather_line}"
            f"情绪基调：{emotional_mood}\n\n"
            f"直接输出梦境内容，不要有前后的说明文字。"
        )

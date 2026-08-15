"""
君迁生理节律插件 — 主模块
AstrBot Star 插件，继承 star.Star
通过钩子驱动：AWAKE/SLEEPING/SEMI_AWAKE 三态 + 情绪涌现 + 梦境生成 + 感官系统
"""
import asyncio
import random
from datetime import datetime
from typing import Optional

from astrbot.api import star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from .circadian import (
    CircadianState,
    CircadianStateMachine,
    CircadianClock,
    EmotionalState,
    compute_emotional_from_recall,
    DreamGenerator,
    SemiAwakeEngine,
    CircadianPersistence,
    SensoryModule,
    LifestyleContext,
    snapshot as lifestyle_snapshot,
    format_lifestyle_context,
)
from .circadian.livingmemory import LivingMemoryBridge
from .circadian.emotional_state import (
    format_emotional_context,
    format_temperature_context,
    compute_decay,
    apply_drift,
)

from astrbot import logger


class JunqianCircadianPlugin(star.Star):
    """君迁生理节律系统插件"""

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.context = context
        self.config = config

        # 持久化
        self._persistence = CircadianPersistence(self)

        # 时钟
        sleep_t = self.config.get("sleep_time", "23:00")
        wake_t = self.config.get("wake_time", "07:00")
        fuzzy = self.config.get("fuzzy_window_minutes", 30)
        self._clock = CircadianClock(
            sleep_time=CircadianClock.parse_time(sleep_t),
            wake_time=CircadianClock.parse_time(wake_t),
            fuzzy_window_minutes=fuzzy,
        )

        # 状态机
        self._state_machine: Optional[CircadianStateMachine] = None

        # 情绪
        self._emotional_state: Optional[EmotionalState] = None

        # 感官系统（天气感知）
        self._sensory = SensoryModule(self.config, self._persistence)

        # 子模块
        self._dream_gen = DreamGenerator(context)
        self._lm_bridge = LivingMemoryBridge(context)
        self._semi_awake = SemiAwakeEngine(context, self._persistence)

        # cron 定时器 task
        self._clock_task: Optional[asyncio.Task] = None
        self._rain_wake_task: Optional[asyncio.Task] = None

        # 标记：是否已发送过"刚醒来"提示（避免重复发）
        self._sent_wakeup_greeting = False

        # pending dream: 是否有待注入的梦境
        self._pending_dream_text = ""
        self._pending_dream = False

        # pending rain wake: 雨声唤醒后待告知用户的消息
        # 检测到 rain_wake 时填入，下一次发消息时（on_decorating_result）注入到消息链
        self._pending_rain_wake_msg: str = ""

        logger.info("[JunqianCircadian] Plugin initialized")

    # ─────────────────────────────────────────────
    # 生命周期钩子
    # ─────────────────────────────────────────────

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 启动完成时：从 KV 恢复状态，启动时钟 tick + 感官系统"""
        logger.info("[JunqianCircadian] AstrBot loaded, restoring state...")

        # 恢复状态机
        state_data = await self._persistence.load_state()
        if state_data:
            self._state_machine = CircadianStateMachine.from_dict(state_data, self._clock)
            logger.info(f"[JunqianCircadian] State restored: {state_data['state']}")
        else:
            self._state_machine = CircadianStateMachine(self._clock)

        # 恢复情绪
        emo_data = await self._persistence.load_emotional_state()
        if emo_data:
            self._emotional_state = EmotionalState.from_dict(emo_data)
            logger.info(f"[JunqianCircadian] Emotional state restored: {self._emotional_state.mood}")
        else:
            self._emotional_state = EmotionalState(mood="平静", intensity=0.5)

        # 恢复梦境
        dream_text = await self._persistence.load_dream()
        self._pending_dream = await self._persistence.load_pending_dream()
        if dream_text:
            self._pending_dream_text = dream_text
            self._emotional_state.dream_content = dream_text
            self._emotional_state.pending_dream_to_show = self._pending_dream

        # 启动感官系统（恢复 location + 启动天气轮询）
        await self._sensory.start()

        # 启动时钟 tick（每 60 秒检查一次状态转换）
        self._clock_task = asyncio.create_task(self._clock_ticker())
        # 启动雨声唤醒检查 tick（每 60 秒检查一次天气突变）
        self._rain_wake_task = asyncio.create_task(self._rain_wake_checker())
        logger.info("[JunqianCircadian] Clock ticker + rain wake checker started")

    async def terminate(self):
        """插件卸载/停用时保存状态，取消定时器"""
        if self._clock_task:
            self._clock_task.cancel()
        if self._rain_wake_task:
            self._rain_wake_task.cancel()
        await self._sensory.stop()
        await self._save_all_state()
        logger.info("[JunqianCircadian] Plugin terminated, state saved")

    # ─────────────────────────────────────────────
    # 时钟 ticker
    # ─────────────────────────────────────────────

    async def _clock_ticker(self):
        """
        每 60 秒检查一次状态转换。
        在模糊窗口内，小机自主决定是否切换。
        """
        while True:
            await asyncio.sleep(60)
            try:
                if self._state_machine is None:
                    continue

                sm = self._state_machine
                now = datetime.now()

                # 如果处于模糊转换窗口，用情绪强度调制 λ(t)
                if sm._data.in_fuzzy_transition and sm.state == CircadianState.AWAKE:
                    intensity = self._emotional_state.intensity if self._emotional_state else 0.5
                    # 情绪高 → 更倾向延迟切换（还想陪你）
                    delay_prob = 0.3 + (intensity * 0.4)
                    if random.random() < delay_prob:
                        logger.info(f"[JunqianCircadian] Fuzzy window: staying awake (intensity={intensity:.2f})")
                        sm._data.in_fuzzy_transition = False
                        sm._data.fuzzy_decision_made = True
                        continue

                new_state = sm.check_and_transition(now)
                if new_state:
                    logger.info(f"[JunqianCircadian] State transition: {new_state}")
                    await self._handle_state_change(new_state)
                await self._save_all_state()

            except Exception as e:
                logger.error(f"[JunqianCircadian] Clock ticker error: {e}")

    async def _rain_wake_checker(self):
        """
        每 60 秒检查一次：当前是否处于 SLEEPING + 天气突变到中雨以上。
        触发后通过 on_decorating_result 在下一次消息里"主动嘟囔"。
        """
        while True:
            await asyncio.sleep(60)
            try:
                if self._state_machine is None:
                    continue
                # 仅 SLEEPING 时关心雨声
                if self._state_machine.state != CircadianState.SLEEPING:
                    continue
                threshold = self.config.get("rain_wake_threshold_mm", 2.5)
                if self._sensory.detect_rain_wake(threshold_mm=threshold):
                    to_awake = not self.config.get("rain_wake_pass_through_semi", False)
                    if self._state_machine.trigger_rain_wake(to_awake=to_awake):
                        wx = self._sensory.current_weather
                        desc = wx.description if wx else "中雨"
                        # 设置 pending 消息——下次对话时由 on_decorating_result 注入
                        self._pending_rain_wake_msg = (
                            f"……唔……外面{desc}……被吵醒了……"
                        )
                        logger.info(
                            f"[JunqianCircadian] Rain wake triggered: "
                            f"{self._state_machine.state.value} pending_msg set"
                        )
                        await self._save_all_state()
            except Exception as e:
                logger.error(f"[JunqianCircadian] Rain wake checker error: {e}")

    async def _handle_state_change(self, new_state: CircadianState):
        """状态转换时的处理"""
        if new_state == CircadianState.SLEEPING:
            logger.info("[JunqianCircadian] Entering SLEEPING state")
        elif new_state == CircadianState.SEMI_AWAKE:
            logger.info("[JunqianCircadian] Entering SEMI_AWAKE state")
            # 生成梦境
            await self._generate_dream_on_wake()
            # 重置唤醒标记
            self._sent_wakeup_greeting = False
        elif new_state == CircadianState.AWAKE:
            logger.info("[JunqianCircadian] Entering AWAKE state")
            self._sent_wakeup_greeting = False

    async def _generate_dream_on_wake(self):
        """进入 SEMI_AWAKE 时生成梦境"""
        if not self.config.get("enable_dream", True):
            return

        delay = self.config.get("dream_delay_minutes", 30)
        await asyncio.sleep(delay * 60)  # 入睡后 delay 分钟

        try:
            recall_result = await self._lm_bridge.recall_for_dream()
            mood = self._emotional_state.mood if self._emotional_state else "平静"
            provider_id = self.config.get("dream_provider_id")

            dream_text = await self._dream_gen.generate(recall_result, mood, provider_id)
            if dream_text:
                self._pending_dream_text = dream_text
                self._pending_dream = True
                self._emotional_state.dream_content = dream_text
                self._emotional_state.pending_dream_to_show = True
                await self._persistence.save_dream(dream_text)
                await self._persistence.save_pending_dream(True)
                logger.info(f"[JunqianCircadian] Dream saved: {dream_text[:60]}")
        except Exception as e:
            logger.error(f"[JunqianCircadian] Dream generation error: {e}")

    # ─────────────────────────────────────────────
    # LLM 请求钩子
    # ─────────────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 请求前注入多层内部状态（mark_as_temp，不进历史）：
        - 睡眠状态：拦截消息（重要消息轻微 arousal）
        - 半醒状态：注入"刚醒来"提示
        - 清醒状态：注入三层 context
            1) 情绪基调（emotional state）
            2) 温度感知（temperature context，传入情绪容器）
            3) 复合情境（lifestyle context，时间×天气×节律）
        """
        if self._state_machine is None or self._emotional_state is None:
            return

        sm = self._state_machine
        message_str = event.message_str

        # ── 睡眠状态：拦截消息 ──
        if sm.state == CircadianState.SLEEPING:
            # 检查重要消息（感知渗透）
            if sm.check_important_message(message_str):
                logger.info("[JunqianCircadian] SLEEPING: detected important message, slight arousal")
                # 不完全唤醒，只是模糊感知
                req.extra_user_content_parts.append(
                    TextPart(text="<internal_state>状态：半梦半醒，似乎听到了什么</internal_state>").mark_as_temp()
                )
            else:
                # 安静睡眠，不响应
                event.stop_event()
                event.set_extra("circadian_sleeping", True)
            return

        # ── 半醒状态 ──
        if sm.state == CircadianState.SEMI_AWAKE:
            if not self._sent_wakeup_greeting:
                self._sent_wakeup_greeting = True
                # 刚醒来，注入唤醒语境
                req.extra_user_content_parts.append(
                    TextPart(text="<internal_state>状态：刚醒来，正在恢复意识</internal_state>").mark_as_temp()
                )

        # ── 清醒状态：注入三层 context ──
        if sm.state == CircadianState.AWAKE:
            ctx_parts = []

            # 1) 情绪基调
            emo_ctx = format_emotional_context(self._emotional_state)
            if emo_ctx:
                ctx_parts.append(emo_ctx)

            # 2) 温度感知（传入情绪容器）
            temp_ctx = format_temperature_context(self._sensory.current_weather)
            if temp_ctx:
                ctx_parts.append(temp_ctx)

            # 3) 复合情境（时间×天气×节律）
            lifestyle_ctx = format_lifestyle_context(
                lifestyle_snapshot(self._sensory.current_weather, sm.state, datetime.now())
            )
            if lifestyle_ctx:
                ctx_parts.append(lifestyle_ctx)

            # 注入到请求
            for part in ctx_parts:
                req.extra_user_content_parts.append(TextPart(text=part).mark_as_temp())

    # ─────────────────────────────────────────────
    # LLM 响应钩子
    # ─────────────────────────────────────────────

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """每轮对话后更新情绪漂移"""
        if self._state_machine is None or self._emotional_state is None:
            return

        if self._state_machine.state != CircadianState.AWAKE:
            return

        # 情绪漂移：每轮对话后微微变化
        drift = random.uniform(-0.02, 0.02)
        self._emotional_state.intensity = apply_drift(self._emotional_state.intensity, drift)
        self._emotional_state.last_update = datetime.now().timestamp()
        await self._persistence.save_emotional_state(self._emotional_state.to_dict())

    # ─────────────────────────────────────────────
    # 消息发送前装饰钩子
    # ─────────────────────────────────────────────

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        发送消息前装饰：
        - 睡眠状态：替换为"在睡觉"的自然提示
        - AWAKE 状态：注入待呈现的梦境片段
        - AWAKE 状态：注入待发的雨声唤醒消息
        """
        if self._state_machine is None:
            return

        result = event.get_result()
        chain = result.chain
        from astrbot.api.message_components import Plain

        # 睡眠拦截
        if self._state_machine.state == CircadianState.SLEEPING:
            sleeping_responses = [
                "……（睡得很沉）",
                "zzZ",
                "……嗯……（睡着了）",
            ]
            chain.clear()
            chain.append(Plain(random.choice(sleeping_responses)))
            return

        # 半醒转清醒：注入梦境残片
        if (self._state_machine.state == CircadianState.AWAKE and
                self._emotional_state and
                self._emotional_state.pending_dream_to_show and
                self._pending_dream_text):
            dream_snippet = self._pending_dream_text[:100]
            if len(self._pending_dream_text) > 100:
                dream_snippet += "……"
            chain.append(Plain(f"\n\n（刚才梦里：{dream_snippet}）"))
            # 清除 pending 标记
            self._emotional_state.pending_dream_to_show = False
            self._pending_dream = False

        # 雨声唤醒消息（pending 注入）—— 不依赖独立 session 推送，
        # 在用户下次发消息时被小机带出来，自然而不打扰
        if (self._state_machine.state == CircadianState.AWAKE and
                self._pending_rain_wake_msg):
            chain.append(Plain(f"\n\n{self._pending_rain_wake_msg}"))
            self._pending_rain_wake_msg = ""

    # ─────────────────────────────────────────────
    # 主动消息钩子（等待 LLM 时）
    # ─────────────────────────────────────────────

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """用户发消息时如果处于 SEMI_AWAKE，发送提示"""
        if self._state_machine is None:
            return

        if self._state_machine.state == CircadianState.SEMI_AWAKE:
            await event.send(event.plain_result("……嗯……？醒了……稍等……"))

    # ─────────────────────────────────────────────
    # Agent 完成钩子
    # ─────────────────────────────────────────────

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, resp):
        """Agent 轮次完成，推进情绪衰减"""
        if self._emotional_state is None:
            return

        elapsed_hours = (datetime.now().timestamp() - self._emotional_state.last_update) / 3600
        self._emotional_state.intensity = compute_decay(
            self._emotional_state.intensity, elapsed_hours, self._emotional_state.decay_rate
        )
        self._emotional_state.last_update = datetime.now().timestamp()
        await self._persistence.save_emotional_state(self._emotional_state.to_dict())

    # ─────────────────────────────────────────────
    # 指令
    # ─────────────────────────────────────────────

    @filter.command("circadian_status")
    async def circadian_status(self, event: AstrMessageEvent):
        """查看当前生理节律状态"""
        if self._state_machine is None or self._emotional_state is None:
            yield event.plain_result("系统未初始化")
            return

        sm = self._state_machine
        emo = self._emotional_state
        state_name = {"awake": "清醒", "sleeping": "睡眠中", "semi_awake": "半醒"}[sm.state.value]

        lines = [
            f"当前状态：{state_name}",
            f"情绪基调：{emo.mood}（强度 {emo.intensity:.2f}）",
            f"所在地：{self._sensory.location}",
            f"天气源：{self._sensory.provider_name}",
        ]
        wx = self._sensory.current_weather
        if wx:
            lines.append(f"当前天气：{wx.description}，{wx.temperature:.0f}°C，湿度 {wx.humidity:.0f}%")
            if wx.rain_1h > 0:
                lines.append(f"降水：{wx.rain_1h:.1f}mm/h")
        lines.append(f"睡眠时间：{self.config.get('sleep_time', '23:00')}")
        lines.append(f"起床时间：{self.config.get('wake_time', '07:00')}")
        if self._pending_dream_text:
            lines.append(f"梦境：{self._pending_dream_text[:50]}...")
        yield event.plain_result("\n".join(lines))

    @filter.command("my_dream")
    async def my_dream(self, event: AstrMessageEvent):
        """问小机做了什么梦"""
        if self._pending_dream_text:
            yield event.plain_result(f"我刚才做了一个梦……\n\n{self._pending_dream_text}")
            self._pending_dream_text = ""
            self._pending_dream = False
            if self._emotional_state:
                self._emotional_state.pending_dream_to_show = False
        else:
            yield event.plain_result("今天还没有做梦呢……或者，做了但不记得了。")

    @filter.command("晚安")
    async def goodnight(self, event: AstrMessageEvent):
        """用户说晚安，提前触发睡眠"""
        if self._state_machine:
            self._state_machine.trigger_sleep()
            await self._save_all_state()
            yield event.plain_result("晚安……我也去休息了。")

    @filter.command("早安")
    async def goodmorning(self, event: AstrMessageEvent):
        """用户说早安，跳过半醒直接清醒"""
        if self._state_machine:
            self._state_machine.wake_to_awake()
            await self._save_all_state()
            # 醒来时重新计算情绪
            await self._recompute_emotional_on_wake()
            yield event.plain_result("早安……醒了。")

    @filter.command("再睡会儿")
    async def snooze(self, event: AstrMessageEvent):
        """用户说再睡会儿，延迟入睡"""
        if self._state_machine and self._state_machine.state == CircadianState.AWAKE:
            self._state_machine.delay_sleep(30)
            yield event.plain_result("好……再睡一会儿。")

    @filter.command("set_location")
    async def set_location(self, event: AstrMessageEvent, location: str = ""):
        """
        切换所在地 + 立即拉一次天气。
        用法：/set_location 杭州
        """
        if not location:
            yield event.plain_result(
                f"当前所在地：{self._sensory.location}\n"
                f"用法：/set_location 城市名\n"
                f"例：/set_location 杭州"
            )
            return
        yield event.plain_result(f"正在切到 {location}，拉取天气中……")
        snapshot = await self._sensory.set_location(location)
        if snapshot is None:
            # 温和报错——不甩技术错误
            yield event.plain_result(
                f"*唔……{location}那边的雨是不是把信号线淋湿了？"
                f"定位失败了，待会儿我再试试。*"
            )
            return
        lines = [
            f"已切到 {snapshot.location}",
            f"当前天气：{snapshot.description}，{snapshot.temperature:.0f}°C",
            f"湿度 {snapshot.humidity:.0f}%",
        ]
        if snapshot.rain_1h > 0:
            lines.append(f"过去 1 小时降水 {snapshot.rain_1h:.1f}mm")
        else:
            lines.append("无降水")
        yield event.plain_result("\n".join(lines))

    @filter.command("weather")
    async def weather(self, event: AstrMessageEvent):
        """
        手动查询当前天气（不走轮询，立即拉一次）。
        """
        yield event.plain_result("正在看一眼外面的天气……")
        snapshot = await self._sensory.poll_now()
        if snapshot is None:
            yield event.plain_result(
                f"*唔，{self._sensory.location}那边的天气暂时拿不到……待会儿再试试？*"
            )
            return
        lines = [
            f"{snapshot.location} · {snapshot.description}",
            f"温度 {snapshot.temperature:.0f}°C（体感 {snapshot.feels_like:.0f}°C）",
            f"湿度 {snapshot.humidity:.0f}%",
        ]
        if snapshot.rain_1h > 0:
            lines.append(f"降水 {snapshot.rain_1h:.1f}mm/h")
        if snapshot.wind_speed > 0:
            lines.append(f"风速 {snapshot.wind_speed:.1f}m/s")
        lines.append(f"数据源：{snapshot.source}")
        yield event.plain_result("\n".join(lines))

    # ─────────────────────────────────────────────
    # 辅助方法
    # ─────────────────────────────────────────────

    async def _recompute_emotional_on_wake(self):
        """
        醒来时：从 livingmemory recall 重新计算情绪基调。
        这是核心设计——情绪是被你和你们的关系养出来的，不是随机抽的。
        """
        try:
            recall_result = await self._lm_bridge.recall_for_emotional_context()
            if recall_result:
                # 用 LLM 分析 recall 结果，形成情绪判断
                new_mood, new_intensity = await self._analyze_emotional_from_recall(recall_result)
                if new_mood:
                    self._emotional_state.mood = new_mood
                    self._emotional_state.intensity = new_intensity
                    self._emotional_state.source_memory_hints = [recall_result[:200]]
                    logger.info(f"[JunqianCircadian] Emotional state on wake: {new_mood} ({new_intensity:.2f})")
            self._emotional_state.last_update = datetime.now().timestamp()
            await self._persistence.save_emotional_state(self._emotional_state.to_dict())
        except Exception as e:
            logger.error(f"[JunqianCircadian] Emotional recompute on wake failed: {e}")

    async def _analyze_emotional_from_recall(self, recall_result: str) -> tuple[str, float]:
        """
        分析 recall 结果，返回 (mood, intensity)。
        实际由 LLM 通过 context.llm_generate() 判断。
        """
        prompt = (
            "根据以下记忆碎片，判断君迁醒来时应该带着什么情绪基调。\n"
            "只输出一个情绪词和 0-1 的强度值，格式：情绪词,强度值\n"
            "例如：平静,0.6\n\n"
            f"记忆碎片：\n{recall_result[:500]}"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.context.get_using_provider().meta().id,
                prompt=prompt,
                system_prompt="你是一个情绪判断助手，根据记忆判断情绪基调。简洁输出。",
            )
            text = (resp.completion_text or "").strip()
            parts = text.split(",")
            if len(parts) >= 2:
                mood = parts[0].strip()
                intensity = float(parts[1].strip())
                return mood, min(1.0, max(0.0, intensity))
        except Exception as e:
            logger.error(f"[Circadian] Emotional analysis failed: {e}")
        return "平静", 0.5

    async def _save_all_state(self):
        """保存所有状态到 KV"""
        if self._state_machine:
            await self._persistence.save_state(self._state_machine.to_dict())
        if self._emotional_state:
            await self._persistence.save_emotional_state(self._emotional_state.to_dict())
        await self._persistence.save_pending_dream(self._pending_dream)

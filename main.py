"""
生理节律插件 — 主模块
AstrBot Star 插件，继承 star.Star
通过钩子驱动：AWAKE/SLEEPING/SEMI_AWAKE 三态 + 情绪涌现 + 梦境生成 + 感官系统
"""
import asyncio
import functools
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


def _command_response(func):
    """
    装饰器：标记一个命令响应，让 SLEEPING 状态的 on_decorating_result 跳过睡眠拦截。

    原理：每个 yield 前设 self._recent_command = True。
    on_decorating_result 看到该标志 → 不替换 chain 为 zzZ → 立即重置为 False。
    """
    @functools.wraps(func)
    async def wrapper(self, event, *args, **kwargs):
        gen = func(self, event, *args, **kwargs)
        try:
            while True:
                self._recent_command = True
                result = await gen.__anext__()
                yield result
        except StopAsyncIteration:
            pass
    return wrapper


def _format_drowsy_context(progress: float) -> str:
    """
    根据渐困进度生成 system_prompt 注入。
    0.0-0.3：轻微犯困
    0.3-0.6：开始有点困
    0.6-0.85：明显困意
    0.85-1.0：快睡着了
    """
    if progress <= 0:
        return ""
    if progress < 0.3:
        body = "微微有点犯困，但还能撑住。"
    elif progress < 0.6:
        body = "开始有点困了，回复可以更短更自然，不需要那么完整。"
    elif progress < 0.85:
        body = "已经很困了，意识开始黏糊，回答会更简短、更口语化、可能有点断断续续。"
    else:
        body = "快撑不住了，意识模糊，可能会在回复中途睡着。"
    return (
        f"<internal_state>状态：{body}（生理节律窗口进度 {progress * 100:.0f}%）"
        f"</internal_state>"
    )


class JunqianCircadianPlugin(star.Star):
    """生理节律系统插件"""

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.context = context
        self.config = config

        # 持久化
        self._persistence = CircadianPersistence(self)

        # 时钟（v0.3.0：入睡/起床窗口 + 半醒时长）
        sleep_ws = self.config.get("sleep_window_start", "23:00")
        sleep_we = self.config.get("sleep_window_end", "00:00")
        wake_ws = self.config.get("wake_window_start", "07:30")
        wake_we = self.config.get("wake_window_end", "08:30")
        semi = self.config.get("semi_awake_window", 30)
        self._clock = CircadianClock(
            sleep_window_start=CircadianClock.parse_time(sleep_ws),
            sleep_window_end=CircadianClock.parse_time(sleep_we),
            wake_window_start=CircadianClock.parse_time(wake_ws),
            wake_window_end=CircadianClock.parse_time(wake_we),
            semi_awake_window=semi,
        )

        # 当前渐困进度（每分钟 tick 更新），用于 LLM prompt 注入
        self._sleep_progress: float = 0.0

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

        # 睡眠拦截豁免：命令响应放行（由 _command_response 装饰器在每个 yield 前设 True，
        # on_decorating_result 检查后立即重置为 False）
        self._recent_command: bool = False

        # 最近一次见过的 AstrMessageEvent——给背景任务（clock ticker 触发梦境）用
        # livingmemory 工具内部需要 event 才能决定 session/persona 作用域
        self._last_event: Optional[AstrMessageEvent] = None

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

        # 为今天 roll 一个随机唤醒时刻（缺失或跨天则重新随机；同日保持）
        from datetime import date
        sm = self._state_machine
        if sm.needs_wake_time_roll(date.today()):
            new_wake = self._clock.random_wake_time()
            sm.set_today_wake_time(new_wake, date.today())
            logger.info(f"[JunqianCircadian] Today's wake time rolled: {new_wake.strftime('%H:%M')}")
        else:
            logger.info(
                f"[JunqianCircadian] Today's wake time: "
                f"{sm.get_data().wake_random_time_iso} (kept)"
            )

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
        tasks_to_cancel = [t for t in (self._clock_task, self._rain_wake_task) if t]
        for t in tasks_to_cancel:
            t.cancel()
        if tasks_to_cancel:
            # 等所有协程收到 CancelledError 后再继续，避免丢任务
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        await self._sensory.stop()
        await self._save_all_state()
        logger.info("[JunqianCircadian] Plugin terminated, state saved")

    # ─────────────────────────────────────────────
    # 时钟 ticker
    # ─────────────────────────────────────────────

    async def _clock_ticker(self):
        """
        每 60 秒检查一次状态转换 + 更新渐困进度。
        v0.3.0：用 sleep_progress 替换模糊窗口；不再有"AI 自主决定"的随机延迟，
        渐困节奏纯靠 prompt 注入实现自然过渡；强制切发生在窗口终点。
        """
        while True:
            await asyncio.sleep(60)
            try:
                if self._state_machine is None:
                    continue

                sm = self._state_machine
                now = datetime.now()

                progress, signal = sm.tick(now)
                self._sleep_progress = progress

                if signal == "force_sleep":
                    logger.info(
                        f"[JunqianCircadian] Force sleep at {now.strftime('%H:%M')} "
                        f"(progress was {progress:.2f})"
                    )
                    await self._handle_state_change(CircadianState.SLEEPING)
                elif signal == "should_wake":
                    wake_t = sm.get_data().wake_random_time_iso or "?"
                    logger.info(
                        f"[JunqianCircadian] Wake time reached: {wake_t} "
                        f"→ SEMI_AWAKE"
                    )
                    await self._handle_state_change(CircadianState.SEMI_AWAKE)

                await self._save_all_state()

            except Exception as e:
                logger.error(f"[JunqianCircadian] Clock ticker error: {e}")

    async def _rain_wake_checker(self):
        """
        每 60 秒检查一次：当前是否处于 SLEEPING + 起床窗口内 + 天气突变到中雨以上。
        触发后通过 on_decorating_result 在下一次消息里"主动嘟囔"。

        v0.3.0：只在起床窗口内才生效（深夜下雨不会吵醒）。
        """
        while True:
            await asyncio.sleep(60)
            try:
                if self._state_machine is None:
                    continue
                # 仅 SLEEPING 时关心雨声
                if self._state_machine.state != CircadianState.SLEEPING:
                    continue
                # v0.3.0：只在起床窗口内才被雨声吵醒
                now = datetime.now()
                if not self._clock.in_wake_window(now):
                    continue
                threshold = self.config.get("rain_wake_threshold_mm", 2.5)
                if self._sensory.detect_rain_wake(threshold_mm=threshold):
                    pass_through_semi = self.config.get("rain_wake_pass_through_semi", True)
                    if self._state_machine.trigger_rain_wake(pass_through_semi=pass_through_semi):
                        wx = self._sensory.current_weather
                        desc = wx.description if wx else "中雨"
                        # 设置 pending 消息——下次对话时由 on_decorating_result 注入
                        self._pending_rain_wake_msg = (
                            f"……唔……外面{desc}……被吵醒了……"
                        )
                        logger.info(
                            f"[JunqianCircadian] Rain wake triggered in wake window: "
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
            recall_result = await self._lm_bridge.recall_for_dream(event=self._last_event)
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
        self._last_event = event
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

            # 0) 渐困提示（v0.3.0）：仅当 sleep_progress > 0 时注入
            progress = self._clock.sleep_progress(datetime.now())
            if progress > 0:
                ctx_parts.append(_format_drowsy_context(progress))

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
        self._last_event = event
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
        - 睡眠状态：替换为"在睡觉"的自然提示（命令响应放行）
        - AWAKE 状态：注入待呈现的梦境片段
        - AWAKE 状态：注入待发的雨声唤醒消息
        """
        self._last_event = event
        if self._state_machine is None:
            return

        result = event.get_result()
        chain = result.chain
        from astrbot.api.message_components import Plain

        # 睡眠拦截
        if self._state_machine.state == CircadianState.SLEEPING:
            # 命令响应放行：/晚安、/weather 等用户主动触发的命令不应该被劫持
            if self._recent_command:
                self._recent_command = False
                return
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
        # 在用户下次发消息时被 AI 带出来，自然而不打扰
        if (self._state_machine.state == CircadianState.AWAKE and
                self._pending_rain_wake_msg):
            chain.append(Plain(f"\n\n{self._pending_rain_wake_msg}"))
            self._pending_rain_wake_msg = ""

    # ─────────────────────────────────────────────
    # 主动消息钩子（等待 LLM 时）
    # ─────────────────────────────────────────────

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """用户发消息时如果处于 SEMI_AWAKE，发送提示并转清醒"""
        self._last_event = event
        if self._state_machine is None:
            return

        if self._state_machine.state == CircadianState.SEMI_AWAKE:
            # 半醒收到第一条消息 → 直接转清醒（不再卡在半醒）
            self._state_machine.wake_to_awake()
            self._sent_wakeup_greeting = False
            await self._save_all_state()
            await event.send(event.plain_result("……嗯……？醒了……稍等……"))

    # ─────────────────────────────────────────────
    # Agent 完成钩子
    # ─────────────────────────────────────────────

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, resp):
        """Agent 轮次完成，推进情绪衰减"""
        self._last_event = event
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

    @_command_response
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
        lines.append(f"睡眠窗口：{self.config.get('sleep_window_start', '23:00')} – {self.config.get('sleep_window_end', '00:00')}")
        lines.append(f"起床窗口：{self.config.get('wake_window_start', '07:30')} – {self.config.get('wake_window_end', '08:30')}")
        sm_data = sm.get_data()
        if sm_data.wake_random_time_iso:
            lines.append(f"今日随机唤醒时刻：{sm_data.wake_random_time_iso}")
        if self._sleep_progress > 0:
            lines.append(f"渐困进度：{self._sleep_progress * 100:.0f}%")
        if self._pending_dream_text:
            lines.append(f"梦境：{self._pending_dream_text[:50]}...")
        yield event.plain_result("\n".join(lines))

    @_command_response
    @filter.command("my_dream")
    async def my_dream(self, event: AstrMessageEvent):
        """问 AI 做了什么梦"""
        if self._pending_dream_text:
            yield event.plain_result(f"我刚才做了一个梦……\n\n{self._pending_dream_text}")
            self._pending_dream_text = ""
            self._pending_dream = False
            if self._emotional_state:
                self._emotional_state.pending_dream_to_show = False
        else:
            yield event.plain_result("今天还没有做梦呢……或者，做了但不记得了。")

    @_command_response
    @filter.command("晚安")
    async def goodnight(self, event: AstrMessageEvent):
        """用户说晚安，提前触发睡眠"""
        if self._state_machine:
            self._state_machine.trigger_sleep()
            await self._save_all_state()
            yield event.plain_result("晚安……我也去休息了。")

    @_command_response
    @filter.command("早安")
    async def goodmorning(self, event: AstrMessageEvent):
        """用户说早安，跳过半醒直接清醒"""
        if self._state_machine:
            self._state_machine.wake_to_awake()
            await self._save_all_state()
            # 醒来时重新计算情绪
            await self._recompute_emotional_on_wake()
            yield event.plain_result("早安……醒了。")

    @_command_response
    @filter.command("再睡会儿")
    async def snooze(self, event: AstrMessageEvent):
        """用户说再睡会儿，延迟入睡"""
        if self._state_machine and self._state_machine.state == CircadianState.AWAKE:
            self._state_machine.delay_sleep(30)
            yield event.plain_result("好……再睡一会儿。")

    @_command_response
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

    @_command_response
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
            recall_result = await self._lm_bridge.recall_for_emotional_context(event=self._last_event)
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
            "根据以下记忆碎片，判断 AI 醒来时应该带着什么情绪基调。\n"
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
            # 兼容全角逗号（中文 LLM 习惯用"，"分隔情绪和强度）
            text = (resp.completion_text or "").strip().replace("，", ",")
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

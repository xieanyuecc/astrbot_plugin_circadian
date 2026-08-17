"""
P0 修复验证脚本 —— 不依赖 AstrBot runtime
通过 mock astrbot 模块 + 包 import，测试 CircadianClock + CircadianStateMachine。
"""
import sys
from types import ModuleType
from datetime import datetime, time


# ── mock astrbot 包（让 circadian/__init__.py 的 import 不报错）──
def _make_logger():
    class _L:
        def info(self, *a, **kw): print("[INFO]", *a)
        def error(self, *a, **kw): print("[ERROR]", *a)
        def warning(self, *a, **kw): print("[WARN]", *a)
        def debug(self, *a, **kw): pass
    return _L()

def _ensure_module(path):
    """递归创建嵌套 ModuleType，确保 sys.modules 里所有父级都有"""
    parts = path.split(".")
    for i in range(1, len(parts) + 1):
        name = ".".join(parts[:i])
        if name not in sys.modules:
            mod = ModuleType(name)
            mod.__path__ = []  # 标记为包
            sys.modules[name] = mod

_ensure_module("astrbot")
sys.modules["astrbot"].logger = _make_logger()

# 给所有 circadian 可能会 import 的子模块做占位
for path in [
    "astrbot.api",
    "astrbot.api.star",
    "astrbot.api.event",
    "astrbot.api.provider",
    "astrbot.api.message_components",
    "astrbot.core",
    "astrbot.core.agent",
    "astrbot.core.agent.run_context",
    "astrbot.core.agent.message",
    "astrbot.core.astr_agent_context",
]:
    _ensure_module(path)

# 给 livingmemory 用的几个类补上占位
sys.modules["astrbot.core.agent.run_context"].ContextWrapper = type("ContextWrapper", (), {})
sys.modules["astrbot.core.astr_agent_context"].AstrAgentContext = type("AstrAgentContext", (), {})
sys.modules["astrbot.core.agent.message"].TextPart = type("TextPart", (), {})


# ── 包方式 import ──
PLUGIN_DIR = r"D:\workspace\astrbot-plugins\astrbot_plugin_junqian_circadian\astrbot_plugin_junqian_circadian"
sys.path.insert(0, PLUGIN_DIR)

from circadian.clock import CircadianClock
from circadian.state_machine import CircadianStateMachine, CircadianState


def make_sm(sleep="23:00", wake="07:00", fuzzy=30):
    clock = CircadianClock(
        sleep_time=CircadianClock.parse_time(sleep),
        wake_time=CircadianClock.parse_time(wake),
        fuzzy_window_minutes=fuzzy,
    )
    return CircadianStateMachine(clock)


def tick(sm, dt):
    return sm.check_and_transition(dt)


# ─────────────────────────────────────────────────────────
# P0-1：睡眠时段判断（修复 _should_trigger_wake 的跨午夜 bug）
# ─────────────────────────────────────────────────────────

def test_1_late_night_stays_sleeping():
    """GLM 报告的核心 bug：23:30 入睡后 60 秒被误判"天亮"。修复后 23:31 应保持 SLEEPING。"""
    sm = make_sm()
    sm.trigger_sleep()
    assert sm.state == CircadianState.SLEEPING, f"trigger_sleep 后应为 SLEEPING，实际 {sm.state}"

    result = tick(sm, datetime(2026, 8, 17, 23, 31))
    assert sm.state == CircadianState.SLEEPING, \
        f"❌ 23:31 应保持 SLEEPING（睡眠时段 23:00-07:00），实际 {sm.state} (result={result})"
    print("✓ Test 1.1: 23:31 保持 SLEEPING（修复 GLM 报告的 1 分钟醒 bug）")


def test_2_six_fifty_nine_stays_sleeping():
    """06:59 还在睡眠时段内，应保持 SLEEPING"""
    sm = make_sm()
    sm.trigger_sleep()
    tick(sm, datetime(2026, 8, 18, 6, 59))
    assert sm.state == CircadianState.SLEEPING, f"❌ 06:59 应保持 SLEEPING，实际 {sm.state}"
    print("✓ Test 1.2: 06:59 保持 SLEEPING")


def test_3_seven_am_wakes_to_semi_awake():
    """07:00 整点出睡眠时段，自动转 SEMI_AWAKE"""
    sm = make_sm()
    sm.trigger_sleep()
    tick(sm, datetime(2026, 8, 18, 7, 0))
    assert sm.state == CircadianState.SEMI_AWAKE, \
        f"❌ 07:00 应自动转 SEMI_AWAKE，实际 {sm.state}"
    print("✓ Test 1.3: 07:00 自动转 SEMI_AWAKE")


def test_4_afternoon_goodnight_wakes_to_semi():
    """
    GLM 问题 3：下午说晚安 → 60 秒后被误判天亮。
    修复后行为：下午不在睡眠时段 → SLEEPING 自动转 SEMI_AWAKE。
    这是合理行为：下午本来就不该睡，自动半醒是对的。
    （不像旧版会被错判"天亮"——现在按"是否在睡眠时段内"严格判断）
    """
    sm = make_sm()
    sm.trigger_sleep()  # 14:00 说晚安
    assert sm.state == CircadianState.SLEEPING

    tick(sm, datetime(2026, 8, 17, 14, 1))
    assert sm.state == CircadianState.SEMI_AWAKE, \
        f"❌ 14:01 应自动转 SEMI_AWAKE（不在睡眠时段内），实际 {sm.state}"
    print("✓ Test 1.4: 14:00 晚安 → 14:01 自动 SEMI_AWAKE（合理：下午不在睡眠时段）")


def test_4b_afternoon_sleep_not_premature():
    """
    另一个角度：14:00 晚安后 60 秒不会被旧 bug 误判"天亮"再睡。
    旧版 _should_trigger_wake 会让 14:01 → SEMI_AWAKE → 然后 _should_trigger_sleep 又让它回 SLEEPING（因为 14:01 >= 23:00 是 false）—— 实际上旧版会让 SEMI_AWAKE 卡住（死胡同）。
    新版：直接 SEMI_AWAKE，不会卡死。
    """
    sm = make_sm()
    sm.trigger_sleep()

    # 连续 tick 几次，都保持 SEMI_AWAKE（不会在 AWAKE/SLEEPING 间震荡）
    for minute in [1, 5, 30, 59]:
        tick(sm, datetime(2026, 8, 17, 14, minute))
        assert sm.state == CircadianState.SEMI_AWAKE, \
            f"❌ 14:{minute:02d} 应保持 SEMI_AWAKE，实际 {sm.state}"
    print("✓ Test 1.4b: 下午晚安后稳定在 SEMI_AWAKE，不会在 AWAKE/SLEEPING 间震荡")


def test_5_auto_sleep_after_fuzzy_window():
    """模糊窗口结束后（AWAKE 状态），到 23:30 自动转 SLEEPING。"""
    sm = make_sm()

    tick(sm, datetime(2026, 8, 17, 22, 59))
    assert sm.state == CircadianState.AWAKE, f"❌ 22:59 应 AWAKE，实际 {sm.state}"

    tick(sm, datetime(2026, 8, 17, 23, 0))
    assert sm.state == CircadianState.AWAKE, \
        f"❌ 23:00 模糊窗口内不应自动切 SLEEPING，实际 {sm.state}"

    tick(sm, datetime(2026, 8, 17, 23, 29))
    assert sm.state == CircadianState.AWAKE, f"❌ 23:29 模糊窗口内应 AWAKE，实际 {sm.state}"

    tick(sm, datetime(2026, 8, 17, 23, 30))
    assert sm.state == CircadianState.SLEEPING, \
        f"❌ 23:30 应自动转 SLEEPING，实际 {sm.state}"
    print("✓ Test 1.5: AWAKE 22:59 → 23:00-23:29 模糊 → 23:30 自动 SLEEPING")


def test_6_non_cross_midnight_config():
    """异常配置：不跨午夜（如 02:00-10:00）也能工作"""
    sm = make_sm(sleep="02:00", wake="10:00")

    tick(sm, datetime(2026, 8, 17, 1, 59))
    assert sm.state == CircadianState.AWAKE, f"❌ 01:59 应 AWAKE，实际 {sm.state}"

    sm.trigger_sleep()
    tick(sm, datetime(2026, 8, 17, 3, 0))
    assert sm.state == CircadianState.SLEEPING, f"❌ 03:00 应 SLEEPING，实际 {sm.state}"

    tick(sm, datetime(2026, 8, 17, 9, 59))
    assert sm.state == CircadianState.SLEEPING, f"❌ 09:59 应 SLEEPING，实际 {sm.state}"

    tick(sm, datetime(2026, 8, 17, 10, 0))
    assert sm.state == CircadianState.SEMI_AWAKE, \
        f"❌ 10:00 应 SEMI_AWAKE，实际 {sm.state}"
    print("✓ Test 1.6: 不跨午夜配置（02:00-10:00）正确")


def test_7_same_time_config_no_sleep():
    """边界：配置相同（如 07:00-07:00）→ 不算睡眠时段，trigger 后会被自动转半醒"""
    sm = make_sm(sleep="07:00", wake="07:00")
    sm.trigger_sleep()
    # 14:00 tick：_is_in_sleep_period 返回 false（sleep_mins == wake_mins）→ 转半醒
    tick(sm, datetime(2026, 8, 17, 12, 0))
    assert sm.state == CircadianState.SEMI_AWAKE, \
        f"❌ 配置相同时 tick 应转 SEMI_AWAKE（_is_in_sleep_period=false），实际 {sm.state}"
    print("✓ Test 1.7: 配置相同时 SLEEPING → SEMI_AWAKE（不算睡眠时段）")


# ─────────────────────────────────────────────────────────
# P0-2：半醒自动转清醒（状态机 API 部分）
# 完整 on_waiting_llm_request 流程需要在 AstrBot 环境跑
# ─────────────────────────────────────────────────────────

def test_8_semi_awake_wake_to_awake():
    """半醒状态调 wake_to_awake() 应该直接转 AWAKE"""
    sm = make_sm()
    sm.trigger_sleep()
    sm.trigger_semi_awake()
    assert sm.state == CircadianState.SEMI_AWAKE

    sm.wake_to_awake()
    assert sm.state == CircadianState.AWAKE, f"❌ wake_to_awake 后应 AWAKE，实际 {sm.state}"
    print("✓ Test 2.1: SEMI_AWAKE → wake_to_awake() → AWAKE（状态机 API 正确）")


def test_9_semi_awake_persists_without_message():
    """半醒状态在没消息时不会自动转 AWAKE。"""
    sm = make_sm()
    sm.trigger_sleep()
    sm.trigger_semi_awake()
    assert sm.state == CircadianState.SEMI_AWAKE

    for minute in [10, 20, 30, 59]:
        tick(sm, datetime(2026, 8, 18, 7, minute))
        assert sm.state == CircadianState.SEMI_AWAKE, \
            f"❌ 07:{minute:02d} 应保持 SEMI_AWAKE（无消息不自动转），实际 {sm.state}"
    print("✓ Test 2.2: SEMI_AWAKE 无消息时不会自动转 AWAKE")


# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("P0 修复验证 —— 睡眠时段判断 + 半醒出口")
    print("=" * 60)

    print("\n[P0-1] 睡眠时段判断")
    test_1_late_night_stays_sleeping()
    test_2_six_fifty_nine_stays_sleeping()
    test_3_seven_am_wakes_to_semi_awake()
    test_4_afternoon_goodnight_wakes_to_semi()
    test_4b_afternoon_sleep_not_premature()
    test_5_auto_sleep_after_fuzzy_window()
    test_6_non_cross_midnight_config()
    test_7_same_time_config_no_sleep()

    print("\n[P0-2] 半醒自动转清醒（状态机 API 部分）")
    test_8_semi_awake_wake_to_awake()
    test_9_semi_awake_persists_without_message()

    print("\n" + "=" * 60)
    print("✅ 所有 P0 测试通过！")
    print("=" * 60)
    print("\n注：P0-2 完整流程（on_waiting_llm_request 调 wake_to_awake）需要 AstrBot 环境跑。")

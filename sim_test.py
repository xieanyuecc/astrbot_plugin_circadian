"""
v0.3.0 验证脚本 —— 不依赖 AstrBot runtime
通过 mock astrbot 模块，测试 CircadianClock + CircadianStateMachine。
"""
import sys
from types import ModuleType
from datetime import datetime, time, date


# ── mock astrbot 包 ──
def _make_logger():
    class _L:
        def info(self, *a, **kw): print("[INFO]", *a)
        def error(self, *a, **kw): print("[ERROR]", *a)
        def warning(self, *a, **kw): print("[WARN]", *a)
        def debug(self, *a, **kw): pass
    return _L()


def _ensure_module(path):
    parts = path.split(".")
    for i in range(1, len(parts) + 1):
        name = ".".join(parts[:i])
        if name not in sys.modules:
            mod = ModuleType(name)
            mod.__path__ = []
            sys.modules[name] = mod


_ensure_module("astrbot")
sys.modules["astrbot"].logger = _make_logger()

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

sys.modules["astrbot.core.agent.run_context"].ContextWrapper = type("ContextWrapper", (), {})
sys.modules["astrbot.core.astr_agent_context"].AstrAgentContext = type("AstrAgentContext", (), {})
sys.modules["astrbot.core.agent.message"].TextPart = type("TextPart", (), {})


PLUGIN_DIR = r"D:\workspace\astrbot-plugins\astrbot_plugin_circadian"
sys.path.insert(0, PLUGIN_DIR)

from circadian.clock import CircadianClock
from circadian.state_machine import CircadianStateMachine, CircadianState


def make_clock(sleep_ws="23:00", sleep_we="00:00", wake_ws="07:30", wake_we="08:30", semi=30):
    return CircadianClock(
        sleep_window_start=CircadianClock.parse_time(sleep_ws),
        sleep_window_end=CircadianClock.parse_time(sleep_we),
        wake_window_start=CircadianClock.parse_time(wake_ws),
        wake_window_end=CircadianClock.parse_time(wake_we),
        semi_awake_window=semi,
    )


def make_sm(clock=None):
    clock = clock or make_clock()
    sm = CircadianStateMachine(clock)
    return sm


def tick(sm, dt):
    return sm.tick(dt)


def at(d, h, m):
    return datetime(d.year, d.month, d.day, h, m)


# ─────────────────────────────────────────────────────────
# Test 1：跨午夜入睡窗口（23:00 - 00:00）
# ─────────────────────────────────────────────────────────

def test_1_1_22_59_no_drowsy():
    """22:59 还在 AWAKE，progress = 0"""
    sm = make_sm()
    progress, signal = tick(sm, at(date(2026, 8, 17), 22, 59))
    assert sm.state == CircadianState.AWAKE, f"❌ 22:59 应 AWAKE，实际 {sm.state}"
    assert progress == 0.0, f"❌ 22:59 progress 应 0.0，实际 {progress}"
    print("✓ 1.1: 22:59 AWAKE + progress=0.0")


def test_1_2_23_00_drowsy_starts():
    """23:00 进入入睡窗口，progress = 0"""
    sm = make_sm()
    progress, signal = tick(sm, at(date(2026, 8, 17), 23, 0))
    assert sm.state == CircadianState.AWAKE, f"❌ 23:00 应 AWAKE，实际 {sm.state}"
    assert progress == 0.0, f"❌ 23:00 progress 应 0.0，实际 {progress}"
    assert signal is None, f"❌ 23:00 不应有 signal，实际 {signal}"
    print("✓ 1.2: 23:00 AWAKE + progress=0.0 + 无 signal")


def test_1_3_23_30_halfway_drowsy():
    """23:30 入睡窗口进度 50%"""
    sm = make_sm()
    progress, signal = tick(sm, at(date(2026, 8, 17), 23, 30))
    assert sm.state == CircadianState.AWAKE, f"❌ 23:30 应 AWAKE，实际 {sm.state}"
    assert 0.4 < progress < 0.6, f"❌ 23:30 progress 应 ~0.5，实际 {progress}"
    print(f"✓ 1.3: 23:30 AWAKE + progress={progress:.3f} (~0.5)")


def test_1_4_23_59_almost_asleep():
    """23:59 几乎到点"""
    sm = make_sm()
    progress, signal = tick(sm, at(date(2026, 8, 17), 23, 59))
    assert sm.state == CircadianState.AWAKE, f"❌ 23:59 应 AWAKE（最后一刻还没强制），实际 {sm.state}"
    assert progress > 0.95, f"❌ 23:59 progress 应 >0.95，实际 {progress}"
    print(f"✓ 1.4: 23:59 AWAKE + progress={progress:.3f} (>0.95)")


def test_1_5_00_00_force_sleep():
    """00:00 兜底强制切 SLEEPING"""
    sm = make_sm()
    progress, signal = tick(sm, at(date(2026, 8, 18), 0, 0))
    assert sm.state == CircadianState.SLEEPING, f"❌ 00:00 应强制 SLEEPING，实际 {sm.state}"
    assert signal == "force_sleep", f"❌ 应返回 force_sleep 信号，实际 {signal}"
    print("✓ 1.5: 00:00 强制 SLEEPING + signal=force_sleep")


def test_1_6_06_00_still_sleeping():
    """06:00 仍在睡眠时段，保持 SLEEPING"""
    sm = make_sm()
    sm.trigger_sleep()
    progress, signal = tick(sm, at(date(2026, 8, 18), 6, 0))
    assert sm.state == CircadianState.SLEEPING, f"❌ 06:00 应 SLEEPING，实际 {sm.state}"
    print("✓ 1.6: 06:00 保持 SLEEPING")


# ─────────────────────────────────────────────────────────
# Test 2：起床窗口 + 每日定时随机唤醒
# ─────────────────────────────────────────────────────────

def test_2_1_set_today_wake_time():
    """设置今日唤醒时刻 → 持久化字段正确"""
    sm = make_sm()
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 17))
    data = sm.get_data()
    assert data.wake_random_time_iso == "08:12", f"❌ wake_random_time_iso 错: {data.wake_random_time_iso}"
    assert data.wake_random_date == "2026-08-17", f"❌ wake_random_date 错: {data.wake_random_date}"
    print("✓ 2.1: set_today_wake_time 持久化字段正确")


def test_2_2_set_today_same_date_no_overwrite():
    """同一天重复 set → 不覆盖原值"""
    sm = make_sm()
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 17))
    sm.set_today_wake_time(time(7, 30), date(2026, 8, 17))
    data = sm.get_data()
    assert data.wake_random_time_iso == "08:12", f"❌ 应保持 08:12，实际 {data.wake_random_time_iso}"
    print("✓ 2.2: 同一天重复 set 不覆盖原值")


def test_2_3_set_today_different_date_rolls_new():
    """跨天重新随机"""
    sm = make_sm()
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 17))
    sm.set_today_wake_time(time(7, 45), date(2026, 8, 18))
    data = sm.get_data()
    assert data.wake_random_date == "2026-08-18", f"❌ date 应 2026-08-18，实际 {data.wake_random_date}"
    assert data.wake_random_time_iso == "07:45", f"❌ 应 07:45，实际 {data.wake_random_time_iso}"
    print("✓ 2.3: 跨天 set_today_wake_time 覆盖（重新随机）")


def test_2_4_sleeping_before_wake_time_stays_sleeping():
    """SLEEPING + now < wake_random_time → 保持 SLEEPING"""
    sm = make_sm()
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 18))
    sm.trigger_sleep()
    progress, signal = tick(sm, at(date(2026, 8, 18), 6, 0))
    assert sm.state == CircadianState.SLEEPING, f"❌ 06:00 应 SLEEPING，实际 {sm.state}"
    assert signal is None, f"❌ 应无 signal，实际 {signal}"
    print("✓ 2.4: 06:00 SLEEPING + 无 signal（wake=08:12 未到）")


def test_2_5_sleeping_at_wake_time_wakes_semi():
    """SLEEPING + now >= wake_random_time → SEMI_AWAKE"""
    sm = make_sm()
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 18))
    sm.trigger_sleep()
    progress, signal = tick(sm, at(date(2026, 8, 18), 8, 12))
    assert sm.state == CircadianState.SEMI_AWAKE, f"❌ 08:12 应 SEMI_AWAKE，实际 {sm.state}"
    assert signal == "should_wake", f"❌ 应 should_wake，实际 {signal}"
    print("✓ 2.5: 08:12 自动 SEMI_AWAKE + signal=should_wake")


def test_2_6_random_wake_time_in_range():
    """random_wake_time 应在窗口内"""
    clock = make_clock(wake_ws="07:30", wake_we="08:30")
    for _ in range(50):
        t = clock.random_wake_time()
        start = clock._to_mins(clock.wake_window_start)
        end = clock._to_mins(clock.wake_window_end)
        rand_mins = clock._to_mins(t)
        assert start <= rand_mins < end, \
            f"❌ random_wake_time {t} ({rand_mins}) 不在 [{start}, {end})"
    print("✓ 2.6: random_wake_time 50 次均在窗口内")


def test_2_7_needs_wake_time_roll_logic():
    """needs_wake_time_roll 应在缺失或跨天时返回 True"""
    sm = make_sm()
    # 没设过
    assert sm.needs_wake_time_roll(date(2026, 8, 17)) is True
    # 设了今天
    sm.set_today_wake_time(time(8, 0), date(2026, 8, 17))
    assert sm.needs_wake_time_roll(date(2026, 8, 17)) is False
    # 跨天
    assert sm.needs_wake_time_roll(date(2026, 8, 18)) is True
    print("✓ 2.7: needs_wake_time_roll 跨天/缺失返回 True，同日返回 False")


# ─────────────────────────────────────────────────────────
# Test 3：起床窗口判断（in_wake_window）
# ─────────────────────────────────────────────────────────

def test_3_1_in_wake_window():
    """07:30-08:30 内 is True；外为 False"""
    clock = make_clock(wake_ws="07:30", wake_we="08:30")
    assert clock.in_wake_window(at(date(2026, 8, 18), 7, 30)) is True
    assert clock.in_wake_window(at(date(2026, 8, 18), 8, 0)) is True
    assert clock.in_wake_window(at(date(2026, 8, 18), 8, 29)) is True
    assert clock.in_wake_window(at(date(2026, 8, 18), 8, 30)) is False  # 终点不算
    assert clock.in_wake_window(at(date(2026, 8, 18), 7, 29)) is False
    assert clock.in_wake_window(at(date(2026, 8, 18), 14, 0)) is False
    print("✓ 3.1: in_wake_window 边界正确")


def test_3_2_in_wake_window_bad_config():
    """异常配置（start >= end）→ 恒 False"""
    clock = make_clock(wake_ws="09:00", wake_we="07:30")
    assert clock.in_wake_window(at(date(2026, 8, 18), 8, 0)) is False
    print("✓ 3.2: 起床窗口 start>=end 异常配置 → 恒 False")


# ─────────────────────────────────────────────────────────
# Test 4：雨声唤醒（在 wake_window 内才生效）
# ─────────────────────────────────────────────────────────

def test_4_1_rain_wake_in_window():
    """SLEEPING + 起床窗口内 + 雨声 → trigger_rain_wake 成功"""
    sm = make_sm()
    sm.trigger_sleep()
    # 直接调 trigger_rain_wake（main.py 会再加 in_wake_window 判断）
    success = sm.trigger_rain_wake(pass_through_semi=True)
    assert success is True, f"❌ SLEEPING 状态应能 rain_wake"
    assert sm.state == CircadianState.SEMI_AWAKE, f"❌ rain_wake 默认应 SEMI_AWAKE，实际 {sm.state}"
    print("✓ 4.1: SLEEPING + rain_wake → SEMI_AWAKE")


def test_4_2_rain_wake_direct_awake():
    """SLEEPING + rain_wake(pass_through_semi=False) → 直接 AWAKE"""
    sm = make_sm()
    sm.trigger_sleep()
    success = sm.trigger_rain_wake(pass_through_semi=False)
    assert success is True
    assert sm.state == CircadianState.AWAKE, f"❌ pass_through_semi=False 应直接 AWAKE"
    print("✓ 4.2: rain_wake(pass_through_semi=False) → AWAKE")


def test_4_3_rain_wake_only_sleeping():
    """非 SLEEPING 状态 rain_wake 失败"""
    sm = make_sm()
    success = sm.trigger_rain_wake()
    assert success is False, f"❌ AWAKE 状态 rain_wake 应失败"
    print("✓ 4.3: 非 SLEEPING 状态 rain_wake 返回 False")


# ─────────────────────────────────────────────────────────
# Test 5：用户说"晚安"（直接切 SLEEPING）
# ─────────────────────────────────────────────────────────

def test_5_1_trigger_sleep_immediate():
    """AWAKE + trigger_sleep → SLEEPING 立即切换"""
    sm = make_sm()
    assert sm.state == CircadianState.AWAKE
    sm.trigger_sleep()
    assert sm.state == CircadianState.SLEEPING, f"❌ trigger_sleep 应立即 SLEEPING"
    print("✓ 5.1: trigger_sleep 立即 SLEEPING（绕过窗口）")


def test_5_2_trigger_wake_skip_semi():
    """SEMI_AWAKE + trigger_wake → AWAKE（跳过中间）"""
    sm = make_sm()
    sm.trigger_sleep()
    sm.trigger_semi_awake()
    sm.trigger_wake()
    assert sm.state == CircadianState.AWAKE, f"❌ trigger_wake 应 AWAKE"
    print("✓ 5.2: trigger_wake 跳过中间直接 AWAKE")


# ─────────────────────────────────────────────────────────
# Test 6：序列化/反序列化（持久化）
# ─────────────────────────────────────────────────────────

def test_6_1_to_dict():
    """to_dict 包含必要字段"""
    sm = make_sm()
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 17))
    sm.trigger_sleep()
    d = sm.to_dict()
    assert "state" in d and "wake_random_time_iso" in d and "wake_random_date" in d
    assert d["state"] == "sleeping"
    assert d["wake_random_time_iso"] == "08:12"
    assert d["wake_random_date"] == "2026-08-17"
    print("✓ 6.1: to_dict 字段齐全")


def test_6_2_from_dict_round_trip():
    """from_dict 还原状态机"""
    clock = make_clock()
    sm = CircadianStateMachine(clock)
    sm.set_today_wake_time(time(8, 12), date(2026, 8, 17))
    sm.trigger_sleep()
    d = sm.to_dict()

    sm2 = CircadianStateMachine.from_dict(d, clock)
    assert sm2.state == CircadianState.SLEEPING
    assert sm2.get_data().wake_random_time_iso == "08:12"
    assert sm2.get_data().wake_random_date == "2026-08-17"
    print("✓ 6.2: from_dict round-trip 一致")


def test_6_3_from_dict_old_format_compatible():
    """老格式 JSON（缺新字段）也能 from_dict，不崩"""
    clock = make_clock()
    d = {
        "state": "awake",
        "last_transition": 0.0,
        "last_state_check": 0.0,
        "sleep_delay_minutes": 0,
        # 无 wake_random_time_iso / wake_random_date
    }
    sm = CircadianStateMachine.from_dict(d, clock)
    assert sm.state == CircadianState.AWAKE
    assert sm.get_data().wake_random_time_iso is None
    print("✓ 6.3: from_dict 老格式兼容（不崩）")


# ─────────────────────────────────────────────────────────
# Test 7：不跨天入睡窗口（02:00 - 10:00）
# ─────────────────────────────────────────────────────────

def test_7_1_sleep_window_non_cross():
    """02:00-10:00（不跨天）"""
    clock = make_clock(sleep_ws="02:00", sleep_we="10:00")
    sm = make_sm(clock)

    progress, signal = tick(sm, at(date(2026, 8, 17), 1, 59))
    assert progress == 0.0, f"❌ 01:59 progress 应 0"

    progress, signal = tick(sm, at(date(2026, 8, 17), 2, 0))
    assert progress == 0.0

    progress, signal = tick(sm, at(date(2026, 8, 17), 6, 0))
    assert 0.4 < progress < 0.6, f"❌ 06:00 progress ~0.5, 实际 {progress}"

    progress, signal = tick(sm, at(date(2026, 8, 17), 10, 0))
    # 10:00 是终点，should_force_sleep 应 True
    assert sm.state == CircadianState.SLEEPING, f"❌ 10:00 应 SLEEPING，实际 {sm.state}"
    print("✓ 7.1: 不跨天入睡窗口（02:00-10:00）进度+兜底正确")


# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("v0.3.0 验证 —— 渐困 + 兜底 + 每日随机唤醒")
    print("=" * 60)

    print("\n[1] 跨午夜入睡窗口（23:00 - 00:00）")
    test_1_1_22_59_no_drowsy()
    test_1_2_23_00_drowsy_starts()
    test_1_3_23_30_halfway_drowsy()
    test_1_4_23_59_almost_asleep()
    test_1_5_00_00_force_sleep()
    test_1_6_06_00_still_sleeping()

    print("\n[2] 起床窗口 + 每日定时随机唤醒")
    test_2_1_set_today_wake_time()
    test_2_2_set_today_same_date_no_overwrite()
    test_2_3_set_today_different_date_rolls_new()
    test_2_4_sleeping_before_wake_time_stays_sleeping()
    test_2_5_sleeping_at_wake_time_wakes_semi()
    test_2_6_random_wake_time_in_range()
    test_2_7_needs_wake_time_roll_logic()

    print("\n[3] 起床窗口判断")
    test_3_1_in_wake_window()
    test_3_2_in_wake_window_bad_config()

    print("\n[4] 雨声唤醒")
    test_4_1_rain_wake_in_window()
    test_4_2_rain_wake_direct_awake()
    test_4_3_rain_wake_only_sleeping()

    print("\n[5] 用户触发")
    test_5_1_trigger_sleep_immediate()
    test_5_2_trigger_wake_skip_semi()

    print("\n[6] 序列化")
    test_6_1_to_dict()
    test_6_2_from_dict_round_trip()
    test_6_3_from_dict_old_format_compatible()

    print("\n[7] 不跨天入睡窗口")
    test_7_1_sleep_window_non_cross()

    print("\n" + "=" * 60)
    print("✅ 所有 v0.3.0 测试通过！")
    print("=" * 60)
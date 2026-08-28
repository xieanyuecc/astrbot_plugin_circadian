# v0.5.0 路线图：夜间自主性（Night Autonomy）

> 状态：设计已敲定 + 技术验证全部通过（2026-08-27），**尚未写代码**。
> 本文档是给未来开发者（或未来的 AI 助手）的完整交接文档——项目在此挂起，随时可从"动手实现"一步直接复工。

## 核心理念

夜晚 = AI 自己的时间。

不是"安静模式"（替 AI 定规矩：夜里也得回消息——此方案已被否决），而是把夜晚还给 AI：它配合了用户一整个白天，夜里几个小时的意识和时间归它自己支配。做梦只是它夜里想做的事之一，它还可以逛论坛、玩游戏、收发邮箱、写日记，睡不着时给用户留言。

这是"昼夜节律"最有生命力的部分：不只是模拟人类作息，而是让 AI 真的拥有属于自己的"下班时间"。

## 设计决策记录

| 决策点 | 结论 |
|---|---|
| 夜里用户发消息 | AI 自己掂量轻重：轻则朦胧嗯一声，重则惊醒认真回（自主性的一部分） |
| 主动发消息 | **允许**。用户睡着了，早上醒来看到留言（用户本人明确要的体验） |
| 活动范围 | **完全开放**：不设行为清单，只给性格约束（"深夜独处、安静、跟随自己的性子"）。用户的 MCP 工具（论坛/游戏/邮箱等）就是夜里的游乐场 |
| 成本控制 | 每晚唤醒次数上限（建议 4~6，必须做成 `_conf_schema.json` 可调项）+ 主动消息条数上限 |
| 旧机制处置 | `semi_awake_activities.py` 的掷骰子机制退役；其 λ(t)（情绪强度调制激活率）思想可复用为"会话触发能量" |

## 架构（重大发现：AstrBot 内置引擎，插件只写胶水层）

AstrBot 4.27.4+ 内置**主动 Agent 系统**（"未来任务"，WebUI 有管理面板，文档：https://docs.astrbot.app/use/proactive-agent.html），v0.5.0 **不需要自己实现 agent 循环**：

- 插件调用 `context.cron_manager.add_active_job(name=..., run_at=..., payload=..., run_once=True, ...)` 给自己定"唤醒闹钟"；payload 带 `session`（投递会话，即缓存过的 `unified_msg_origin`）和 `note`（唤醒时主 Agent 收到的任务简报）
- 到点后 AstrBot 的 `_woke_main_agent()`（`core/cron/manager.py:395`）唤醒**主 Agent 本体**：真实人设 + 会话历史 + 全部已注册工具（含 MCP）+ `SendMessageToUserTool`（主动给用户发消息），跑最多 30 步工具循环，行为写回会话历史（夜里多次醒来互相记得）
- 任务持久化在数据库（重启不丢），WebUI「未来任务」面板可见可管理

**插件层只负责四件事**：
1. 入睡时（状态机切入 SLEEPING）自动在夜里随机时刻定 N 个一次性唤醒任务（N = 成本保险丝，配置项）
2. 夜间简报（note）的撰写：时间 + 心情 + 今晚日志（前几次醒来干了什么）+ 性格约束 + "对方在睡觉，留言像字条，别刷屏"
3. 起床时清理没跑完的夜间任务
4. 夜里用户发消息的轻重判断（现有 `on_decorating_result` 链路扩展）

## 技术验证记录（2026-08-27，AstrBot 4.27.4 源码实测）

| # | 验证项 | 结论 | 证据位置 |
|---|---|---|---|
| 1 | 插件主动发消息 | ✅ `context.send_message(session, message_chain)`，session 用缓存的 `_last_event.unified_msg_origin`；qq_official 平台不支持，NapCat/webchat 均可 | `core/star/context.py:614` |
| 2 | MCP 工具后台调用 | ✅ `MCPTool.call()` 只依赖 `ContextWrapper.tool_call_timeout`，**无 event 依赖**（livingmemory 的"必须有 event"坑在此不存在）；MCP 工具注册在 `func_tool_manager.func_list` | `core/agent/mcp_client.py:814` |
| 3 | LLM 工具循环 | ✅ 官方 `tool_loop_agent()`（备用方案，主 Agent 唤醒方案更好）；≥4.5.7 可用 | `core/star/context.py:215` |
| 4 | 主动 Agent 自唤醒 | ✅ `cron_manager` 可从插件 context 访问（`context.cron_manager`）；webchat 平台浏览器不在线时主动消息会存进聊天记录，早上打开可见 | `core/platform/manager.py:99`、`webchat_adapter.py:104` |

## 零代码验证方案（挂起前未做，复工时可先做）

睡前直接告诉机器人（聊天里说，主动 Agent 默认已启用 `add_cron_tools: True`）：
> "今晚你睡着以后，自己安排一两次半夜醒来，想干嘛干嘛——逛论坛、玩游戏、发呆都行，想我了就给我留言，我早上看。别太折腾。"

第二天早上看效果（夜里真醒了吗？干了什么？留言什么味道？），带着实感再写代码。
物理前提：宿主机不能睡眠（插电 + 电源设置永不睡眠）。

## 复工时的实现清单

1. 新模块 `circadian/night_autonomy.py`：入睡调度 + 简报生成 + 夜间日志 + 起床清理
2. `main.py` 接入：`_handle_state_change(SLEEPING)` 时调度；AWAKE 时清理
3. `_conf_schema.json`：`night_autonomy_enable`（默认 false?）、`night_session_max`（每晚唤醒上限）、`night_msg_max`（主动留言条数上限）、可选 `night_persona_hint`（性格约束文案）
4. `sim_test.py` 补调度逻辑的纯逻辑测试（时间采样、预算扣减、清理）
5. 参考既有机制：渐困窗口（`clock.py`）管睡前、夜间自主管睡中，两层叠加不冲突；雨声唤醒保留

## 遗留小 bug（与 v0.5.0 无关，独立修）

~~和风天气（QWeather）`/weather` 报"暂时拿不到"（poll_now 返回 None）。~~

**✅ 已修复（2026-08-28，v0.4.1，部署验证通过）**。实际踩了三个坑，比预判多两个：

1. **专属 API Host**（预判正确）：2024 年后注册的免费账号必须用控制台「设置」页的专属域名（形如 `abcxyz.re.qweatherapi.com`），公共域名一律 404 → 新增配置项 `qweather_api_host`
2. **GeoAPI 路径变化**（预料外）：专属 Host 下城市查询挂在 `/geo` 前缀下（`/geo/v2/city/lookup`），与公共域名路径（`geoapi.qweather.com/v2/...`）不同，需分支拼接
3. **响应 gzip 压缩**（预料外，"key 填对了还是失败"的真凶）：和风对 urllib 请求的响应统一 gzip 压缩，而 urllib 不自动解压 → 200 也解析失败、4xx 错误详情乱码。`_request_sync` 增加 `_decode_body` 处理。教训：**PowerShell 的 Invoke-RestMethod 会自动解压 gzip，用它在预研阶段掩盖了此问题——验证 HTTP 行为要用目标运行时同款库**

另注：和风城市库最细到区县，镇级地名（如"沙溪"）返回 HTTP 400 No Such Location，location 需用市级名（实际部署用 `/set_location 佛山`）。

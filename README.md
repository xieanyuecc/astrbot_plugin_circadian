# 昼夜节律 (astrbot_plugin_circadian)

让 AstrBot 里的 AI 拥有自己的生物钟——会睡、会醒、会半梦半醒、会做梦、能感知外界的天气与温度。

> 昼夜有节，醒梦有时。让 AI 也能感知时间的流转。

## 功能

- **状态机**：AWAKE / SEMI_AWAKE / SLEEPING 三态自切换（按用户配置的睡眠/起床时段，支持跨午夜与模糊窗口）
- **半醒活动**：起床后默认 30 分钟处于 SEMI_AWAKE 状态，可自主发起消息
- **梦境生成**：睡眠期间累积梦境上下文，醒来时延迟生成（默认延迟 30 分钟，方便回忆）
- **感官系统**：实时感知天气（wttr.in 真实接口）、温度
- **雨声唤醒**：雨强达到阈值（默认 2.5 mm/h）时触发唤醒，可选"先梦后醒"
- **情绪容器**：基于状态 / 天气 / 温度多源信号的情绪涌现
- **生活上下文**：自动生成 `姐姐在缩被窝` `下着雨窗外有些凉` 一类情境片段，喂给 LLM 当提示

## 安装

**方式 A：AstrBot 桌面版 / 插件市场（推荐）**

插件管理 → "从 Git 安装" → 填仓库地址：

```
https://github.com/xieanyuecc/astrbot_plugin_circadian.git
```

**方式 B：手动安装**

```bash
cd /path/to/astrbot/data/plugins
git clone git@github.com:xieanyuecc/astrbot_plugin_circadian.git
```

重启 AstrBot（或在控制台重载插件）。

## 命令

| 触发 | 说明 |
|---|---|
| `/circadian_status` | 查看当前状态、情绪、生活上下文 |
| `/weather` | 手动查询当前天气（绕开轮询） |
| `/my_dream` | 查看最近的梦境存档 |
| `/set_location 城市名` | 切换所在地（影响 wttr 查询） |
| `晚安` | 自然语言入睡 |
| `早安` | 自然语言起床 |
| `再睡会儿` | 自然语言继续睡 |

## 配置项

> 在 AstrBot 控制台 → 插件管理 → astrbot_plugin_circadian → 配置 修改。

| 项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `sleep_time` | string | `23:00` | 每日入睡时间（HH:MM） |
| `wake_time` | string | `07:00` | 每日起床时间（HH:MM），可晚于 sleep（跨午夜） |
| `semi_awake_window` | int | `30` | 半醒窗口（分钟），起床后多长算半醒 |
| `fuzzy_window_minutes` | int | `30` | 状态切换模糊窗口（分钟），时间波动容忍度 |
| `enable_dream` | bool | `true` | 是否启用梦境生成 |
| `dream_delay_minutes` | int | `30` | 进入半醒后多久生成梦境 |
| `dream_provider_id` | select_provider | — | 梦境生成用的 LLM（建议免费轻量模型） |
| `weather_provider` | string | `wttr` | `wttr`=wttr.in 真实 API / `mock`=模拟（调试用） |
| `location` | string | `沙溪` | 所在地，影响天气查询 |
| `poll_interval_minutes` | int | `60` | 天气轮询间隔（分钟） |
| `rain_wake_threshold_mm` | float | `2.5` | 雨声唤醒阈值（mm/h，2.5=中雨起） |
| `rain_wake_pass_through_semi` | bool | `false` | `true`=雨声唤醒先梦后醒，`false`=直接醒 |
| `hot_threshold` | float | `30.0` | 炎热关心阈值（°C） |
| `cool_threshold` | float | `25.0` | 凉爽阈值（°C，20-25°C 之间） |
| `cold_threshold` | float | `20.0` | 寒冷关心阈值（°C） |

## 当前状态 (v0.2.1)

### ✅ 已完成

- **P0-1** 睡眠时段判断修复：GLM 报告的「23:30 入睡 60 秒后被误判天亮」解决
- **P0-2** 半醒自动转清醒修复：消除「半醒卡死」
- **P2** 睡眠拦截放行命令：`/circadian_status` 等命令在睡眠时段也能响应
- **P2** 默认天气源由 `mock` 改 `wttr.in`
- **P3** 全角逗号兼容

测试覆盖：`sim_test.py` 9 个用例全过。

### ⏳ 计划中（P3+）

- `/my_dream` 看完立即存档
- `/再睡会儿` 检查 `sleep_delay_minutes` 后再触发
- 摆设功能审计：模糊掷骰子真实生效
- 向量化长期记忆（硅基流动 bge-m3 + 余弦相似度检索）
- 接入真实的生活上下文（取代模板生成）

## 已知限制

- **可选依赖 livingmemory 插件**：启用后梦境会尝试桥接调 `recall_long_term_memory` 工具；未安装时优雅降级返回空，不报错。
- **wttr.in 接口**：偶尔超时不影响主流程，下次轮询（默认 60 分钟）会重试。
- **跨平台**：metadata 声明支持 `telegram / discord / qq_official / satori`，AstrBot `>=4.16,<5`。

## 依赖

无外部 Python 包。仅使用 AstrBot SDK 内置 API（`context.llm_generate`、KV 存储、hook 回调）。

## 关联项目

- [hermes-chat（雁栖）](https://example.invalid)（不公开）— 桌面 / 浏览器端应用层，TypeScript 重写同一人格系统
- 这两个仓库稳定能力会互相借鉴（状态机结构、感官模块、情绪容器、生活上下文格式）

## 许可

MIT

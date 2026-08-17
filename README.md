# 昼夜节律 (astrbot_plugin_circadian)

让 AstrBot 里的 AI 拥有自己的生物钟——会睡、会醒、会半梦半醒、会做梦、能感知外界的天气与温度。

> 昼夜有节，醒梦有时。让 AI 也能感知时间的流转。

## 功能

- **状态机**：AWAKE / SEMI_AWAKE / SLEEPING 三态自切换
- **灵活入睡（v0.3.0）**：可配置入睡窗口（如 `23:00-00:00`），进入窗口后 LLM 回复会"自然渐困"（system prompt 注入提示，回复越来越短越黏糊），到终点仍未睡则兜底强制切 SLEEPING（语义：困得不行睡着了）
- **灵活起床（v0.3.0）**：可配置起床窗口（如 `07:30-08:30`），每天在窗口内随机选一个时刻醒来，**持久化**——重启不重新随机，跨过凌晨才会重新随机
- **雨声唤醒（v0.3.0 收窄）**：中雨以上（≥2.5mm/h）可在起床窗口内触发；默认走 SEMI_AWAKE（被吵醒不等于清醒，先半醒再转醒）
- **半醒活动**：起床后默认 30 分钟处于 SEMI_AWAKE 状态，可自主发起消息
- **梦境生成**：睡眠期间累积梦境上下文，醒来时延迟生成（默认延迟 30 分钟，方便回忆）
- **感官系统**：实时感知天气（wttr.in 真实接口）、温度
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
| `/circadian_status` | 查看当前状态、情绪、窗口、今日唤醒时刻 |
| `/weather` | 手动查询当前天气（绕开轮询） |
| `/my_dream` | 查看最近的梦境存档 |
| `/set_location 城市名` | 切换所在地（影响 wttr 查询） |
| `晚安` | 自然语言入睡（绕过窗口，立即切 SLEEPING） |
| `早安` | 自然语言起床（跳过半醒直接清醒） |
| `再睡会儿` | 自然语言延迟入睡 |

## 配置项

> 在 AstrBot 控制台 → 插件管理 → astrbot_plugin_circadian → 配置 修改。

| 项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `sleep_window_start` | string | `23:00` | 入睡窗口起点（开始渐困） |
| `sleep_window_end` | string | `00:00` | 入睡窗口终点（兜底强制入睡，可跨午夜） |
| `wake_window_start` | string | `07:30` | 起床窗口起点（最早自然醒） |
| `wake_window_end` | string | `08:30` | 起床窗口终点（最晚自然醒） |
| `semi_awake_window` | int | `30` | 半醒窗口（分钟），起床后多长算半醒 |
| `enable_dream` | bool | `true` | 是否启用梦境生成 |
| `dream_delay_minutes` | int | `30` | 进入半醒后多久生成梦境 |
| `dream_provider_id` | select_provider | — | 梦境生成用的 LLM（建议免费轻量模型） |
| `weather_provider` | string | `wttr` | `wttr`=wttr.in 真实 API / `mock`=模拟（调试用） |
| `location` | string | `沙溪` | 所在地，影响天气查询 |
| `poll_interval_minutes` | int | `60` | 天气轮询间隔（分钟） |
| `rain_wake_threshold_mm` | float | `2.5` | 雨声唤醒阈值（mm/h，2.5=中雨起），仅在起床窗口内生效 |
| `rain_wake_pass_through_semi` | bool | `true` | `true`=雨声唤醒先梦后醒，`false`=直接醒 |
| `hot_threshold` | float | `30.0` | 炎热关心阈值（°C） |
| `cool_threshold` | float | `25.0` | 凉爽阈值（°C，20-25°C 之间） |
| `cold_threshold` | float | `20.0` | 寒冷关心阈值（°C） |

### 跨午夜配置说明

`sleep_window_start` 和 `sleep_window_end` 配合即可跨午夜：

- `23:00` / `00:00`：晚上 11 点开始渐困，到凌晨 0 点强制入睡
- `02:00` / `10:00`：凌晨 2 点开始渐困（跨夜班场景），早上 10 点兜底
- `00:00` / `08:00`：从 0 点到早上 8 点（不跨午夜）

`wake_window_start` 和 `wake_window_end` 不跨午夜（必须 wake_start < wake_end）。

## v0.3.0 主要变更

- 移除 `sleep_time` / `wake_time` / `fuzzy_window_minutes`，改为四个窗口字段
- 入睡窗口内逐步注入"渐困"system prompt，分四级（<0.3 / 0.3-0.6 / 0.6-0.85 / ≥0.85）
- 起床时刻改为每天固定一次随机（在 `wake_window_start`-`wake_window_end` 内选），跨过凌晨重新随机
- 雨声唤醒限制到起床窗口内才生效
- `rain_wake_pass_through_semi` 默认值由 `false` 改为 `true`（被吵醒不等于清醒）

测试覆盖：`sim_test.py` 23 个用例全过。

## 已知限制

- **可选依赖 livingmemory 插件**：启用后梦境会尝试桥接调 `recall_long_term_memory` 工具；未安装时优雅降级返回空，不报错。
- **wttr.in 接口**：偶尔超时不影响主流程，下次轮询（默认 60 分钟）会重试。
- **跨平台**：metadata 声明支持 `telegram / discord / qq_official / satori`，AstrBot `>=4.16,<5`。

## 依赖

无外部 Python 包。仅使用 AstrBot SDK 内置 API（`context.llm_generate`、KV 存储、hook 回调）。

## 关联项目

- hermes-chat（雁栖）（不公开）— 桌面 / 浏览器端应用层，TypeScript 重写同一人格系统
- 这两个仓库稳定能力会互相借鉴（状态机结构、感官模块、情绪容器、生活上下文格式）

## 许可

MIT
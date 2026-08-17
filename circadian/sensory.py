"""
SensoryModule — 感官系统模块
让小机拥有对外部环境的感知能力。

设计原则：
- 一个 Sensor 对应一类外部信号（天气、空气质量、紫外线……）
- 所有 Sensor 通过统一的 Provider 接口拉取数据
- SensoryModule 聚合所有 Sensor，对外暴露当前快照
- 状态变化时由调用方决定如何响应（解耦：传感器不知道状态机）

当前实现：
- WeatherProvider：天气
  - MockWeatherProvider：默认调试用，预设几个城市天气人格，未知城市走通用模板
  - WttrInProvider：真实 API（https://wttr.in），无需 key，全球覆盖
- 雨声唤醒检测：当前 SLEEPING 状态下若检测到中雨/大雨/暴雨，触发状态切换

Provider 通过配置 `weather_provider` 切换："mock" / "wttr"
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Dict, Any, ClassVar
import asyncio
import json
import random
import urllib.request
import urllib.error

from astrbot import logger


# ─────────────────────────────────────────────────────────────
# 天气快照
# ─────────────────────────────────────────────────────────────

@dataclass
class WeatherSnapshot:
    """一帧天气数据"""
    location: str
    status: str                 # "clear" | "cloudy" | "rain" | "snow" | "fog" | "thunderstorm"
    description: str            # "中雨" / "晴转多云"
    temperature: float          # °C
    feels_like: float           # °C
    humidity: float             # 0-100
    rain_1h: float              # 过去 1 小时降水量(mm)
    wind_speed: float           # m/s
    timestamp: float
    source: str                 # "mock" | "wttr"

    # 雨量等级（中国气象局标准，mm/h）
    RAIN_INTENSITY: ClassVar[Dict[str, tuple]] = {
        "light": (0.0, 2.5),       # 小雨
        "moderate": (2.5, 8.0),    # 中雨
        "heavy": (8.0, 16.0),      # 大雨
        "violent": (16.0, 999.0),  # 暴雨
    }

    @property
    def rain_intensity(self) -> Optional[str]:
        """返回雨量等级（light/moderate/heavy/violent），无雨返回 None"""
        for level, (low, high) in self.RAIN_INTENSITY.items():
            if low <= self.rain_1h < high:
                return level
        return None

    @property
    def is_rainy(self) -> bool:
        return self.status == "rain" or self.rain_1h > 0

    @property
    def is_heavy_rain(self) -> bool:
        """中雨及以上"""
        return self.rain_intensity in ("moderate", "heavy", "violent")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WeatherSnapshot":
        return cls(**data)


# ─────────────────────────────────────────────────────────────
# 天气 Provider 抽象
# ─────────────────────────────────────────────────────────────

class WeatherProvider(ABC):
    """天气数据源抽象接口"""

    @abstractmethod
    async def fetch(self, location: str) -> Optional[WeatherSnapshot]:
        """拉取指定位置的天气，返回 None 表示失败"""
        ...


# ─────────────────────────────────────────────────────────────
# Mock Provider —— 调试用，预设城市天气人格
# ─────────────────────────────────────────────────────────────

class MockWeatherProvider(WeatherProvider):
    """
    模拟天气源——不调外部 API。

    设计目的：
    - 让用户不开 API 也能跑完整链路（rain_wake、温度感知、深夜低温等）
    - 预设几个城市的天气人格，让"去旅游"演示更生动
    - 仅在插件**首次安装启动**时给一帧暴雨，方便立刻看到"被雨声吵醒"的效果
      （用持久化标记，后续重启不再给暴雨，避免 /set_location 切城市时也吃到）

    城市预设：
    - 沙溪 / 中山：南方湿润，雨多
    - 杭州：江南烟雨
    - 北京：北方干燥，多风
    - 上海：海洋性气候
    - 广州：南方湿热
    - 东京 / Kyoto：温带海洋性
    - 其它：通用南方城市模板
    """

    # 城市人格（按 location 字符串匹配关键字，命中即用）
    # 字段：status, base_temp, rain_1h, humidity_bias, wind_bias, description
    CITY_PROFILES: Dict[str, Dict[str, Any]] = {
        "沙溪":   {"status": "rain",       "base_temp": 23.0, "rain_1h": 5.0, "humidity_bias": 15, "wind_bias": 0,  "desc": "南方雨"},
        "中山":   {"status": "rain",       "base_temp": 24.0, "rain_1h": 4.0, "humidity_bias": 12, "wind_bias": 0,  "desc": "南方雨"},
        "杭州":   {"status": "cloudy",     "base_temp": 18.0, "rain_1h": 2.0, "humidity_bias": 10, "wind_bias": 1,  "desc": "江南烟雨"},
        "北京":   {"status": "clear",      "base_temp": 12.0, "rain_1h": 0.0, "humidity_bias": -25,"wind_bias": 4,  "desc": "北方晴"},
        "上海":   {"status": "cloudy",     "base_temp": 20.0, "rain_1h": 1.0, "humidity_bias": 5,  "wind_bias": 2,  "desc": "海洋性多云"},
        "广州":   {"status": "cloudy",     "base_temp": 27.0, "rain_1h": 1.5, "humidity_bias": 18, "wind_bias": 0,  "desc": "南方湿热"},
        "深圳":   {"status": "cloudy",     "base_temp": 26.0, "rain_1h": 1.0, "humidity_bias": 16, "wind_bias": 1,  "desc": "南方湿热"},
        "东京":   {"status": "cloudy",     "base_temp": 16.0, "rain_1h": 0.5, "humidity_bias": 0,  "wind_bias": 2,  "desc": "海洋性多云"},
        "京都":   {"status": "cloudy",     "base_temp": 14.0, "rain_1h": 0.8, "humidity_bias": 5,  "wind_bias": 1,  "desc": "温带多云"},
        "成都":   {"status": "cloudy",     "base_temp": 17.0, "rain_1h": 1.2, "humidity_bias": 12, "wind_bias": 0,  "desc": "盆地多云"},
    }

    # 通用模板（未命中预设的城市）
    GENERIC_PROFILE = {"status": "cloudy", "base_temp": 20.0, "rain_1h": 0.5, "humidity_bias": 5, "wind_bias": 1, "desc": "多云"}

    # 启动时给的"测试暴雨"——只在首次安装启动给一次
    BOOT_STORM = {"status": "thunderstorm", "base_temp": 22.0, "rain_1h": 18.5, "humidity_bias": 25, "wind_bias": 6, "desc": "暴雨伴雷电"}

    def __init__(self, persistence=None):
        # 接收 persistence 用于持久化首次 fetch 标记
        self._persistence = persistence

    def _match_profile(self, location: str) -> Dict[str, Any]:
        """根据 location 字符串匹配城市人格"""
        for keyword, profile in self.CITY_PROFILES.items():
            if keyword in location:
                return profile
        return self.GENERIC_PROFILE

    async def _consume_first_fetch(self) -> bool:
        """检查并消费首次 fetch 标记。返回 True 表示是首次 fetch"""
        if self._persistence is None:
            # 没有 persistence，回退到实例内标记（行为同旧版）
            if hasattr(self, "_first_fetch_done"):
                return False
            self._first_fetch_done = True
            return True
        # 用持久化标记，跨重启也保持
        flag = await self._persistence.load_first_fetch_done()
        if flag:
            return False
        await self._persistence.save_first_fetch_done(True)
        return True

    async def fetch(self, location: str) -> WeatherSnapshot:
        # 仅首次安装启动给暴雨，之后（含后续重启）都按城市人格派生
        is_first = await self._consume_first_fetch()
        profile = self.BOOT_STORM if is_first else self._match_profile(location)

        # 在 profile 基础上加随机抖动
        temp = profile["base_temp"] + random.uniform(-2.0, 2.0)
        rain = max(0.0, profile["rain_1h"] + random.uniform(-0.5, 1.0))
        humidity = max(10.0, min(100.0, 60 + profile["humidity_bias"] + random.uniform(-10, 15)))
        wind = max(0.0, profile["wind_bias"] + random.uniform(0, 3))

        # 雨量描述升级
        desc = profile["desc"]
        if rain >= 16:
            desc = "暴雨"
        elif rain >= 8:
            desc = "大雨"
        elif rain >= 2.5:
            desc = "中雨"
        elif rain >= 0.5:
            desc = "小雨"

        return WeatherSnapshot(
            location=location,
            status=profile["status"] if rain < 0.5 else "rain",
            description=desc,
            temperature=round(temp, 1),
            feels_like=round(temp + random.uniform(-2, 1), 1),
            humidity=round(humidity, 1),
            rain_1h=round(rain, 2),
            wind_speed=round(wind, 1),
            timestamp=datetime.now().timestamp(),
            source="mock",
        )


# ─────────────────────────────────────────────────────────────
# wttr.in Provider —— 真实 API，无需 key
# ─────────────────────────────────────────────────────────────

class WttrInProvider(WeatherProvider):
    """
    wttr.in 天气源（无需 API key，全球覆盖）。

    接口：https://wttr.in/{location}?format=j1
    返回 JSON：current_condition[0] 含温度/湿度/降水/天气描述等
    """

    # wttr.in 状态文本 → 我们内部 status 的映射
    WTTR_STATUS_MAP = {
        "clear": "clear",
        "sunny": "clear",
        "partly cloudy": "cloudy",
        "cloudy": "cloudy",
        "overcast": "cloudy",
        "mist": "fog",
        "fog": "fog",
        "patchy rain possible": "rain",
        "patchy light drizzle": "rain",
        "light drizzle": "rain",
        "patchy light rain": "rain",
        "light rain": "rain",
        "moderate rain at times": "rain",
        "moderate rain": "rain",
        "heavy rain at times": "rain",
        "heavy rain": "rain",
        "light freezing rain": "rain",
        "moderate or heavy freezing rain": "rain",
        "patchy snow possible": "snow",
        "patchy light snow": "snow",
        "light snow": "snow",
        "moderate snow": "snow",
        "heavy snow": "snow",
        "thundery outbreaks possible": "thunderstorm",
        "patchy light rain with thunder": "thunderstorm",
        "moderate or heavy rain with thunder": "thunderstorm",
    }

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def _parse_status(self, desc_en: str) -> str:
        """wttr.in 英文描述 → 内部状态码"""
        desc_lower = desc_en.lower().strip()
        for key, val in self.WttrInProvider.WTTR_STATUS_MAP.items():
            if key in desc_lower:
                return val
        return "cloudy"  # fallback

    def _fetch_sync(self, location: str) -> Dict[str, Any]:
        """同步抓取（包到 to_thread 里）"""
        # wttr.in 支持中文 location，URL 自动编码
        from urllib.parse import quote
        url = f"https://wttr.in/{quote(location)}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.79"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def fetch(self, location: str) -> Optional[WeatherSnapshot]:
        try:
            data = await asyncio.to_thread(self._fetch_sync, location)
            cur = data["current_condition"][0]
            desc_en = cur["weatherDesc"][0]["value"]
            status = self._parse_status(desc_en)
            # rain_1h: wttr.j1 没有"过去1小时降水量"，用 precipMM 当近似
            rain_1h = float(cur.get("precipMM", 0))
            # 风速：wttr 给 kmph，转 m/s
            wind_kph = float(cur.get("windspeedKmph", 0))
            wind_ms = round(wind_kph / 3.6, 1)
            # 真实地点（wttr 会反查 nearest_area）
            area = data.get("nearest_area", [{}])[0]
            real_name = area.get("areaName", [{}])[0].get("value", location)
            return WeatherSnapshot(
                location=real_name,
                status=status,
                description=desc_en,
                temperature=float(cur["temp_C"]),
                feels_like=float(cur.get("FeelsLikeC", cur["temp_C"])),
                humidity=float(cur["humidity"]),
                rain_1h=round(rain_1h, 2),
                wind_speed=wind_ms,
                timestamp=datetime.now().timestamp(),
                source="wttr",
            )
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
            logger.error(f"[WttrInProvider] fetch failed for {location}: {e}")
            return None
        except Exception as e:
            logger.error(f"[WttrInProvider] unexpected error: {e}")
            return None


# ─────────────────────────────────────────────────────────────
# 感官模块主类
# ─────────────────────────────────────────────────────────────

class SensoryModule:
    """
    感官系统主类。

    职责：
    - 管理所有外部传感器（目前只有 WeatherProvider，未来可加 AirQualityProvider 等）
    - 定期轮询，更新当前快照
    - 检测天气突变（雨声唤醒）
    - 暴露当前快照供其他模块（state_machine、emotional_state、lifestyle_context）使用

    配置项：
    - weather_provider: "mock" | "wttr"，选哪个 provider
    - location: 默认所在地（默认 "沙溪"）
    - poll_interval_minutes: 轮询间隔（默认 30）
    - rain_wake_threshold_mm: 雨声唤醒阈值（默认 2.5）
    """

    def __init__(self, config, persistence):
        self.config = config
        self.persistence = persistence
        self._location = config.get("location", "沙溪")
        self._current_weather: Optional[WeatherSnapshot] = None
        self._last_weather: Optional[WeatherSnapshot] = None
        self._poll_task: Optional[asyncio.Task] = None
        # 根据配置选 provider
        self._provider: WeatherProvider = self._create_provider()

    def _create_provider(self) -> WeatherProvider:
        provider_name = self.config.get("weather_provider", "mock")
        if provider_name == "wttr":
            logger.info("[SensoryModule] Using wttr.in provider")
            return WttrInProvider()
        logger.info("[SensoryModule] Using mock provider")
        # 传 persistence 给 mock provider，用于持久化"首次 fetch 已完成"标记
        return MockWeatherProvider(persistence=self.persistence)

    # ── 生命周期 ──

    async def start(self):
        """AstrBot 启动时调一次：恢复 location/weather + 启动轮询"""
        # 恢复持久化的 location
        saved_loc = await self.persistence.load_location()
        if saved_loc:
            self._location = saved_loc
            logger.info(f"[SensoryModule] Restored location: {saved_loc}")
        # 恢复上一次的天气（避免重启后状态丢失）
        saved_wx = await self.persistence.load_weather()
        if saved_wx:
            try:
                self._current_weather = WeatherSnapshot.from_dict(saved_wx)
                self._last_weather = self._current_weather
                logger.info(f"[SensoryModule] Restored weather: {self._current_weather.description}")
            except Exception as e:
                logger.error(f"[SensoryModule] Failed to restore weather: {e}")
        # 启动 30 分钟轮询
        self._poll_task = asyncio.create_task(self._poll_loop())
        # 立即拉一次（不等 30 分钟）
        await self.poll_now()
        logger.info(f"[SensoryModule] Started, location={self._location}, provider={type(self._provider).__name__}")

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    # ── 轮询 ──

    async def _poll_loop(self):
        interval = self.config.get("poll_interval_minutes", 60) * 60
        while True:
            await asyncio.sleep(interval)
            await self.poll_now()

    async def poll_now(self) -> Optional[WeatherSnapshot]:
        """立即拉一次天气，更新快照并持久化"""
        try:
            snapshot = await self._provider.fetch(self._location)
            if snapshot:
                self._last_weather = self._current_weather
                self._current_weather = snapshot
                await self.persistence.save_weather(snapshot.to_dict())
                logger.info(
                    f"[SensoryModule] Weather: {snapshot.location} {snapshot.description} "
                    f"{snapshot.temperature:.1f}°C rain={snapshot.rain_1h}mm"
                )
                return snapshot
            return None
        except Exception as e:
            logger.error(f"[SensoryModule] Poll error: {e}")
            return None

    # ── 用户指令 ──

    async def set_location(self, new_location: str) -> Optional[WeatherSnapshot]:
        """
        用户切换所在地。返回新的天气快照，失败返回 None。
        """
        old_location = self._location
        self._location = new_location
        snapshot = await self.poll_now()
        if snapshot is None:
            # 拉取失败，回滚
            self._location = old_location
            logger.warning(f"[SensoryModule] set_location failed, rolled back to {old_location}")
            return None
        await self.persistence.save_location(new_location)
        logger.info(f"[SensoryModule] Location: {old_location} → {new_location}")
        return snapshot

    # ── 雨声唤醒检测 ──

    def detect_rain_wake(self, threshold_mm: float = 2.5) -> bool:
        """
        检测雨声唤醒条件。

        触发条件（满足任一即可）：
        - 当前帧达到中雨及以上（rain_1h >= threshold_mm）
        - 从无雨突变到中雨以上（防漂移）

        阈值由配置传入，默认 2.5mm = 中雨起点。
        """
        if not self._current_weather:
            return False
        cur = self._current_weather
        # 当前已经达到中雨以上
        if cur.rain_1h >= threshold_mm and cur.status in ("rain", "thunderstorm"):
            return True
        # 突变检测：上次几乎无雨 → 这次中雨以上
        if self._last_weather:
            if self._last_weather.rain_1h < 0.5 and cur.rain_1h >= threshold_mm:
                return True
        return False

    # ── 属性 ──

    @property
    def location(self) -> str:
        return self._location

    @property
    def current_weather(self) -> Optional[WeatherSnapshot]:
        return self._current_weather

    @property
    def provider_name(self) -> str:
        return type(self._provider).__name__

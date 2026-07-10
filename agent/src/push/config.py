"""推送配置管理——存储监控币种、预警规则、推送时间等设置。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _config_path() -> Path:
    base = Path.home() / ".vibe-trading"
    base.mkdir(parents=True, exist_ok=True)
    return base / "push_config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "symbols": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
    "price_alerts": {
        "enabled": True,
        "threshold_percent": 5.0,  # 24h 涨跌幅超过此值触发预警
    },
    "hourly_push": {
        "enabled": True,
        "content": "price",  # price / price_and_news
    },
    "news_push": {
        "enabled": True,
        "times": ["08:00", "20:00"],  # 每天推送时间 (北京时间)
    },
}


def load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Merge with defaults for any missing keys
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    path = _config_path()
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def get_symbols() -> list[str]:
    return load_config().get("symbols", ["BTC-USDT", "ETH-USDT", "SOL-USDT"])

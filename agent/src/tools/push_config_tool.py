"""推送配置 AI 工具——AI 可以读取和修改飞书推送设置。"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.push import config as push_config


class GetPushConfigTool(BaseTool):
    """查看推送配置。"""

    name = "get_push_config"
    description = "查看当前飞书推送配置：监控币种、预警阈值、推送时间等。"
    parameters = {"type": "object", "properties": {}, "required": []}
    repeatable = True
    is_readonly = True

    def execute(self, **_: Any) -> str:
        try:
            cfg = push_config.load_config()
            return json.dumps({"status": "ok", "config": cfg}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


class UpdatePushConfigTool(BaseTool):
    """修改推送配置。"""

    name = "update_push_config"
    description = (
        "修改飞书推送配置。可以添加/删除监控币种、修改价格预警阈值、"
        "开关每小时推送、修改新闻推送时间。"
        "symbols: 监控币种列表，如 ['BTC-USDT', 'ETH-USDT', 'SOL-USDT']。"
        "price_alert_threshold: 24h涨跌幅触发预警的百分比，如 5.0。"
        "hourly_enabled: 是否开启每小时行情推送。"
        "news_enabled: 是否开启新闻推送。"
        "news_times: 新闻推送时间列表，如 ['08:00', '20:00']。"
        "enabled: 总开关，是否启用推送。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "description": "是否启用推送"},
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "监控币种列表",
            },
            "price_alert_threshold": {
                "type": "number",
                "description": "价格预警阈值(%)",
            },
            "hourly_enabled": {"type": "boolean", "description": "每小时行情推送"},
            "news_enabled": {"type": "boolean", "description": "新闻推送"},
            "news_times": {
                "type": "array",
                "items": {"type": "string"},
                "description": "新闻推送时间",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            cfg = push_config.load_config()

            if "enabled" in kwargs:
                cfg["enabled"] = bool(kwargs["enabled"])
            if "symbols" in kwargs:
                cfg["symbols"] = list(kwargs["symbols"])
            if "price_alert_threshold" in kwargs:
                cfg["price_alerts"]["threshold_percent"] = float(kwargs["price_alert_threshold"])
            if "hourly_enabled" in kwargs:
                cfg["hourly_push"]["enabled"] = bool(kwargs["hourly_enabled"])
            if "news_enabled" in kwargs:
                cfg["news_push"]["enabled"] = bool(kwargs["news_enabled"])
            if "news_times" in kwargs:
                cfg["news_push"]["times"] = list(kwargs["news_times"])

            push_config.save_config(cfg)
            return json.dumps({
                "status": "ok",
                "message": "推送配置已更新，将在下次检查时生效。",
                "config": cfg,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)

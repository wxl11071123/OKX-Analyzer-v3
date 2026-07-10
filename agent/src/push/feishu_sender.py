"""飞书消息发送——通过飞书 Webhook 或 Bot API 发送推送消息。"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _get_feishu_webhook() -> str | None:
    """从 agent.json 读取飞书 webhook URL（如果有的话）。"""
    config_path = Path.home() / ".vibe-trading" / "agent.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        return config.get("channels", {}).get("feishu", {}).get("webhook_url")
    except Exception:
        return None


def send_feishu_text(text: str) -> bool:
    """通过飞书 Webhook 发送文本消息。"""
    webhook = _get_feishu_webhook()
    if not webhook:
        logger.warning("飞书 webhook 未配置，跳过推送")
        return False

    try:
        resp = httpx.post(
            webhook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10,
        )
        return resp.is_success
    except Exception as e:
        logger.error(f"飞书推送失败: {e}")
        return False


def send_feishu_card(title: str, content: str, url: str = "") -> bool:
    """通过飞书 Webhook 发送卡片消息。"""
    webhook = _get_feishu_webhook()
    if not webhook:
        return False

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    if url:
        card["card"]["elements"].append({
            "tag": "action",
            "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"}, "url": url, "type": "default"}],
        })

    try:
        resp = httpx.post(webhook, json=card, timeout=10)
        return resp.is_success
    except Exception as e:
        logger.error(f"飞书卡片推送失败: {e}")
        return False


from pathlib import Path

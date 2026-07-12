"""飞书消息发送——通过飞书 Bot API 发送推送消息（无需 webhook）。

使用 app_id + app_secret 获取 tenant_access_token，
然后调用 send message API 发送消息到指定会话。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

FEISHU_API = "https://open.feishu.cn/open-apis"
_token_cache: dict[str, str | float] = {"token": "", "expires_at": 0}
_token_lock = threading.Lock()


def _get_feishu_creds() -> tuple[str, str, str, str]:
    """从 agent.json 读取飞书 app_id, app_secret, push_open_id, push_chat_id。"""
    config_path = Path.home() / ".vibe-trading" / "agent.json"
    if not config_path.exists():
        return "", "", "", ""

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        feishu = config.get("channels", {}).get("feishu", {})
        return (
            feishu.get("app_id", ""),
            feishu.get("app_secret", ""),
            feishu.get("push_open_id", ""),
            feishu.get("push_chat_id", ""),
        )
    except Exception:
        return "", "", "", ""


def _get_tenant_token() -> str:
    """获取飞书 tenant_access_token（带缓存）。"""
    import time

    with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return str(_token_cache["token"])

        app_id, app_secret, _, _ = _get_feishu_creds()
        if not app_id or not app_secret:
            return ""

        try:
            resp = httpx.post(
                f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                token = data["tenant_access_token"]
                _token_cache["token"] = token
                _token_cache["expires_at"] = now + data.get("expire", 7200) - 300
                return token
            else:
                logger.error(f"飞书 token 获取失败: {data}")
        except Exception as e:
            logger.error(f"飞书 token 请求异常: {e}")

        return ""


def _get_receive_id_and_type() -> tuple[str, str]:
    """获取推送接收者 ID 和类型。优先级: chat_id > open_id。"""
    _, _, open_id, chat_id = _get_feishu_creds()
    if chat_id:
        return chat_id, "chat_id"
    if open_id:
        return open_id, "open_id"
    return "", ""


def send_feishu_text(text: str) -> bool:
    """通过飞书 Bot API 发送文本消息。"""
    app_id, _, _, _ = _get_feishu_creds()
    receive_id, receive_type = _get_receive_id_and_type()
    if not app_id or not receive_id:
        logger.warning("飞书凭证或接收者 ID 未配置，跳过推送")
        return False

    token = _get_tenant_token()
    if not token:
        return False

    try:
        content = json.dumps({"text": text})
        resp = httpx.post(
            f"{FEISHU_API}/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": receive_id, "msg_type": "text", "content": content},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            return True
        else:
            logger.error(f"飞书消息发送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"飞书消息发送异常: {e}")
        return False


def send_feishu_card(title: str, content: str, url: str = "") -> bool:
    """通过飞书 Bot API 发送卡片消息。"""
    receive_id, receive_type = _get_receive_id_and_type()
    app_id, _, _, _ = _get_feishu_creds()
    if not app_id or not receive_id:
        return False

    token = _get_tenant_token()
    if not token:
        return False

    try:
        card_content = json.dumps({
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": content}],
        })
        body: dict = {"receive_id": receive_id, "msg_type": "interactive", "content": card_content}
        if url:
            body["content"] = json.dumps({
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
                "elements": [
                    {"tag": "markdown", "content": content},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"}, "url": url, "type": "default"}
                    ]},
                ],
            })

        resp = httpx.post(
            f"{FEISHU_API}/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        return resp.json().get("code") == 0
    except Exception as e:
        logger.error(f"飞书卡片发送失败: {e}")
        return False


def send_feishu_card_with_buttons(title: str, content: str, buttons: list[dict]) -> bool:
    """通过飞书 Bot API 发送带交互按钮的卡片消息。

    Args:
        title: 卡片标题
        content: 卡片正文（Markdown 格式）
        buttons: 按钮列表，每项格式:
            {"text": "按钮文字", "type": "primary"|"danger"|"default", "value": "action_key"}

    Returns:
        是否发送成功。
    """
    receive_id, receive_type = _get_receive_id_and_type()
    app_id, _, _, _ = _get_feishu_creds()
    if not app_id or not receive_id:
        return False

    token = _get_tenant_token()
    if not token:
        return False

    try:
        actions = []
        for btn in buttons:
            btn_type = btn.get("type", "default")
            # 飞书卡片按钮类型映射
            feishu_type = btn_type if btn_type in ("primary", "danger", "default") else "default"
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn.get("text", "")},
                "type": feishu_type,
                "value": json.dumps(btn.get("value", "")),
            })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "action", "actions": actions},
            ],
        }
        card_content = json.dumps(card, ensure_ascii=False)

        resp = httpx.post(
            f"{FEISHU_API}/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": receive_id, "msg_type": "interactive", "content": card_content},
            timeout=10,
        )
        return resp.json().get("code") == 0
    except Exception as e:
        logger.error(f"飞书交互按钮卡片发送失败: {e}")
        return False

"""OKX 只读 API —— 账户余额 + 持仓查询。

直接调用 OKX REST API，不依赖 trading connector 的复杂配置。
使用 .env 中的 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

BASE_URL = "https://www.okx.com"


def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    secret = os.getenv("OKX_API_SECRET", "")
    prehash = timestamp + method.upper() + path + body
    mac = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _headers(method: str, path: str, body: str = "") -> dict[str, str]:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if ts.endswith("+00:00"):
        ts = ts.replace("+00:00", "Z")
    return {
        "OK-ACCESS-KEY": os.getenv("OKX_API_KEY", ""),
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.getenv("OKX_PASSPHRASE", ""),
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(os.getenv("OKX_API_KEY") and os.getenv("OKX_API_SECRET") and os.getenv("OKX_PASSPHRASE"))


def get_account_balance() -> dict[str, Any]:
    """获取账户余额（总权益、可用余额、未实现盈亏）。"""
    path = "/api/v5/account/balance"
    resp = httpx.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    data = resp.json()

    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")

    total_eq = 0.0
    avail = 0.0
    upl = 0.0

    for item in data.get("data", []):
        for detail in item.get("details", []):
            if detail.get("ccy") == "USDT":
                total_eq = float(detail.get("eq", 0))
                avail = float(detail.get("availBal", 0))
                upl = float(detail.get("upl", 0))
                break

    return {
        "total_equity": total_eq,
        "available_balance": avail,
        "unrealized_pnl": upl,
        "realized_pnl": 0.0,  # OKX balance API doesn't provide this directly
        "currency": "USDT",
    }


def get_positions(inst_type: str = "SWAP") -> list[dict[str, Any]]:
    """获取当前持仓。"""
    path = f"/api/v5/account/positions?instType={inst_type}"
    resp = httpx.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    data = resp.json()

    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")

    result = []
    for item in data.get("data", []):
        pos_side = item.get("posSide", "net")
        pos_qty = float(item.get("pos", 0))
        # Skip zero positions
        if abs(pos_qty) < 0.0001:
            continue

        side = "long" if (pos_side == "long" or (pos_side == "net" and pos_qty > 0)) else "short"
        result.append({
            "symbol": item.get("instId", ""),
            "side": side,
            "quantity": abs(pos_qty),
            "avg_price": float(item.get("avgPx", 0)),
            "mark_price": float(item.get("markPx", 0)),
            "unrealized_pnl": float(item.get("upl", 0)),
            "unrealized_pnl_pct": float(item.get("uplRatio", 0)) * 100,
            "notional": float(item.get("notionalUsd", 0)),
        })

    # Also fetch SPOT balances as "positions"
    path_spot = "/api/v5/account/balance"
    resp_spot = httpx.get(BASE_URL + path_spot, headers=_headers("GET", path_spot), timeout=10)
    data_spot = resp_spot.json()

    if data_spot.get("code") == "0":
        for item in data_spot.get("data", []):
            for detail in item.get("details", []):
                qty = float(detail.get("availBal", 0))
                if qty > 0.0001 and detail.get("ccy") != "USDT":
                    result.append({
                        "symbol": f"{detail['ccy']}-USDT",
                        "side": "long",
                        "quantity": qty,
                        "avg_price": 0,
                        "mark_price": 0,
                        "unrealized_pnl": 0,
                        "unrealized_pnl_pct": 0,
                        "notional": float(detail.get("eqUsd", 0)),
                    })

    return result

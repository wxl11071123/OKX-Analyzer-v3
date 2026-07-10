"""OKX 账户工具——通过 HTTP relay 直接查询（不依赖 trading connector）。"""

from __future__ import annotations

import json, os
from typing import Any

import httpx
from src.agent.tools import BaseTool

RELAY = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")


class PortfolioTool(BaseTool):
    """查看 OKX 账户余额和持仓。"""

    name = "okx_portfolio"
    description = (
        "查看你的 OKX 账户总权益、可用余额、持仓列表（现货+合约）。"
        "包含每笔持仓的均价、标记价、未实现盈亏。数据来自 OKX API。"
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    repeatable = True
    is_readonly = True

    def execute(self, **_: Any) -> str:
        try:
            account = httpx.get(f"{RELAY}/api/v5/account/balance", headers=_okx_headers("GET", "/api/v5/account/balance"), timeout=20).json()
            positions = httpx.get(f"{RELAY}/api/v5/account/positions?instType=SWAP", headers=_okx_headers("GET", "/api/v5/account/positions?instType=SWAP"), timeout=20).json()

            result = {"account": _parse_balance(account), "positions": [], "spot_holdings": []}

            for item in positions.get("data", []):
                pos = float(item.get("pos") or 0)
                if abs(pos) < 0.0001:
                    continue
                side = "long" if pos > 0 else "short"
                result["positions"].append({
                    "symbol": item["instId"], "side": side,
                    "quantity": abs(pos), "avg_price": float(item.get("avgPx") or 0),
                    "mark_price": float(item.get("markPx") or 0),
                    "unrealized_pnl": float(item.get("upl") or 0),
                    "pnl_pct": f"{float(item.get('uplRatio') or 0) * 100:.2f}%",
                })

            # Spot balances
            for detail in account.get("data", [{}])[0].get("details", []):
                qty = float(detail.get("availBal") or 0)
                ccy = detail.get("ccy", "")
                if qty > 0.0001 and ccy != "USDT":
                    usd = float(detail.get("eqUsd") or 0)
                    result["spot_holdings"].append({
                        "currency": ccy, "quantity": qty,
                        "usd_value": round(usd, 2),
                        "price": round(usd / qty, 4) if qty > 0 else 0,
                    })

            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def _okx_headers(method: str, path: str) -> dict:
    import base64, hashlib, hmac
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    secret = os.getenv("OKX_API_SECRET", "")
    prehash = ts + method.upper() + path
    sign = base64.b64encode(hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    return {
        "OK-ACCESS-KEY": os.getenv("OKX_API_KEY", ""),
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.getenv("OKX_PASSPHRASE", ""),
        "Content-Type": "application/json",
    }


def _parse_balance(data: dict) -> dict:
    item = data.get("data", [{}])[0]
    return {
        "total_equity": round(float(item.get("totalEq") or 0), 2),
        "available_usdt": round(sum(float(d.get("availBal") or 0) for d in item.get("details", []) if d.get("ccy") == "USDT"), 2),
        "unrealized_pnl": round(float(item.get("upl") or 0), 2),
    }

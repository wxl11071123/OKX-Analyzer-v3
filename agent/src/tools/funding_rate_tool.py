"""OKX 资金费率工具 -- 通过 relay 查询永续合约资金费率。"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from src.agent.tools import BaseTool

RELAY = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")


class FundingRateTool(BaseTool):
    """查询 OKX 永续合约资金费率。"""

    name = "okx_funding_rate"
    description = (
        "查询 OKX 永续合约当前和历史资金费率。"
        "返回当前费率、年化费率、下次结算时间。"
        "用于判断市场拥挤度：费率极高正=多头拥挤，费率极负=空头拥挤。"
        "数据来自 OKX API，实时准确。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "instId": {
                "type": "string",
                "description": "永续合约 ID，如 BTC-USDT-SWAP、ETH-USDT-SWAP",
            },
            "history": {
                "type": "boolean",
                "description": "是否查询历史费率（最近30条）。默认 false 只查当前。",
                "default": False,
            },
        },
        "required": ["instId"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        inst_id = kwargs["instId"]
        history = kwargs.get("history", False)

        try:
            if history:
                url = f"{RELAY}/api/v5/public/funding-rate-history"
                params = {"instId": inst_id, "limit": "30"}
            else:
                url = f"{RELAY}/api/v5/public/funding-rate"
                params = {"instId": inst_id}

            resp = httpx.get(url, params=params, timeout=15)
            data = resp.json()

            if data.get("code") != "0":
                return json.dumps({
                    "status": "error",
                    "error": data.get("msg", "unknown error"),
                }, ensure_ascii=False)

            results = []
            for item in data.get("data", []):
                rate = float(item.get("fundingRate") or 0)
                annualized = rate * 3 * 365 * 100  # 8h -> 年化百分比
                results.append({
                    "instId": item.get("instId"),
                    "fundingRate_8h": f"{rate * 100:.6f}%",
                    "annualized": f"{annualized:.2f}%",
                    "fundingTime": item.get("fundingTime"),
                    "settFundingRate": item.get("settFundingRate"),
                    "signal": _rate_signal(rate),
                })

            return json.dumps({
                "status": "ok",
                "instId": inst_id,
                "current": results[0] if results else None,
                "history": results if history else None,
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": str(e),
            }, ensure_ascii=False)


def _rate_signal(rate: float) -> str:
    """根据费率返回拥挤度信号。"""
    if rate > 0.001:
        return "多头极度拥挤，警惕反转"
    elif rate > 0.0003:
        return "多头偏多"
    elif rate > -0.0001:
        return "中性"
    elif rate > -0.0005:
        return "空头偏多"
    else:
        return "空头极度拥挤，警惕轧空"

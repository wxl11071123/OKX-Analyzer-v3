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

# 缓存（避免每次请求都打 OKX API）
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 30  # 30 秒缓存


def _cached(key: str, fetcher):
    """带缓存的 API 调用。"""
    import time
    now = time.time()
    if key in _cache:
        ts, val = _cache[key]
        if now - ts < CACHE_TTL:
            return val
    val = fetcher()
    _cache[key] = (now, val)
    return val


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
    """获取账户余额（带缓存）。"""
    return _cached("balance", _fetch_account_balance)


def _fetch_account_balance() -> dict[str, Any]:
    path = "/api/v5/account/balance"
    resp = httpx.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
    data = resp.json()

    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")

    total_eq = 0.0
    avail_usdt = 0.0
    upl = 0.0

    for item in data.get("data", []):
        total_eq += float(item.get("totalEq") or 0)
        upl += float(item.get("upl") or 0)
        for detail in item.get("details", []):
            if detail.get("ccy") == "USDT":
                avail_usdt = float(detail.get("availBal") or 0)

    return {
        "total_equity": round(total_eq, 2),
        "available_balance": round(avail_usdt, 2),
        "unrealized_pnl": round(upl, 2),
        "realized_pnl": 0.0,
        "currency": "USDT",
    }


def get_positions(inst_type: str = "SWAP") -> list[dict[str, Any]]:
    """获取当前持仓（带缓存）。"""
    return _cached(f"positions_{inst_type}", lambda: _fetch_positions(inst_type))


def _fetch_positions(inst_type: str = "SWAP") -> list[dict[str, Any]]:
    result = []

    # 1. 合约持仓（SWAP）
    if inst_type != "SPOT":
        path = f"/api/v5/account/positions?instType={inst_type}"
        resp = httpx.get(BASE_URL + path, headers=_headers("GET", path), timeout=10)
        data = resp.json()

        if data.get("code") == "0":
            for item in data.get("data", []):
                pos_side = item.get("posSide", "net")
                pos_qty = float(item.get("pos") or 0)
                if abs(pos_qty) < 0.0001:
                    continue

                side = "long" if (pos_side == "long" or (pos_side == "net" and pos_qty > 0)) else "short"
                result.append({
                    "symbol": item.get("instId", ""),
                    "side": side,
                    "quantity": abs(pos_qty),
                    "avg_price": float(item.get("avgPx") or 0),
                    "mark_price": float(item.get("markPx") or 0),
                    "unrealized_pnl": float(item.get("upl") or 0),
                    "unrealized_pnl_pct": float(item.get("uplRatio") or 0) * 100,
                    "notional": float(item.get("notionalUsd") or 0),
                })

    # 2. 现货持仓 + 成本价计算
    cost_basis = _calc_spot_cost_basis()
    path_spot = "/api/v5/account/balance"
    resp_spot = httpx.get(BASE_URL + path_spot, headers=_headers("GET", path_spot), timeout=10)
    data_spot = resp_spot.json()

    if data_spot.get("code") == "0":
        for item in data_spot.get("data", []):
            for detail in item.get("details", []):
                qty = float(detail.get("availBal") or 0)
                ccy = detail.get("ccy", "")
                if qty > 0.0001 and ccy != "USDT":
                    usd_value = float(detail.get("eqUsd") or 0)
                    mark_price = usd_value / qty if qty > 0 else 0
                    cb = cost_basis.get(ccy, {})
                    avg_price = cb.get("avg_price", 0)
                    total_cost = cb.get("total_cost", 0)
                    pnl = (mark_price - avg_price) * qty if avg_price > 0 else 0
                    pnl_pct = ((mark_price - avg_price) / avg_price * 100) if avg_price > 0 else 0

                    result.append({
                        "symbol": f"{ccy}-USDT",
                        "side": "long",
                        "quantity": qty,
                        "avg_price": round(avg_price, 4),
                        "mark_price": round(mark_price, 4),
                        "unrealized_pnl": round(pnl, 4),
                        "unrealized_pnl_pct": round(pnl_pct, 2),
                        "notional": usd_value,
                    })

    return result


def _calc_spot_cost_basis() -> dict[str, dict[str, float]]:
    """从成交记录计算现货持仓成本价。

    Returns:
        {ccy: {"avg_price": x, "total_cost": y, "total_qty": z}}
    """
    try:
        from src.trade_log import db as trade_db
        db.init_db()
        trades = trade_db.query_trades(inst_type="SPOT", limit=1000)
    except Exception:
        return {}

    if not trades:
        return {}

    basis: dict[str, dict[str, float]] = {}
    for t in trades:
        symbol = t.get("symbol", "")
        if not symbol.endswith("-USDT"):
            continue
        ccy = symbol.replace("-USDT", "")
        side = t.get("side", "")
        price = float(t.get("price") or 0)
        qty = float(t.get("quantity") or 0)
        if price <= 0 or qty <= 0:
            continue

        if ccy not in basis:
            basis[ccy] = {"total_cost": 0, "total_qty": 0, "avg_price": 0}

        if side == "buy":
            basis[ccy]["total_cost"] += price * qty
            basis[ccy]["total_qty"] += qty
        elif side == "sell":
            # 卖出时按比例减少持仓成本
            if basis[ccy]["total_qty"] > 0:
                ratio = qty / basis[ccy]["total_qty"]
                basis[ccy]["total_cost"] *= (1 - ratio)
                basis[ccy]["total_qty"] = max(0, basis[ccy]["total_qty"] - qty)

        if basis[ccy]["total_qty"] > 0:
            basis[ccy]["avg_price"] = basis[ccy]["total_cost"] / basis[ccy]["total_qty"]

    return basis

"""全凯利仓位计算器。

从实盘交易日志（trade_log.db）读取最近 N 笔已平仓交易，
计算胜率 p 和盈亏比 b，输出 f_kelly 和 position_capital。

冷启动：交易数 < 10 时用回测先验值 p=0.57, b=1.1。
f_kelly 分级：>0.10 实盘 / 0~0.10 地板(10%) / <0 预警(仍地板)。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from src.trade_log.db import _get_conn

logger = logging.getLogger(__name__)

FALLBACK_P = 0.57
FALLBACK_B = 1.1
MIN_TRADES = 10
DEFAULT_LOOKBACK = 100
KELLY_FRACTION = 1.0
FLOOR_PCT = 0.10
KELLY_START_TIME_DEFAULT = "2026-07-01T00:00:00Z"


def _get_start_ts() -> int:
    """KELLY_START_TIME 环境变量转换为 epoch 秒。"""
    raw = os.getenv("KELLY_START_TIME", KELLY_START_TIME_DEFAULT)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())


class KellyCalculator:
    """全凯利仓位动态计算器。

    Usage::

        kelly = KellyCalculator(lookback=100)
        result = kelly.calculate()
        # result["f_kelly"] -> 凯利比例
        # result["position_capital"] -> 单笔保证金
        # result["status"] -> "normal" / "floor" / "warning"
    """

    def __init__(self, lookback: int = DEFAULT_LOOKBACK):
        self.lookback = lookback

    def calculate(self) -> dict[str, Any]:
        """计算凯利仓位（含分级处理）。"""
        raw = self._compute_from_db()
        if raw is None:
            raw = self._fallback_raw()

        equity = raw["equity"]
        f_kelly = raw["f_kelly"]
        rules = _apply_kelly_rules(f_kelly, equity)

        return {
            "p": raw["p"],
            "b": raw["b"],
            "f_kelly": round(f_kelly, 4),
            "equity": round(equity, 2),
            "position_capital": round(rules["position_capital"], 2),
            "status": rules["status"],
            "trades_used": raw.get("trades_used", 0),
            "use_fallback": raw.get("use_fallback", True),
        }

    def _compute_from_db(self) -> dict[str, Any] | None:
        """从 trade_log.db 读取数据计算，过滤 KELLY_START_TIME 之前的记录。"""
        try:
            start_ts = _get_start_ts()
            conn = _get_conn()
            rows = conn.execute(
                "SELECT * FROM trade_log WHERE pnl != 0 AND fill_time >= ? "
                "ORDER BY fill_time DESC LIMIT ?",
                (start_ts, self.lookback),
            ).fetchall()
            conn.close()

            if len(rows) < MIN_TRADES:
                logger.info(
                    "凯利计算: 仅 %d 笔已平仓交易（需 >=%d），使用冷启动值",
                    len(rows), MIN_TRADES,
                )
                return None

            wins = [r["pnl"] for r in rows if r["pnl"] > 0]
            losses = [abs(r["pnl"]) for r in rows if r["pnl"] < 0]

            if not wins or not losses:
                logger.info("凯利计算: 缺少赢/输样本，使用冷启动值")
                return None

            p = len(wins) / (len(wins) + len(losses))
            avg_win = sum(wins) / len(wins)
            avg_loss = sum(losses) / len(losses)
            b = avg_win / avg_loss if avg_loss > 0 else 0

            if b <= 0:
                logger.info("凯利计算: 盈亏比 <= 0，使用冷启动值")
                return None

            f_kelly = p - (1 - p) / b
            equity = self._get_equity()

            return {
                "p": round(p, 4),
                "b": round(b, 4),
                "f_kelly": f_kelly,
                "equity": equity,
                "trades_used": len(rows),
                "use_fallback": False,
            }
        except Exception:
            logger.exception("凯利计算失败")
            return None

    def _fallback_raw(self) -> dict[str, Any]:
        """冷启动原始值（分级前）。"""
        p = FALLBACK_P
        b = FALLBACK_B
        f_kelly = p - (1 - p) / b
        equity = self._get_equity()

        return {
            "p": p,
            "b": b,
            "f_kelly": f_kelly,
            "equity": equity,
            "trades_used": 0,
            "use_fallback": True,
        }

    @staticmethod
    def _get_equity() -> float:
        """获取 OKX 账户当前总权益。"""
        try:
            import httpx

            relay = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")
            resp = httpx.get(
                f"{relay}/api/v5/account/balance",
                headers=_okx_headers("GET", "/api/v5/account/balance"),
                timeout=10,
            )
            data = resp.json()
            item = data.get("data", [{}])[0]
            return float(item.get("totalEq") or 0)
        except Exception:
            logger.warning("获取权益失败")
            return 0.0


def _apply_kelly_rules(f_kelly: float, equity: float) -> dict[str, Any]:
    """f_kelly 分级处理。

    - f_kelly > 0.10: 用实际值 (normal)
    - 0 <= f_kelly <= 0.10: 10% 地板 (floor)
    - f_kelly < 0: 10% 地板 + 预警 (warning)
    """
    if f_kelly > 0.10:
        position_capital = equity * f_kelly
        status = "normal"
    elif f_kelly >= 0:
        position_capital = equity * FLOOR_PCT
        status = "floor"
    else:
        position_capital = equity * FLOOR_PCT
        status = "warning"

    return {"position_capital": position_capital, "status": status}


def f_kelly_negative_days() -> int:
    """统计 f_kelly 连续为负的天数（基于 trade_log 最近 14 天）。

    用于 executor 中检测是否需要自动 halt。
    """
    start_ts = _get_start_ts()
    now = int(time.time())
    lookback_window = 14 * 86400

    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT pnl, fill_time FROM trade_log "
            "WHERE pnl != 0 AND fill_time >= ? AND fill_time >= ? "
            "ORDER BY fill_time DESC LIMIT 500",
            (start_ts, now - lookback_window),
        ).fetchall()
        conn.close()

        if not rows:
            return 0

        wins = sum(1 for r in rows if r["pnl"] > 0)
        losses = sum(1 for r in rows if r["pnl"] < 0)
        total = wins + losses
        if total < 10:
            return 0

        p = wins / total
        if p == 0 or losses == 0:
            return 14 if p < 0.5 else 0

        avg_win = sum(r["pnl"] for r in rows if r["pnl"] > 0) / max(1, wins)
        avg_loss = abs(sum(r["pnl"] for r in rows if r["pnl"] < 0)) / max(1, losses)
        b = avg_win / avg_loss if avg_loss > 0 else 0

        if b <= 0:
            return 14

        fk = p - (1 - p) / b
        if fk < -0.05:
            return 14
        return 0
    except Exception:
        return 0


def _okx_headers(method: str, path: str) -> dict:
    import base64
    import hashlib
    import hmac
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    secret = os.getenv("OKX_API_SECRET", "")
    prehash = ts + method.upper() + path
    sign = base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": os.getenv("OKX_API_KEY", ""),
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.getenv("OKX_PASSPHRASE", ""),
        "Content-Type": "application/json",
    }
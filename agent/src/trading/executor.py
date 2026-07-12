"""TSMOM 策略交易执行器。

主循环（60s 轮询）：
- 每 4H：检查入场信号（TSMOM + Hurst）
- 每 1H：检查持仓 EMA20 止损
- 每次开/平仓：风控红线检查 + halt 自动触发 + 飞书通知 + 交易日志

状态持久化：~/.vibe-trading/trading_state.json
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests as req

from src.indicators.ta import compute_ema, compute_hurst
from src.live.halt import halt_flag_set, trip_halt
from src.push.feishu_sender import send_feishu_card_with_buttons
from src.trade_log.db import _get_conn, init_db
from src.trading.connectors.okx.sdk import (
    _sway_auth_headers,
    calc_swap_sz,
    place_swap_order,
    place_swap_stop_order,
    set_swap_leverage,
)
from src.trading.kelly import KellyCalculator

logger = logging.getLogger(__name__)

RELAY = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")
OKX_API = f"{RELAY}/api/v5"
STATE_FILE = Path.home() / ".vibe-trading" / "trading_state.json"
TZ_UTC = timezone.utc

ENTRY_INTERVAL_HOURS = 4
STOP_CHECK_MINUTES = 60

DAILY_LOSS_PCT = 0.06
MAX_CONSECUTIVE_LOSSES = 3


@dataclass
class TradingState:
    """交易引擎持久化状态。"""

    position: dict | None = None
    daily_pnl: float = 0.0
    daily_pnl_date: str = ""
    consecutive_losses: int = 0
    last_signal_at: str = ""


@dataclass
class ExecutorConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    initial_capital: float = 150.0
    max_positions: int = 1
    is_live: bool = False


class TradingExecutor:
    """TSMOM 策略自动化交易执行器。"""

    def __init__(self, config: ExecutorConfig):
        self.cfg = config
        self.kelly = KellyCalculator(lookback=100)
        self.state = self._load_state()
        self._stop_flag = False
        self._last_entry_check = 0.0
        self._last_stop_check = 0.0
        self._last_symbol_refresh = 0.0
        init_db()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self):
        """主循环。"""
        logger.info(
            "TradingExecutor 启动, symbols=%s, capital=%.1f, live=%s",
            self.cfg.symbols, self.cfg.initial_capital, self.cfg.is_live,
        )
        self._send_startup_card()

        while not self._stop_flag:
            try:
                if halt_flag_set():
                    logger.warning("halt 已触发，跳过交易循环")
                    time.sleep(60)
                    continue

                now = time.time()
                if self.state.position:
                    self._reconcile_position()
                    self._check_stop_loss()
                    self._check_signal_reversal()
                else:
                    if now - self._last_entry_check >= ENTRY_INTERVAL_HOURS * 3600:
                        self._check_entry_signal()
                        self._last_entry_check = now

                time.sleep(60)
            except Exception:
                logger.exception("主循环异常")
                time.sleep(60)

    def stop(self):
        self._stop_flag = True

    # ------------------------------------------------------------------
    # 持仓对账
    # ------------------------------------------------------------------

    def _reconcile_position(self):
        """与 OKX 对账，确认持仓完整性。

        每轮主循环执行，HTTP GET 仅 1-2 秒。
        - OKX 返回无匹配 → 本地持仓清零
        - 数量不一致 → 以 OKX 为准
        - 网络异常 → 跳过本轮（下次对账）
        """
        if not self.state.position:
            return
        sym = self.state.position["symbol"]
        direction = self.state.position["direction"]
        try:
            resp = req.get(
                f"{OKX_API}/account/positions",
                params={"instId": sym},
                headers=_sway_auth_headers("GET", "/api/v5/account/positions"),
                timeout=10,
            )
            data = resp.json()
            positions = data.get("data", [])

            matched = None
            expected_pos_side = "long" if direction == "long" else "short"
            for p in positions:
                if p.get("instId") == sym and p.get("posSide") == expected_pos_side:
                    matched = p
                    break

            if matched is None:
                logger.warning("持仓对账: %s 在 OKX 已不存在，清除本地状态", sym)
                self.state.position = None
                self._save_state()
                return

            okx_qty = int(float(matched.get("pos", 0)))
            local_qty = self.state.position["size"]
            if okx_qty != local_qty:
                logger.warning(
                    "持仓对账: %s 数量不一致 (本地=%d, OKX=%d)，修正为 OKX 值",
                    sym, local_qty, okx_qty,
                )
                if okx_qty == 0:
                    self.state.position = None
                else:
                    self.state.position["size"] = okx_qty
                self._save_state()

        except Exception:
            logger.exception("持仓对账失败，跳过本轮")

    # ------------------------------------------------------------------
    # 入场信号
    # ------------------------------------------------------------------

    def _check_entry_signal(self):
        """检查 TSMOM 入场信号。"""
        now = time.time()
        if now - self._last_symbol_refresh > 86400:
            self._refresh_symbols()
            self._last_symbol_refresh = now

        logger.info("检查入场信号...")
        for sym in self.cfg.symbols:
            try:
                df = self._fetch_klines(sym, 300, "4H")
                if df is None or len(df) < 200:
                    continue
                close = df["close"]
                tsmom_ret = float(close.pct_change(120).iloc[-1])
                if pd.isna(tsmom_ret):
                    continue
                hurst = compute_hurst(close, 200)
                hurst_val = float(hurst.iloc[-1])
                if pd.isna(hurst_val) or hurst_val <= 0.55:
                    continue

                direction = "long" if tsmom_ret > 0 else "short"
                if tsmom_ret == 0:
                    continue

                logger.info(
                    "入场信号: %s %s TSMOM=%.1f%% Hurst=%.3f price=%.4f",
                    sym, direction, tsmom_ret * 100, hurst_val, close.iloc[-1],
                )
                self._open_position({
                    "symbol": sym,
                    "direction": direction,
                    "tsmom_pct": round(tsmom_ret * 100, 2),
                    "hurst": round(hurst_val, 4),
                    "entry_price": float(close.iloc[-1]),
                    "atr": self._calc_atr(df),
                })
                self._last_entry_check = time.time()
                self.state.last_signal_at = datetime.now(TZ_UTC).isoformat()
                return
            except Exception:
                logger.debug("%s 信号检查失败", sym, exc_info=True)

    def _refresh_symbols(self):
        """从选币结果动态更新交易标的列表（每日执行）。"""
        try:
            from src.tools.coin_scanner_tool import CoinScannerTool

            cs = CoinScannerTool()
            result = cs.execute(top_n=5)
            data = json.loads(result)
            candidates = data.get("candidates", [])
            if candidates:
                new_symbols = [c["symbol"] for c in candidates]
                self.cfg.symbols = new_symbols
                logger.info("选币刷新: %s", new_symbols)
            else:
                logger.info("选币刷新: 无候选，保留现有列表")
        except Exception:
            logger.exception("选币刷新失败，保留已有列表")

    # ------------------------------------------------------------------
    # 开仓
    # ------------------------------------------------------------------

    def _open_position(self, candidate: dict):
        """开仓：halt→kelly→leverage→order→stop order→feishu→log。"""
        if halt_flag_set():
            logger.warning("halt 已触发，跳过开仓")
            return
        if self.state.position:
            logger.info("已有持仓 %s，跳过开仓", self.state.position["symbol"])
            return

        sym = candidate["symbol"]
        direction = candidate["direction"]
        entry_price = candidate["entry_price"]

        # Kelly sizing
        kelly_r = self.kelly.calculate()
        equity = kelly_r.get("equity", self.cfg.initial_capital)
        f_quarter = kelly_r["f_quarter"]
        position_capital = equity * f_quarter
        leverage = 5
        notional = position_capital * leverage
        sz, ct_val = calc_swap_sz(notional, sym)

        if sz < 1:
            logger.warning("名义价值 %.1f 不足以开 1 张 %s (ctVal=%.2f)", notional, sym, ct_val)
            return

        # 双向持仓方向映射
        pos_side = "long" if direction == "long" else "short"
        side = "buy" if direction == "long" else "sell"

        logger.info("开仓: %s %s sz=%d price=%.4f notional=%.1f", sym, direction, sz, entry_price, notional)

        # 1. 设置杠杆
        lev_r = set_swap_leverage(symbol=sym, lever=str(leverage))
        if lev_r["status"] != "ok":
            logger.error("设置杠杆失败: %s", lev_r.get("error"))
            return

        # 2. 市价开仓
        order_r = place_swap_order(symbol=sym, side=side, pos_side=pos_side, sz=str(sz))
        if order_r["status"] != "ok":
            logger.error("开仓失败: %s", order_r.get("error"))
            return

        # 3. 硬止损（ATR × 3，防崩溃）
        atr = candidate.get("atr", 0)
        if atr > 0:
            stop_price = entry_price - 3 * atr if direction == "long" else entry_price + 3 * atr
            stop_side = "sell" if direction == "long" else "buy"
            stop_r = place_swap_stop_order(
                symbol=sym, side=stop_side, pos_side=pos_side,
                sz=str(sz), stop_price=str(round(stop_price, 6)),
            )
            if stop_r["status"] == "ok":
                logger.info("止损单已设置: algo_id=%s stop=%.4f", stop_r.get("algo_id"), stop_price)
            else:
                logger.error("止损单设置失败: %s", stop_r.get("error"))
            stop_order_id = stop_r.get("algo_id") or stop_r.get("order_id", "")
        else:
            stop_order_id = ""

        # 4. 状态持久化
        self.state.position = {
            "symbol": sym,
            "direction": direction,
            "pos_side": pos_side,
            "entry_price": entry_price,
            "entry_at": datetime.now(TZ_UTC).isoformat(),
            "size": sz,
            "leverage": leverage,
            "stop_order_id": stop_order_id,
        }
        self._save_state()

        # 5. 飞书通知 + 交易日志
        self._log_trade(sym, direction, entry_price, sz, "open")
        self._send_trade_card(sym, direction, entry_price, sz, str(order_r.get("order_id", "")))

    # ------------------------------------------------------------------
    # 止损 / 平仓
    # ------------------------------------------------------------------

    def _check_stop_loss(self):
        """检查 EMA20 止损（每 1H）。"""
        if not self.state.position:
            return
        pos = self.state.position
        sym = pos["symbol"]
        direction = pos["direction"]

        df = self._fetch_klines(sym, 100, "1H")
        if df is None or len(df) < 20:
            return

        close = df["close"]
        ema20 = compute_ema(close, 20)
        current = float(close.iloc[-1])
        ema = float(ema20.iloc[-1])

        if direction == "long" and current < ema:
            logger.info("EMA20 止损触发: %s long 现价%.4f < EMA20 %.4f", sym, current, ema)
            self._close_position("EMA20止损")
        elif direction == "short" and current > ema:
            logger.info("EMA20 止损触发: %s short 现价%.4f > EMA20 %.4f", sym, current, ema)
            self._close_position("EMA20止损")
        self._last_stop_check = time.time()

    def _check_signal_reversal(self):
        """检查 TSMOM 信号是否反转（每 4H）。"""
        if not self.state.position:
            return
        pos = self.state.position
        sym = pos["symbol"]
        direction = pos["direction"]

        df = self._fetch_klines(sym, 300, "4H")
        if df is None or len(df) < 200:
            return

        close = df["close"]
        tsmom_ret = float(close.pct_change(120).iloc[-1])
        if pd.isna(tsmom_ret):
            return

        if (direction == "long" and tsmom_ret < 0) or (direction == "short" and tsmom_ret > 0):
            logger.info("TSMOM 信号反转: %s direction=%s tsmom=%.1f%%", sym, direction, tsmom_ret * 100)
            self._close_position("TSMOM反转")

    def _close_position(self, reason: str):
        """平仓：market order → cancel stop → pnl → risk check → feishu → log。"""
        pos = self.state.position
        sym = pos["symbol"]
        direction = pos["direction"]
        sz = str(pos["size"])
        entry_price = pos["entry_price"]
        pos_side = pos["pos_side"]

        # 平仓方向和开仓相反
        close_side = "sell" if direction == "long" else "buy"

        order_r = place_swap_order(symbol=sym, side=close_side, pos_side=pos_side, sz=str(sz))
        if order_r["status"] != "ok":
            logger.error("平仓失败: %s", order_r.get("error"))
            return

        # 获取当前价格算 PnL
        current_price = self._get_current_price(sym)
        if direction == "long":
            pnl = (current_price - entry_price) * pos["size"] * self._get_ct_val(sym)
        else:
            pnl = (entry_price - current_price) * pos["size"] * self._get_ct_val(sym)

        logger.info("平仓: %s %s reason=%s pnl=%.2f", sym, direction, reason, pnl)

        # 风控红线检查
        self._check_risk_limits(pnl)

        # 日志
        self._log_trade(sym, direction, entry_price, pos["size"], "close", pnl)

        # 通知
        self._send_close_card(sym, direction, entry_price, current_price, pnl, reason)

        # 清空状态
        self.state.position = None
        self._save_state()

    # ------------------------------------------------------------------
    # 风控
    # ------------------------------------------------------------------

    def _check_risk_limits(self, pnl: float):
        """风控红线检查，触发自动 halt。"""
        today = datetime.now(TZ_UTC).strftime("%Y-%m-%d")
        if self.state.daily_pnl_date != today:
            self.state.daily_pnl = 0.0
            self.state.daily_pnl_date = today

        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        equity = self.cfg.initial_capital
        kelly_r = self.kelly.calculate()
        if kelly_r.get("equity", 0) > 0:
            equity = kelly_r["equity"]

        if equity > 0 and abs(self.state.daily_pnl) / equity > DAILY_LOSS_PCT:
            trip_halt("auto", f"日亏损>{DAILY_LOSS_PCT*100:.0f}% PnL={self.state.daily_pnl:.1f}")
            self._send_alert(f"🚨 自动停止: 日亏损超过{DAILY_LOSS_PCT*100:.0f}%")

        if self.state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            trip_halt("auto", f"连续{MAX_CONSECUTIVE_LOSSES}笔止损")
            self._send_alert(f"🚨 自动停止: 连续{MAX_CONSECUTIVE_LOSSES}笔止损")

        self._save_state()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _fetch_klines(self, symbol: str, limit: int, interval: str) -> pd.DataFrame | None:
        try:
            resp = req.get(
                f"{OKX_API}/market/candles",
                params={"instId": symbol, "bar": interval, "limit": str(limit)},
                timeout=20,
            )
            data = resp.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            rows = data["data"]
            df = pd.DataFrame(rows, columns=[
                "ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_ccy_quote", "confirm",
            ])
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df.sort_values("ts").reset_index(drop=True)
            return df
        except Exception:
            return None

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
        from src.indicators.ta import compute_atr
        try:
            s = compute_atr(df["high"], df["low"], df["close"], period)
            return float(s.iloc[-1]) if not s.empty else 0.0
        except Exception:
            return 0.0

    def _get_current_price(self, symbol: str) -> float:
        try:
            resp = req.get(f"{OKX_API}/market/ticker", params={"instId": symbol}, timeout=5)
            data = resp.json()
            items = data.get("data", [])
            if items:
                return float(items[0].get("last") or 0)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _get_ct_val(symbol: str) -> float:
        base = symbol.split("-")[0]
        return 0.01 if base in ("BTC", "ETH") else 10.0

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _load_state(self) -> TradingState:
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                return TradingState(
                    position=raw.get("position"),
                    daily_pnl=raw.get("daily_pnl", 0.0),
                    daily_pnl_date=raw.get("daily_pnl_date", ""),
                    consecutive_losses=raw.get("consecutive_losses", 0),
                    last_signal_at=raw.get("last_signal_at", ""),
                )
            except Exception:
                logger.exception("加载状态失败")
        return TradingState()

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "position": self.state.position,
            "daily_pnl": self.state.daily_pnl,
            "daily_pnl_date": self.state.daily_pnl_date,
            "consecutive_losses": self.state.consecutive_losses,
            "last_signal_at": self.state.last_signal_at,
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # 通知 + 日志
    # ------------------------------------------------------------------

    def _log_trade(self, symbol: str, direction: str, price: float, size: int,
                   trade_type: str, pnl: float = 0.0):
        try:
            conn = _get_conn()
            now = int(datetime.now(TZ_UTC).timestamp())
            trade_id = f"exec_{now}_{symbol.replace('-','_')}_{trade_type}"
            conn.execute(
                """INSERT OR IGNORE INTO trade_log
                   (trade_id, symbol, inst_type, side, pos_side, price, quantity,
                    pnl, exec_type, fill_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_id, symbol, "SWAP",
                    "buy" if direction == "long" else "sell",
                    "long" if direction == "long" else "short",
                    price, size, pnl, trade_type, now, now,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("交易日志写入失败")

    def _send_trade_card(self, symbol: str, direction: str, price: float,
                         size: int, order_id: str):
        d_emoji = "📈" if direction == "long" else "📉"
        text = (
            f"**{d_emoji} 开仓: {symbol} {direction}**\n\n"
            f"入场价: {price:.4f}\n"
            f"张数: {size}\n"
            f"杠杆: 5X\n"
            f"订单号: {order_id}\n"
        )
        send_feishu_card_with_buttons(
            "交易通知", text,
            buttons=[
                {"text": "停止交易", "type": "danger", "value": {"action": "halt_trading"}},
                {"text": "查看持仓", "type": "default", "value": {"action": "view_positions"}},
            ],
        )

    def _send_close_card(self, symbol: str, direction: str, entry: float,
                         exit_price: float, pnl: float, reason: str):
        d_emoji = "📈" if direction == "long" else "📉"
        pnl_str = f"+{pnl:.2f}U" if pnl >= 0 else f"{pnl:.2f}U"
        text = (
            f"**{d_emoji} 平仓: {symbol} {direction}**\n\n"
            f"入场价: {entry:.4f}\n"
            f"出场价: {exit_price:.4f}\n"
            f"盈亏: {pnl_str}\n"
            f"原因: {reason}\n"
        )
        send_feishu_card_with_buttons(
            "平仓通知", text,
            buttons=[
                {"text": "停止交易", "type": "danger", "value": {"action": "halt_trading"}},
            ],
        )

    def _send_startup_card(self):
        env = "LIVE" if self.cfg.is_live else "DEMO"
        text = (
            f"**TSMOM 交易引擎已启动**\n\n"
            f"环境: {env}\n"
            f"币种: {', '.join(self.cfg.symbols)}\n"
            f"单次最大仓位: {self.cfg.max_positions}\n"
            f"时间: {datetime.now(TZ_UTC).isoformat()}\n"
        )
        send_feishu_card_with_buttons(
            "交易引擎启动", text,
            buttons=[
                {"text": "停止交易", "type": "danger", "value": {"action": "halt_trading"}},
            ],
        )

    def _send_alert(self, msg: str):
        from src.push.feishu_sender import send_feishu_text
        send_feishu_text(msg)
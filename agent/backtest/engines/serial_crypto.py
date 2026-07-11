"""串行加密合约回测引擎。

一次只持有一个仓位。信号队列按币种优先级排序（BTC > ETH > 山寨）。
支持：ATR 止损、阶梯锁利、EMA 交叉退出、时间退出、资金费率、强平检查。

与 BaseEngine 的区别：
- 不是每 bar rebalance 所有币种，而是逐 bar 检查信号队列
- 当前持仓未平仓时，新信号排队等待
- 平仓后从队列中取最高优先级的信号开仓
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.engines._market_hooks import (
    calc_crypto_funding_fee,
    check_crypto_liquidation,
)
from backtest.models import EquitySnapshot, Position, TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class SerialConfig:
    """串行引擎配置。"""
    initial_capital: float = 150.0
    capital_per_trade: float = 50.0          # 每份资金
    btc_leverage: float = 10.0
    altcoin_leverage: float = 5.0
    maker_rate: float = 0.0002
    taker_rate: float = 0.0005
    slippage_rate: float = 0.0005
    funding_rate: float = 0.0001
    # 止损：N × ATR，上限 capital_per_trade × max_loss_pct
    atr_stop_multiplier: float = 1.5
    max_loss_pct: float = 0.15                # 资金亏损上限 15%
    # 阶梯锁利：每 N × ATR 锁一次
    atr_profit_multiplier: float = 3.0
    # 时间退出（bar 数）
    btc_max_holding_bars: int = 90            # 15 天 × 6 bar/天 (4H)
    alt_max_holding_bars: int = 24            # 24 小时 (1H)
    # 币种优先级：数字越小优先级越高
    symbol_priority: dict = field(default_factory=lambda: {
        "BTC-USDT": 0,
        "ETH-USDT": 1,
    })


@dataclass
class PendingSignal:
    """排队中的信号。"""
    symbol: str
    direction: int          # 1 多, -1 空
    timestamp: pd.Timestamp
    atr: float              # 入场时的 ATR，用于计算止损
    entry_price: float


class SerialCryptoEngine:
    """串行加密合约回测引擎。

    核心约束：同一时间最多持有一个仓位。
    信号按币种优先级排队，平仓后自动取队首信号开仓。
    """

    def __init__(self, config: SerialConfig):
        self.cfg = config
        self.capital: float = config.initial_capital
        self.position: Optional[Position] = None
        self.trades: List[TradeRecord] = []
        self.equity_snapshots: List[EquitySnapshot] = []
        self._funding_applied: set = set()
        self._funding_daily_done: set = set()

        # 持仓附加状态
        self._stop_price: float = 0.0
        self._atr_at_entry: float = 0.0
        self._profit_tiers_taken: int = 0
        self._entry_bar_idx: int = 0
        self._max_holding_bars: int = 0

        # 信号队列
        self._signal_queue: deque[PendingSignal] = deque()

    def run(
        self,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
        atr_map: Dict[str, pd.Series],
        symbols_config: Dict[str, dict],
    ) -> Dict[str, Any]:
        """运行串行回测。

        Args:
            data_map: symbol -> OHLCV DataFrame, index 为时间。
            signal_map: symbol -> signal Series (1/0/-1)。
            atr_map: symbol -> ATR Series。
            symbols_config: symbol -> {"leverage": float, "is_btc": bool}

        Returns:
            回测指标字典。
        """
        # 合并所有时间戳
        all_dates: set = set()
        for df in data_map.values():
            all_dates.update(df.index)
        dates = pd.DatetimeIndex(sorted(all_dates))

        for i, ts in enumerate(dates):
            self._bar_idx = i

            # 1. 检查当前持仓：止损/锁利/时间退出/EMA交叉（开仓后的下一个 bar 才检查）
            if self.position is not None and i > self._entry_bar_idx:
                self._check_position(ts, i, data_map, signal_map)

            # 2. 如果空仓，从队列取信号开仓
            if self.position is None:
                self._try_open_from_queue(ts, i, data_map, symbols_config)

            # 3. 采集新信号（当前 bar 产生的信号入队）
            self._collect_signals(ts, signal_map, atr_map, data_map)

            # 4. 资金费率 + 强平检查（开仓后的下一个 bar 才检查）
            if self.position is not None and i > self._entry_bar_idx:
                self._apply_funding_and_liq(ts, data_map)

            # 5. 记录权益快照
            equity = self._calc_equity(ts, data_map)
            self.equity_snapshots.append(EquitySnapshot(
                timestamp=ts,
                capital=self.capital,
                unrealized=equity - self.capital - self._position_margin(),
                equity=equity,
                positions=1 if self.position else 0,
            ))

        # 强制平仓
        if self.position is not None and len(dates) > 0:
            last_ts = dates[-1]
            price = self._get_price(self.position.symbol, last_ts, data_map)
            self._close_position(price, last_ts, "end_of_backtest")

        # 计算指标
        equity_series = pd.Series(
            [s.equity for s in self.equity_snapshots],
            index=[s.timestamp for s in self.equity_snapshots],
        )
        return self._calc_metrics(equity_series)

    def _collect_signals(
        self,
        ts: pd.Timestamp,
        signal_map: Dict[str, pd.Series],
        atr_map: Dict[str, pd.Series],
        data_map: Dict[str, pd.DataFrame],
    ) -> None:
        """采集当前 bar 的新信号入队。"""
        for symbol, sig_series in signal_map.items():
            if ts not in sig_series.index:
                continue
            sig = sig_series.at[ts]
            if sig is None or pd.isna(sig) or sig == 0:
                continue
            direction = int(sig) if sig > 0 else -1
            if abs(direction) != 1:
                continue

            # 已经在持仓且是同一币种 -> 跳过
            if self.position and self.position.symbol == symbol:
                continue

            # 获取 ATR 和价格
            atr_val = 0.0
            if symbol in atr_map and ts in atr_map[symbol].index:
                atr_val = float(atr_map[symbol].at[ts] or 0)
            price = self._get_price(symbol, ts, data_map)
            if price <= 0 or atr_val <= 0:
                continue

            # 去重：队列里同一币种同方向只保留一个
            already_queued = any(
                s.symbol == symbol and s.direction == direction
                for s in self._signal_queue
            )
            if already_queued:
                continue

            self._signal_queue.append(PendingSignal(
                symbol=symbol,
                direction=direction,
                timestamp=ts,
                atr=atr_val,
                entry_price=price,
            ))

    def _try_open_from_queue(
        self,
        ts: pd.Timestamp,
        bar_idx: int,
        data_map: Dict[str, pd.DataFrame],
        symbols_config: Dict[str, dict],
    ) -> None:
        """从信号队列中取最高优先级信号开仓。"""
        if not self._signal_queue:
            return

        # 按优先级排序
        priority = self.cfg.symbol_priority
        sorted_signals = sorted(
            self._signal_queue,
            key=lambda s: priority.get(s.symbol, 99),
        )

        # 取第一个
        sig = sorted_signals[0]
        self._signal_queue.clear()  # 开仓后清空队列（只取最优信号）

        # 检查信号是否过时（超过 2 bar 的信号丢弃）
        df = data_map.get(sig.symbol)
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]
        open_price = float(bar.get("open", bar.get("close", 0)))
        if open_price <= 0:
            return

        # 滑点
        slipped = open_price * (1 + sig.direction * self.cfg.slippage_rate)

        # 杠杆
        sc = symbols_config.get(sig.symbol, {})
        is_btc = sc.get("is_btc", False)
        leverage = self.cfg.btc_leverage if is_btc else self.cfg.altcoin_leverage
        capital = self.cfg.capital_per_trade

        # 仓位大小
        notional = capital * leverage
        size = notional / slipped

        # 手续费
        comm = size * slipped * self.cfg.taker_rate

        # 检查资金
        if capital + comm > self.capital:
            return

        self.capital -= comm

        self.position = Position(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=slipped,
            entry_time=ts,
            size=size,
            leverage=leverage,
            entry_bar_idx=bar_idx,
            entry_commission=comm,
        )

        # 设置止损
        self._atr_at_entry = sig.atr
        stop_distance = sig.atr * self.cfg.atr_stop_multiplier
        # 资金止损上限：每币亏损不超过 max_loss_amount
        # max_loss_amount / size = 每币最大亏损（价格单位）
        max_loss_amount = capital * self.cfg.max_loss_pct
        max_loss_distance = max_loss_amount / size if size > 0 else stop_distance
        # 取较小的止损距离（ATR 和资金上限哪个先到）
        actual_stop_distance = min(stop_distance, max_loss_distance)
        self._stop_price = slipped - sig.direction * actual_stop_distance
        self._profit_tiers_taken = 0
        self._entry_bar_idx = bar_idx
        self._max_holding_bars = self.cfg.btc_max_holding_bars if is_btc else self.cfg.alt_max_holding_bars

    def _check_position(
        self,
        ts: pd.Timestamp,
        bar_idx: int,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
    ) -> None:
        """检查当前持仓是否触发退出条件。"""
        pos = self.position
        if pos is None:
            return

        df = data_map.get(pos.symbol)
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]
        high = float(bar.get("high", bar.get("close", 0)))
        low = float(bar.get("low", bar.get("close", 0)))
        close = float(bar.get("close", 0))

        # 1. 止损检查
        if pos.direction == 1 and low <= self._stop_price:
            self._close_position(self._stop_price, ts, "stop_loss")
            return
        if pos.direction == -1 and high >= self._stop_price:
            self._close_position(self._stop_price, ts, "stop_loss")
            return

        # 2. 阶梯锁利检查
        profit_target = self._atr_at_entry * self.cfg.atr_profit_multiplier
        next_tier_price = pos.entry_price + pos.direction * profit_target * (self._profit_tiers_taken + 1)
        if pos.direction == 1 and high >= next_tier_price:
            # 锁利：移动止损到上一级锁利位
            self._profit_tiers_taken += 1
            self._stop_price = pos.entry_price + pos.direction * profit_target * (self._profit_tiers_taken - 1)
            if self._stop_price < pos.entry_price:
                self._stop_price = pos.entry_price  # 至少保本
        elif pos.direction == -1 and low <= next_tier_price:
            self._profit_tiers_taken += 1
            self._stop_price = pos.entry_price + pos.direction * profit_target * (self._profit_tiers_taken - 1)
            if self._stop_price > pos.entry_price:
                self._stop_price = pos.entry_price

        # 3. EMA 交叉退出（趋势反转）
        sig_series = signal_map.get(pos.symbol)
        if sig_series is not None and ts in sig_series.index:
            sig = sig_series.at[ts]
            if sig is not None and not pd.isna(sig):
                sig_val = int(sig) if sig != 0 else 0
                # 信号反转 -> 平仓
                if sig_val != 0 and sig_val != pos.direction:
                    self._close_position(close, ts, "ema_cross")
                    return
                # 信号消失（变 0）-> 平仓
                if sig_val == 0:
                    self._close_position(close, ts, "signal_exit")
                    return

        # 4. 时间退出
        holding_bars = bar_idx - self._entry_bar_idx
        if holding_bars >= self._max_holding_bars:
            self._close_position(close, ts, "time_exit")
            return

    def _apply_funding_and_liq(
        self,
        ts: pd.Timestamp,
        data_map: Dict[str, pd.DataFrame],
    ) -> None:
        """资金费率和强平检查。"""
        pos = self.position
        if pos is None:
            return

        df = data_map.get(pos.symbol)
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]
        fee = calc_crypto_funding_fee(
            pos.symbol, bar, ts,
            {pos.symbol: pos},
            self.cfg.funding_rate,
            self._funding_applied,
            self._funding_daily_done,
        )
        self.capital -= fee

        if check_crypto_liquidation(pos.symbol, bar, {pos.symbol: pos}):
            mark_price = float(bar.get("close", pos.entry_price))
            self._close_position(mark_price, ts, "liquidation")

    def _close_position(self, exit_price: float, exit_time: pd.Timestamp, reason: str) -> None:
        """平仓。"""
        pos = self.position
        if pos is None:
            return

        pnl = pos.direction * pos.size * (exit_price - pos.entry_price)
        margin = pos.size * pos.entry_price / pos.leverage
        exit_comm = pos.size * exit_price * self.cfg.maker_rate
        self.capital += margin + pnl - exit_comm

        holding_bars = self._bar_idx - pos.entry_bar_idx if hasattr(self, '_bar_idx') else 0

        self.trades.append(TradeRecord(
            symbol=pos.symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            size=pos.size,
            leverage=pos.leverage,
            pnl=pnl,
            pnl_pct=pnl / margin * 100 if margin > 0 else 0.0,
            exit_reason=reason,
            holding_bars=holding_bars,
            commission=pos.entry_commission + exit_comm,
        ))
        self.position = None
        self._stop_price = 0.0
        self._atr_at_entry = 0.0
        self._profit_tiers_taken = 0

    def _position_margin(self) -> float:
        """当前持仓占用保证金。"""
        if self.position is None:
            return 0.0
        return self.position.size * self.position.entry_price / self.position.leverage

    def _calc_equity(self, ts: pd.Timestamp, data_map: Dict[str, pd.DataFrame]) -> float:
        """计算总权益。"""
        if self.position is None:
            return self.capital
        price = self._get_price(self.position.symbol, ts, data_map)
        pnl = self.position.direction * self.position.size * (price - self.position.entry_price)
        margin = self._position_margin()
        return self.capital + margin + pnl

    @staticmethod
    def _get_price(symbol: str, ts: pd.Timestamp, data_map: Dict[str, pd.DataFrame]) -> float:
        """获取某时刻收盘价。"""
        df = data_map.get(symbol)
        if df is None or ts not in df.index:
            return 0.0
        val = df.at[ts, "close"] if "close" in df.columns else 0.0
        return float(val) if pd.notna(val) else 0.0

    def _calc_metrics(self, equity_series: pd.Series) -> Dict[str, Any]:
        """计算回测指标。"""
        if equity_series.empty:
            return {}

        total_return = (equity_series.iloc[-1] / equity_series.iloc[0] - 1) * 100
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak * 100
        max_drawdown = drawdown.min()

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        win_rate = len(wins) / len(self.trades) * 100 if self.trades else 0
        avg_win = np.mean([t.pnl for t in wins]) if wins else 0
        avg_loss = np.mean([abs(t.pnl) for t in losses]) if losses else 0
        profit_factor = sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)) if losses and sum(t.pnl for t in losses) != 0 else float('inf')
        avg_hold_bars = np.mean([t.holding_bars for t in self.trades]) if self.trades else 0

        # 空仓时间
        flat_bars = sum(1 for s in self.equity_snapshots if s.positions == 0)
        flat_pct = flat_bars / len(self.equity_snapshots) * 100 if self.equity_snapshots else 0

        # 退出原因统计
        exit_reasons: dict[str, int] = {}
        for t in self.trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

        # 按币种统计
        by_symbol: dict[str, dict] = {}
        for t in self.trades:
            if t.symbol not in by_symbol:
                by_symbol[t.symbol] = {"trades": 0, "pnl": 0.0, "wins": 0}
            by_symbol[t.symbol]["trades"] += 1
            by_symbol[t.symbol]["pnl"] += t.pnl
            if t.pnl > 0:
                by_symbol[t.symbol]["wins"] += 1

        return {
            "final_equity": round(equity_series.iloc[-1], 2),
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "win_rate_pct": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else float('inf'),
            "total_trades": len(self.trades),
            "avg_holding_bars": round(avg_hold_bars, 1),
            "flat_time_pct": round(flat_pct, 1),
            "exit_reasons": exit_reasons,
            "by_symbol": by_symbol,
        }

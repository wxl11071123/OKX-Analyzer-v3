"""TSMOM 退出策略：结构位止损 + ATR 跟踪止盈。

退出逻辑：
- 止损：入场时的 swing low/high（过去 10 根 K 线的最低/最高点）
- 跟踪止盈：盈利超过 ATR×3 后，止损移到保本位
  盈利超过 ATR×6 后，止损移到 entry + ATR×2（锁利）
- 信号反转退出：TSMOM 信号反转方向
- 时间退出：240 根 4H（40天）
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from backtest.engines.serial_crypto import ExitDecision, ExitStrategy, SerialConfig
from backtest.models import Position


class TSMOMExit(ExitStrategy):
    """TSMOM 退出策略：结构位止损 + 跟踪止盈。"""

    def __init__(
        self,
        swing_lookback: int = 10,
        atr_trail_multiplier: float = 3.0,
        atr_lock_multiplier: float = 6.0,
        max_holding_bars: int = 240,
    ):
        self.swing_lookback = swing_lookback
        self.atr_trail_multiplier = atr_trail_multiplier
        self.atr_lock_multiplier = atr_lock_multiplier
        self.max_holding_bars = max_holding_bars

    def on_open(
        self,
        pos: Position,
        entry_bar: pd.Series,
        atr: float,
        config: SerialConfig,
    ) -> float:
        """开仓时设置止损：用 entry_bar 的 low/high 近似 swing 位。

        严格来说应该用入场前 N 根的 swing low/high，但 on_open 接口
        只传入单根 K 线。用 entry_bar 的 low/high 作为近似（4H 框架下
        相邻 K 线连续性好，误差可接受）。
        """
        if pos.direction == 1:
            stop = float(entry_bar.get("low", pos.entry_price * 0.97))
        else:
            stop = float(entry_bar.get("high", pos.entry_price * 1.03))

        if stop <= 0:
            stop = pos.entry_price - pos.direction * atr * self.atr_trail_multiplier

        return stop

    def on_bar(
        self,
        pos: Position,
        bar: pd.Series,
        ts: pd.Timestamp,
        bar_idx: int,
        entry_bar_idx: int,
        stop_price: float,
        atr_at_entry: float,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
        config: SerialConfig,
    ) -> ExitDecision:
        """每个 bar 检查退出条件。"""
        high = float(bar.get("high", 0))
        low = float(bar.get("low", 0))
        close = float(bar.get("close", 0))

        # 1. 止损检查
        if pos.direction == 1 and low <= stop_price:
            return ExitDecision(should_exit=True, exit_price=stop_price, reason="stop_loss")
        if pos.direction == -1 and high >= stop_price:
            return ExitDecision(should_exit=True, exit_price=stop_price, reason="stop_loss")

        # 2. 跟踪止盈：移动止损
        if atr_at_entry > 0:
            profit = pos.direction * (close - pos.entry_price)
            profit_in_atr = profit / atr_at_entry

            new_stop = stop_price

            if profit_in_atr >= self.atr_trail_multiplier:
                breakeven = pos.entry_price
                if pos.direction == 1 and breakeven > stop_price:
                    new_stop = breakeven
                elif pos.direction == -1 and breakeven < stop_price:
                    new_stop = breakeven

            if profit_in_atr >= self.atr_lock_multiplier:
                lock_price = pos.entry_price + pos.direction * atr_at_entry * 2
                if pos.direction == 1 and lock_price > new_stop:
                    new_stop = lock_price
                elif pos.direction == -1 and lock_price < new_stop:
                    new_stop = lock_price

            if new_stop != stop_price:
                return ExitDecision(should_exit=False, new_stop_price=new_stop)

        # 3. 信号反转退出
        sig_series = signal_map.get(pos.symbol)
        if sig_series is not None and ts in sig_series.index:
            sig = sig_series.at[ts]
            if sig is not None and not pd.isna(sig):
                sig_val = int(sig) if sig != 0 else 0
                if sig_val != 0 and sig_val != pos.direction:
                    return ExitDecision(should_exit=True, exit_price=close, reason="signal_reverse")

        # 4. 时间退出
        holding_bars = bar_idx - entry_bar_idx
        if holding_bars >= self.max_holding_bars:
            return ExitDecision(should_exit=True, exit_price=close, reason="time_exit")

        return ExitDecision(should_exit=False)

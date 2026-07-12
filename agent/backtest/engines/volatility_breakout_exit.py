"""波动率突破退出策略（策略B 配套）。

退出逻辑：
- 止损：BB反向轨（做多止损=bb_lower，做空止损=bb_upper），开仓时设定
- 跟踪止盈：价格盈利超过 ATR×2 后，止损移动到 entry_price（保本）
  继续盈利超过 ATR×4 后，止损移动到 entry_price + ATR×1（锁利）
- 时间退出：最多持仓 120 根 4H K线（20天）
- 信号反转退出
"""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from backtest.engines.serial_crypto import ExitDecision, ExitStrategy, SerialConfig
from backtest.models import Position
from src.indicators.ta import compute_bollinger


class VolatilityBreakoutExit(ExitStrategy):
    """波动率突破退出策略。"""

    def __init__(
        self,
        bb_window: int = 20,
        bb_std: float = 2.0,
        atr_trail_multiplier: float = 2.0,
        atr_lock_multiplier: float = 4.0,
        max_holding_bars: int = 120,
    ):
        self.bb_window = bb_window
        self.bb_std = bb_std
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
        """开仓时设置止损：BB反向轨。"""
        close = float(entry_bar.get("close", pos.entry_price))

        # 计算 BB（用最近的 close 序列）
        # entry_bar 是单行，无法算 BB，需要用固定止损作为后备
        # 止损 = entry_price - direction × ATR × 2（和BB轨宽度近似）
        if atr > 0:
            stop_distance = atr * self.atr_trail_multiplier
        else:
            stop_distance = pos.entry_price * 0.03  # 3% 后备

        return pos.entry_price - pos.direction * stop_distance

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
        """每个bar检查退出条件。"""
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

            # 盈利超过 ATR×2 -> 止损移到保本
            if profit_in_atr >= self.atr_trail_multiplier:
                breakeven = pos.entry_price
                if pos.direction == 1 and breakeven > stop_price:
                    new_stop = breakeven
                elif pos.direction == -1 and breakeven < stop_price:
                    new_stop = breakeven

            # 盈利超过 ATR×4 -> 止损移到锁利位
            if profit_in_atr >= self.atr_lock_multiplier:
                lock_price = pos.entry_price + pos.direction * atr_at_entry
                if pos.direction == 1 and lock_price > new_stop:
                    new_stop = lock_price
                elif pos.direction == -1 and lock_price < new_stop:
                    new_stop = lock_price

            if new_stop != stop_price:
                return ExitDecision(
                    should_exit=False,
                    new_stop_price=new_stop,
                )

        # 3. 信号反转退出（只在出现反向信号时平仓，信号消失不平仓）
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

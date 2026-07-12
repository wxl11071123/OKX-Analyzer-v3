"""TSMOM 退出策略 v2：EMA 移动止损。

核心改进：用 EMA20 作为移动止损线，替代结构位/ATR 止损。
- 做多：止损 = EMA20（价格在 EMA20 之上时持有，跌破才退出）
- 做空：止损 = EMA20（价格在 EMA20 之下时持有，突破才退出）
- EMA20 随趋势移动，趋势持续则止损跟上，趋势反转则止损触发

这样给了趋势足够的展开空间（EMA20 距价格通常 3-8%），
同时不会被噪音洗出去（单根插针很少持续突破 EMA20）。
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from backtest.engines.serial_crypto import ExitDecision, ExitStrategy, SerialConfig
from backtest.models import Position
from src.indicators.ta import compute_ema


class TSMOMEMAExit(ExitStrategy):
    """TSMOM + EMA 移动止损退出策略。"""

    def __init__(
        self,
        ema_period: int = 20,
        buffer_pct: float = 0.0,
        max_holding_bars: int = 240,
    ):
        self.ema_period = ema_period
        self.buffer_pct = buffer_pct
        self.max_holding_bars = max_holding_bars

    def on_open(
        self,
        pos: Position,
        entry_bar: pd.Series,
        atr: float,
        config: SerialConfig,
    ) -> float:
        """开仓时止损：用入场K线的 EMA20 近似。

        如果没有 EMA20 数据，用 ATR×3 作为后备。
        """
        close = float(entry_bar.get("close", pos.entry_price))
        if atr > 0:
            return pos.entry_price - pos.direction * atr * 3.0
        return pos.entry_price * (0.97 if pos.direction == 1 else 1.03)

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

        # 1. 用 EMA20 作为移动止损
        df = data_map.get(pos.symbol)
        if df is not None and "close" in df.columns:
            ema = compute_ema(df["close"], self.ema_period)
            if ts in ema.index:
                ema_val = float(ema.at[ts])
                if not pd.isna(ema_val) and ema_val > 0:
                    if pos.direction == 1:
                        new_stop = ema_val * (1 - self.buffer_pct)
                        if new_stop > stop_price:
                            stop_price = new_stop
                    else:
                        new_stop = ema_val * (1 + self.buffer_pct)
                        if new_stop < stop_price or stop_price == 0:
                            stop_price = new_stop

        # 止损检查
        if pos.direction == 1 and low <= stop_price:
            return ExitDecision(should_exit=True, exit_price=stop_price, reason="stop_loss")
        if pos.direction == -1 and high >= stop_price:
            return ExitDecision(should_exit=True, exit_price=stop_price, reason="stop_loss")

        # 2. 信号反转退出
        sig_series = signal_map.get(pos.symbol)
        if sig_series is not None and ts in sig_series.index:
            sig = sig_series.at[ts]
            if sig is not None and not pd.isna(sig):
                sig_val = int(sig) if sig != 0 else 0
                if sig_val != 0 and sig_val != pos.direction:
                    return ExitDecision(should_exit=True, exit_price=close, reason="signal_reverse")

        # 3. 时间退出
        holding_bars = bar_idx - entry_bar_idx
        if holding_bars >= self.max_holding_bars:
            return ExitDecision(should_exit=True, exit_price=close, reason="time_exit")

        # 更新止损价
        return ExitDecision(should_exit=False, new_stop_price=stop_price)

"""山寨币均值回归信号引擎。

1H 周期，快进快出：
- RSI < 25 + 价格触及/跌破布林带下轨 -> 做多（超卖反弹）
- RSI > 75 + 价格触及/突破布林带上轨 -> 做空（超买回落）
- ATR 止损紧（1×ATR），目标 2×ATR
- 24h 时间退出

信号值：1 做多, -1 做空, 0 观望
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from src.indicators.ta import compute_bollinger, compute_rsi


class MeanReversionSignalEngine:
    """山寨币均值回归信号引擎。

    核心逻辑：山寨币暴涨暴跌，趋势不持续。做反转而非跟踪。
    RSI 极端 + 布林带边缘 = 均值回归入场信号。
    """

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 25.0,
        rsi_overbought: float = 75.0,
        bb_window: int = 20,
        bb_std: float = 2.0,
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_window = bb_window
        self.bb_std = bb_std

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        """生成信号。

        Args:
            data_map: symbol -> OHLCV DataFrame (1H)。

        Returns:
            symbol -> signal Series (1/0/-1)。
        """
        signals: Dict[str, pd.Series] = {}

        for symbol, df in data_map.items():
            close = df["close"]
            rsi = compute_rsi(close, self.rsi_period)
            bb = compute_bollinger(close, self.bb_window, self.bb_std)

            bb_upper = bb["bb_upper"]
            bb_lower = bb["bb_lower"]

            sig = pd.Series(0, index=df.index, dtype=int)

            for i in range(len(df)):
                if pd.isna(rsi.iloc[i]) or pd.isna(bb_lower.iloc[i]) or pd.isna(bb_upper.iloc[i]):
                    continue

                rsi_val = rsi.iloc[i]
                price = close.iloc[i]

                # 做多：RSI 超卖 + 价格触及/跌破布林带下轨
                if rsi_val < self.rsi_oversold and price <= bb_lower.iloc[i]:
                    sig.iloc[i] = 1

                # 做空：RSI 超买 + 价格触及/突破布林带上轨
                elif rsi_val > self.rsi_overbought and price >= bb_upper.iloc[i]:
                    sig.iloc[i] = -1

            signals[symbol] = sig

        return signals

    def compute_atr_map(self, data_map: Dict[str, pd.DataFrame], period: int = 14) -> Dict[str, pd.Series]:
        """计算各币种的 ATR 序列。"""
        from src.indicators.ta import compute_atr
        atr_map: Dict[str, pd.Series] = {}
        for symbol, df in data_map.items():
            atr_map[symbol] = compute_atr(df["high"], df["low"], df["close"], period=period)
        return atr_map

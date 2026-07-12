"""TSMOM（时间序列动量）信号引擎。

基于 Moskowitz, Ooi, Pedersen (2013) 的学术研究：
- 过去 N 期收益为正 -> 做多
- 过去 N 期收益为负 -> 做空
- 只有 1 个参数（回看周期），最小过拟合风险

Hurst 验证显示加密 4H 市场 H=0.69（强趋势态），TSMOM 在趋势态有理论支撑。

信号值：1 做多, -1 做空, 0 观望
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from src.indicators.ta import compute_atr


class TSMOMSignalEngine:
    """时间序列动量信号引擎。

    核心逻辑：过去 N 期的总收益方向预测未来方向。
    与 EMA 交叉的区别：
    - EMA 交叉衡量均线关系（滞后），TSMOM 衡量实际收益（直接）
    - TSMOM 只有 1 个参数，EMA 交叉需要 2 个周期 + 信号确认
    """

    def __init__(
        self,
        lookback: int = 120,
        hurst_filter: bool = True,
        hurst_window: int = 200,
        hurst_threshold: float = 0.55,
    ):
        self.lookback = lookback
        self.hurst_filter = hurst_filter
        self.hurst_window = hurst_window
        self.hurst_threshold = hurst_threshold

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        """生成信号。

        Args:
            data_map: symbol -> OHLCV DataFrame (4H)。

        Returns:
            symbol -> signal Series (1/0/-1)。
        """
        signals: Dict[str, pd.Series] = {}

        for symbol, df in data_map.items():
            close = df["close"]

            # TSMOM: 过去 N 期收益率
            past_return = close.pct_change(self.lookback)

            # Hurst 过滤
            if self.hurst_filter:
                from src.indicators.ta import compute_hurst
                hurst = compute_hurst(close, self.hurst_window)
                trend_regime = hurst > self.hurst_threshold
            else:
                trend_regime = pd.Series(True, index=df.index)

            # 信号：过去收益为正做多，为负做空
            sig = pd.Series(0, index=df.index, dtype=int)
            sig[(past_return > 0) & trend_regime] = 1
            sig[(past_return < 0) & trend_regime] = -1

            signals[symbol] = sig

        return signals

    def compute_atr_map(self, data_map: Dict[str, pd.DataFrame], period: int = 14) -> Dict[str, pd.Series]:
        """计算各币种的 ATR 序列。"""
        atr_map: Dict[str, pd.Series] = {}
        for symbol, df in data_map.items():
            atr_map[symbol] = compute_atr(df["high"], df["low"], df["close"], period=period)
        return atr_map

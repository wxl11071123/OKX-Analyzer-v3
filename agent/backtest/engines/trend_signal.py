"""BTC/ETH 趋势跟踪信号引擎（纪律 v1.2）。

4H 周期：
- EMA20 > EMA50 且 ADX(14) > 25 -> 做多信号
- EMA20 < EMA50 且 ADX(14) > 25 -> 做空信号
- ADX <= 25 -> 无信号
- 价格回调到 EMA20 ±2% 范围才发出入场信号（不追突破）

信号值：1 做多, -1 做空, 0 观望
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from src.indicators.ta import compute_adx, compute_ema


class TrendSignalEngine:
    """趋势跟踪信号引擎。

    纪律 2.1：4H EMA20/EMA50 + ADX(14)>25 定义趋势方向
    纪律 2.2：价格回调到 EMA20 ±2% 才入场
    纪律 2.3：禁止追突破（价格偏离 EMA20 >5% 不发信号）
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        pullback_pct: float = 0.02,      # 距 EMA20 ±2% 视为回调到位
        max_deviation_pct: float = 0.05,  # 偏离 EMA20 >5% 视为追突破
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.pullback_pct = pullback_pct
        self.max_deviation_pct = max_deviation_pct

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        """生成信号。

        Args:
            data_map: symbol -> OHLCV DataFrame。

        Returns:
            symbol -> signal Series (1/0/-1)。
        """
        signals: Dict[str, pd.Series] = {}

        for symbol, df in data_map.items():
            close = df["close"]
            high = df["high"]
            low = df["low"]

            ema_f = compute_ema(close, self.ema_fast)
            ema_s = compute_ema(close, self.ema_slow)
            adx_df = compute_adx(high, low, close, self.adx_period)
            adx = adx_df["adx"]

            sig = pd.Series(0, index=df.index, dtype=int)

            for i in range(len(df)):
                if pd.isna(adx.iloc[i]) or pd.isna(ema_f.iloc[i]) or pd.isna(ema_s.iloc[i]):
                    continue

                adx_val = adx.iloc[i]
                ema_f_val = ema_f.iloc[i]
                ema_s_val = ema_s.iloc[i]
                price = close.iloc[i]

                # ADX <= 25：无趋势，不交易
                if adx_val <= self.adx_threshold:
                    continue

                # 趋势方向
                if ema_f_val > ema_s_val:
                    trend_dir = 1  # 做多
                elif ema_f_val < ema_s_val:
                    trend_dir = -1  # 做空
                else:
                    continue

                # 价格距 EMA20 的偏离
                deviation = abs(price - ema_f_val) / ema_f_val if ema_f_val > 0 else 0

                # 禁止追突破：偏离 >5% 不发信号
                if deviation > self.max_deviation_pct:
                    continue

                # 回调到位：价格在 EMA20 ±2% 内
                if deviation <= self.pullback_pct:
                    sig.iloc[i] = trend_dir

            signals[symbol] = sig

        return signals

    def compute_atr_map(self, data_map: Dict[str, pd.DataFrame], period: int = 14) -> Dict[str, pd.Series]:
        """计算各币种的 ATR 序列。"""
        from src.indicators.ta import compute_atr
        atr_map: Dict[str, pd.Series] = {}
        for symbol, df in data_map.items():
            atr_map[symbol] = compute_atr(df["high"], df["low"], df["close"], period=period)
        return atr_map

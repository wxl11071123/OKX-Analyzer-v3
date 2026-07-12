"""波动率突破信号引擎（策略B）。

4H 周期，基于 GARCH 波动率聚集效应：
- BBW（布林带宽度）压缩到近 N 根 K 线的最低分位时，波动率处于压缩态
- 价格突破 BB 上轨 -> 做多；跌破 BB 下轨 -> 做空
- 成交量必须 > 均量 × 1.5（确认突破有效）

信号值：1 做多, -1 做空, 0 观望
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from src.indicators.ta import compute_atr, compute_bollinger


class VolatilityBreakoutSignalEngine:
    """波动率突破信号引擎。

    核心逻辑：低波动压缩后的突破有方向持续性（GARCH效应）。
    """

    def __init__(
        self,
        bb_window: int = 20,
        bb_std: float = 2.0,
        bbw_lookback: int = 120,
        bbw_percentile: float = 0.20,
        volume_multiplier: float = 1.5,
        volume_ma: int = 20,
    ):
        self.bb_window = bb_window
        self.bb_std = bb_std
        self.bbw_lookback = bbw_lookback
        self.bbw_percentile = bbw_percentile
        self.volume_multiplier = volume_multiplier
        self.volume_ma = volume_ma

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
            high = df["high"]
            low = df["low"]
            volume = df["vol"]

            bb = compute_bollinger(close, self.bb_window, self.bb_std)
            bb_upper = bb["bb_upper"]
            bb_lower = bb["bb_lower"]
            bb_mid = bb["bb_mid"]

            # BBW = (upper - lower) / mid
            bbw = (bb_upper - bb_lower) / bb_mid.replace(0, pd.NA)

            # BBW 压缩分位数
            bbw_rank = bbw.rolling(self.bbw_lookback).rank(pct=True)

            # 成交量均线
            vol_ma = volume.rolling(self.volume_ma).mean()

            sig = pd.Series(0, index=df.index, dtype=int)

            for i in range(len(df)):
                if pd.isna(bbw_rank.iloc[i]) or pd.isna(vol_ma.iloc[i]):
                    continue
                if pd.isna(bb_upper.iloc[i]) or pd.isna(bb_lower.iloc[i]):
                    continue

                # BBW 必须处于压缩态（低于分位阈值）
                if bbw_rank.iloc[i] > self.bbw_percentile:
                    continue

                # 成交量必须放大
                if vol_ma.iloc[i] <= 0 or volume.iloc[i] < vol_ma.iloc[i] * self.volume_multiplier:
                    continue

                price = close.iloc[i]

                # 突破 BB 上轨 -> 做多
                if price >= bb_upper.iloc[i]:
                    sig.iloc[i] = 1

                # 跌破 BB 下轨 -> 做空
                elif price <= bb_lower.iloc[i]:
                    sig.iloc[i] = -1

            signals[symbol] = sig

        return signals

    def compute_atr_map(self, data_map: Dict[str, pd.DataFrame], period: int = 14) -> Dict[str, pd.Series]:
        """计算各币种的 ATR 序列。"""
        atr_map: Dict[str, pd.Series] = {}
        for symbol, df in data_map.items():
            atr_map[symbol] = compute_atr(df["high"], df["low"], df["close"], period=period)
        return atr_map

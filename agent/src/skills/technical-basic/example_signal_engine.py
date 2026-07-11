"""技术面基础指标合集信号引擎。

将趋势（EMA/ADX）、均值回归（BB/RSI）、量价（OBV/量比）三维度合并，
通过投票机制生成综合交易信号。纯 pandas 实现，适用于任何 OHLCV 数据。
"""

from typing import Dict

import pandas as pd

from src.indicators.ta import compute_adx, compute_bollinger, compute_obv, compute_rsi


class SignalEngine:
    """技术面基础指标合集信号引擎。

    三维度投票：趋势（EMA交叉+ADX强度）、均值回归（BB+RSI）、量价（OBV+量比），
    综合生成做多/做空/观望信号。

    Attributes:
        ema_fast: 快线 EMA 周期。
        ema_slow: 慢线 EMA 周期。
        adx_period: ADX 计算周期。
        adx_threshold: ADX 趋势强度阈值。
        bb_window: 布林带窗口。
        bb_std: 布林带标准差倍数。
        rsi_period: RSI 周期。
        rsi_oversold: RSI 超卖阈值。
        rsi_overbought: RSI 超买阈值。
        vol_ma_period: 成交量均线周期。
        obv_ma_period: OBV 均线周期。

    Example:
        >>> engine = SignalEngine()
        >>> signals = engine.generate({"BTC-USDT": df})
        >>> signals["BTC-USDT"].value_counts()
    """

    def __init__(
        self,
        ema_fast: int = 12,
        ema_slow: int = 26,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        bb_window: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: float = 30,
        rsi_overbought: float = 70,
        vol_ma_period: int = 20,
        obv_ma_period: int = 20,
    ):
        """初始化技术面信号引擎。

        Args:
            ema_fast: 快线 EMA 周期。
            ema_slow: 慢线 EMA 周期。
            adx_period: ADX 计算周期。
            adx_threshold: ADX 趋势强度阈值。
            bb_window: 布林带窗口。
            bb_std: 布林带标准差倍数。
            rsi_period: RSI 周期。
            rsi_oversold: RSI 超卖阈值。
            rsi_overbought: RSI 超买阈值。
            vol_ma_period: 成交量均线周期。
            obv_ma_period: OBV 均线周期。
        """
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.bb_window = bb_window
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.vol_ma_period = vol_ma_period
        self.obv_ma_period = obv_ma_period

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        """根据三维度指标投票生成交易信号。

        Args:
            data_map: 标的代码到 OHLCV DataFrame 的映射。
                DataFrame 需包含 open/high/low/close/volume 列，index 为 datetime。

        Returns:
            标的代码到信号 Series 的映射（1=做多, -1=做空, 0=观望）。
        """
        result = {}
        for code, df in data_map.items():
            result[code] = self._generate_one(df)
        return result

    def _generate_one(self, df: pd.DataFrame) -> pd.Series:
        """对单个标的生成信号。

        Args:
            df: OHLCV DataFrame，index 为 datetime。

        Returns:
            信号 Series（1=做多, -1=做空, 0=观望）。
        """
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # --- 趋势维度 ---
        ema_f = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_s = close.ewm(span=self.ema_slow, adjust=False).mean()
        adx_df = compute_adx(high, low, close, self.adx_period)
        adx = adx_df["adx"]

        trend_bull = (ema_f > ema_s) & (adx > self.adx_threshold)
        trend_bear = (ema_f < ema_s) & (adx > self.adx_threshold)

        # --- 均值回归维度 ---
        bb = compute_bollinger(close, self.bb_window, self.bb_std)
        rsi = compute_rsi(close, self.rsi_period)

        mr_oversold = (close < bb["bb_lower"]) & (rsi < self.rsi_oversold)
        mr_overbought = (close > bb["bb_upper"]) & (rsi > self.rsi_overbought)

        # --- 量价维度 ---
        obv = compute_obv(close, volume)
        obv_ma = obv.rolling(self.obv_ma_period).mean()

        vol_bull = obv > obv_ma
        vol_bear = obv < obv_ma

        # --- 三维度投票 ---
        buy = (trend_bull | mr_oversold) & vol_bull & ~mr_overbought
        sell = (trend_bear | mr_overbought) & vol_bear & ~mr_oversold

        signal = buy.astype(int) - sell.astype(int)
        signal = signal.fillna(0).astype(int)
        return signal


def _fetch_okx(inst_id: str, bar: str = "1D", limit: int = 300) -> pd.DataFrame:
    """从 OKX API 获取 K 线数据。

    Args:
        inst_id: 交易对标识，如 "BTC-USDT"。
        bar: K 线周期，默认 "1D"。
        limit: 获取根数，默认 300。

    Returns:
        OHLCV DataFrame，index 为 datetime。
    """
    import os
    import requests
    base = os.getenv("OKX_RELAY", "https://www.okx.com")
    resp = requests.get(
        f"{base.rstrip('/')}/api/v5/market/candles",
        params={"instId": inst_id, "bar": bar, "limit": str(limit)},
    )
    candles = resp.json()["data"]
    columns = [
        "ts", "open", "high", "low", "close",
        "vol", "volCcy", "volCcyQuote", "confirm",
    ]
    df = pd.DataFrame(reversed(candles), columns=columns)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    df = df.set_index("ts")
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["volume"] = df["vol"].astype(float)
    return df


if __name__ == "__main__":
    symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    data_map = {}
    for sym in symbols:
        print(f"Fetching {sym} ...")
        data_map[sym] = _fetch_okx(sym, bar="1D", limit=300)

    engine = SignalEngine()
    signals = engine.generate(data_map)

    for sym in symbols:
        sig = signals[sym]
        n_bars = len(data_map[sym])
        buys = sig[sig == 1]
        sells = sig[sig == -1]
        print(f"\n{sym} ({n_bars} bars)")
        print(f"  Buy signals:  {len(buys)}")
        print(f"  Sell signals: {len(sells)}")
        print(f"  Neutral:      {n_bars - len(buys) - len(sells)}")
        if len(buys) > 0:
            print(f"  Last buy:     {buys.index[-1]:%Y-%m-%d}")
        if len(sells) > 0:
            print(f"  Last sell:    {sells.index[-1]:%Y-%m-%d}")

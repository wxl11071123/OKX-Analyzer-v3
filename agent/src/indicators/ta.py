"""技术指标计算模块。

提供标准化的技术指标计算函数，供 indicator_tool 和 skill 共同使用。
所有函数均为纯 pandas/numpy 实现，不依赖外部数据源。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_ema(close: pd.Series, period: int = 12) -> pd.Series:
    """计算 EMA（指数移动平均线）。

    使用 ``adjust=False`` 模式，递推公式为：
    ``EMA_t = (close - EMA_{t-1}) * (2/(period+1)) + EMA_{t-1}``，
    首个值取 close 的第一个值。与 TradingView 默认行为一致。

    Args:
        close: 收盘价序列。
        period: EMA 周期。

    Returns:
        EMA 值序列。
    """
    return close.ewm(span=period, adjust=False).mean()


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """计算 RSI（Wilder EWM 平滑）。

    Args:
        close: 收盘价序列。
        period: RSI 周期。

    Returns:
        RSI 值序列，范围 0-100。
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def compute_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """计算 MACD（移动平均收敛散度指标）。

    DIF = EMA(fast) - EMA(slow)
    DEA = EMA(DIF, signal)
    HIST = DIF - DEA

    Args:
        close: 收盘价序列。
        fast: 快线 EMA 周期。
        slow: 慢线 EMA 周期。
        signal: 信号线 EMA 周期。

    Returns:
        包含 macd（DIF）、macd_signal（DEA）、macd_hist（HIST）列的 DataFrame。
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return pd.DataFrame({"macd": dif, "macd_signal": dea, "macd_hist": hist})


def compute_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    """计算 ADX 及 +DI/-DI（Wilder EWM 平滑全链路）。

    链路：+DM/-DM -> TR -> Wilder 平滑 -> +DI/-DI -> DX -> ADX。

    Args:
        high: 最高价序列。
        low: 最低价序列。
        close: 收盘价序列。
        period: ADX 周期。

    Returns:
        包含 plus_di、minus_di、adx 列的 DataFrame。
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    # +DM / -DM
    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    # True Range
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder 平滑
    alpha = 1 / period
    smoothed_tr = tr.ewm(alpha=alpha, min_periods=period).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=alpha, min_periods=period).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=alpha, min_periods=period).mean()

    # +DI / -DI
    plus_di = 100 * smoothed_plus_dm / smoothed_tr
    minus_di = 100 * smoothed_minus_dm / smoothed_tr

    # DX -> ADX
    di_sum = plus_di + minus_di
    di_sum = di_sum.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=alpha, min_periods=period).mean()

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx})


def compute_bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """计算布林带。

    Args:
        close: 收盘价序列。
        window: 移动平均窗口。
        num_std: 标准差倍数。

    Returns:
        包含 bb_mid、bb_upper、bb_lower 列的 DataFrame。
    """
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    return pd.DataFrame({
        "bb_mid": mid,
        "bb_upper": mid + num_std * std,
        "bb_lower": mid - num_std * std,
    })


def compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """计算 OBV（能量潮指标）。

    Args:
        close: 收盘价序列。
        volume: 成交量序列。

    Returns:
        OBV 序列。
    """
    sign = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (volume * sign).cumsum()


def compute_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """计算 ATR（平均真实波幅）。

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = Wilder EWM 平滑的 TR。

    Args:
        high: 最高价序列。
        low: 最低价序列。
        close: 收盘价序列。
        period: ATR 周期。

    Returns:
        ATR 值序列。
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def compute_hurst(close: pd.Series, window: int = 200) -> pd.Series:
    """计算滚动 Hurst 指数（R/S 分析法）。

    H > 0.5：持续性序列（趋势态）
    H = 0.5：随机游走
    H < 0.5：反持续性序列（均值回归态）

    Args:
        close: 收盘价序列。
        window: 滚动窗口大小，建议 >= 200 以保证估计稳定性。

    Returns:
        Hurst 指数序列。
    """
    hurst_values = pd.Series(np.nan, index=close.index)

    close_arr = close.values

    for i in range(window - 1, len(close_arr)):
        segment = close_arr[i - window + 1: i + 1]
        segment = segment[~np.isnan(segment)]
        if len(segment) < window:
            continue

        returns = np.diff(np.log(segment))
        if len(returns) < 20:
            continue

        n = len(returns)
        rs_values = []
        ns = []

        for k in [2, 4, 5, 8, 10, 16, 20, 25, 32, 40, 50, 80, 100]:
            if k > n // 2:
                break
            num_groups = n // k
            if num_groups < 1:
                continue
            rs_list = []
            for g in range(num_groups):
                group = returns[g * k: (g + 1) * k]
                if len(group) < 2:
                    continue
                mean_g = np.mean(group)
                cumdev = np.cumsum(group - mean_g)
                r = np.max(cumdev) - np.min(cumdev)
                s = np.std(group, ddof=1)
                if s > 0:
                    rs_list.append(r / s)
            if rs_list:
                rs_values.append(np.mean(rs_list))
                ns.append(k)

        if len(ns) < 4:
            continue

        log_ns = np.log(ns)
        log_rs = np.log(rs_values)

        slope, _ = np.polyfit(log_ns, log_rs, 1)
        hurst_values.iloc[i] = slope

    return hurst_values

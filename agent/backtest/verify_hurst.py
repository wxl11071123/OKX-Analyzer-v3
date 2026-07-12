"""Hurst 指数验证：检查各币种的真实 regime 分布。

回答关键问题：
1. 加密 4H 市场是趋势态多还是均值回归态多？
2. Hurst 是否有足够的波动（regime 切换），还是一直停在 0.5？
3. 哪些币种适合趋势策略，哪些适合均值回归？
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
import requests

from src.indicators.ta import compute_hurst
from backtest.run_volatility_backtest import CACHE_DIR

BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"
CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]


def fetch_klines(inst_id, bar="4H", days=360):
    """拉更长的数据(360天)以验证 Hurst 稳定性。"""
    cache_key = f"{inst_id}_{bar}_{days}d.csv"
    cache_path = os.path.join(CACHE_DIR, cache_key)
    if os.path.exists(cache_path):
        df = pd.read_csv(cache_path, index_col="ts", parse_dates=True)
        if not df.empty:
            return df[["open", "high", "low", "close", "vol"]]

    os.makedirs(CACHE_DIR, exist_ok=True)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    all_data = []
    after = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)
    import time

    while after > start_ms:
        resp = requests.get(f"{BASE_URL}/market/history-candles", params={
            "instId": inst_id, "bar": bar, "limit": "300", "after": str(after),
        }, timeout=15)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            break
        batch = [r for r in data["data"] if r[8] == "1"]
        all_data.extend(batch)
        after = int(batch[-1][0])
        if len(batch) < 300:
            break
        time.sleep(0.1)

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data, columns=CANDLE_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("ts").reset_index(drop=True)
    df.set_index("ts", inplace=True)
    df = df[df.index >= pd.Timestamp(start_ms, unit='ms')]
    df = df[["open", "high", "low", "close", "vol"]]
    df.to_csv(cache_path)
    return df


def main():
    print("=" * 80)
    print("Hurst 指数验证 (360天 4H)")
    print("=" * 80)

    symbols = [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "PEPE-USDT",
        "WIF-USDT", "BONK-USDT", "ARB-USDT", "OP-USDT", "TIA-USDT",
        "AVAX-USDT", "LINK-USDT", "ADA-USDT", "XRP-USDT", "LTC-USDT",
    ]

    print(f"\n拉取 {len(symbols)} 个币种 4H 数据 (360天)...")
    data_4h = {}
    for symbol in symbols:
        inst_id = f"{symbol}-SWAP"
        print(f"  {inst_id}...", end=" ", flush=True)
        df = fetch_klines(inst_id, bar="4H", days=360)
        if df.empty:
            print("FAIL")
            continue
        data_4h[symbol] = df
        print(f"{len(df)} bars")

    print(f"\n计算 Hurst 指数 (window=200)...")
    print(f"\n{'Symbol':<16} {'Bars':>6} {'Mean':>7} {'Median':>7} {'Std':>6} "
          f"{'Trend%':>7} {'MeanRev%':>8} {'Random%':>8} {'Min':>6} {'Max':>6}")
    print("-" * 90)

    all_hurst = {}

    for symbol, df in data_4h.items():
        close = df["close"]
        h = compute_hurst(close, window=200)
        h_clean = h.dropna()

        if len(h_clean) < 10:
            print(f"{symbol:<16} {len(df):>6} insufficient Hurst data ({len(h_clean)} points)")
            continue

        all_hurst[symbol] = h_clean

        mean_h = h_clean.mean()
        median_h = h_clean.median()
        std_h = h_clean.std()
        trend_pct = (h_clean > 0.55).sum() / len(h_clean) * 100
        meanrev_pct = (h_clean < 0.45).sum() / len(h_clean) * 100
        random_pct = ((h_clean >= 0.45) & (h_clean <= 0.55)).sum() / len(h_clean) * 100
        min_h = h_clean.min()
        max_h = h_clean.max()

        print(f"{symbol:<16} {len(df):>6} {mean_h:>7.4f} {median_h:>7.4f} {std_h:>6.4f} "
              f"{trend_pct:>7.1f} {meanrev_pct:>8.1f} {random_pct:>8.1f} {min_h:>6.4f} {max_h:>6.4f}")

    # 汇总
    print(f"\n{'='*80}")
    print("汇总")
    print(f"{'='*80}")

    all_h = pd.concat(all_hurst.values())
    print(f"  所有币种 Hurst 统计:")
    print(f"    全局均值:   {all_h.mean():.4f}")
    print(f"    全局中位数: {all_h.median():.4f}")
    print(f"    全局标准差: {all_h.std():.4f}")
    print(f"    趋势态 (H>0.55): {(all_h > 0.55).sum() / len(all_h) * 100:.1f}%")
    print(f"    均值回归 (H<0.45): {(all_h < 0.45).sum() / len(all_h) * 100:.1f}%")
    print(f"    随机游走 (0.45-0.55): {((all_h >= 0.45) & (all_h <= 0.55)).sum() / len(all_h) * 100:.1f}%")

    # 关键判断
    trend_pct_overall = (all_h > 0.55).sum() / len(all_h) * 100
    meanrev_pct_overall = (all_h < 0.45).sum() / len(all_h) * 100
    random_pct_overall = ((all_h >= 0.45) & (all_h <= 0.55)).sum() / len(all_h) * 100

    print(f"\n  关键判断:")
    if random_pct_overall > 60:
        print(f"    [WARN] {random_pct_overall:.1f}% 时间处于随机游走态，Hurst filter 可能过滤掉太多信号")
    else:
        print(f"    [OK] 仅 {random_pct_overall:.1f}% 时间处于随机游走态，有足够的 regime 切换")

    if trend_pct_overall > 20:
        print(f"    [OK] {trend_pct_overall:.1f}% 趋势态，趋势模块有足够触发空间")
    else:
        print(f"    [WARN] 仅 {trend_pct_overall:.1f}% 趋势态，趋势模块可能信号不足")

    if meanrev_pct_overall > 20:
        print(f"    [OK] {meanrev_pct_overall:.1f}% 均值回归态，均值回归模块有足够触发空间")
    else:
        print(f"    [WARN] 仅 {meanrev_pct_overall:.1f}% 均值回归态，均值回归模块可能信号不足")

    # 打印 BTC 的时间分布
    if "BTC-USDT" in all_hurst:
        btc_h = all_hurst["BTC-USDT"]
        print(f"\n  BTC Hurst 时间分布 (按月):")
        for month in range(1, 13):
            month_data = btc_h[btc_h.index.month == month]
            if len(month_data) < 5:
                continue
            trend = (month_data > 0.55).sum() / len(month_data) * 100
            meanrev = (month_data < 0.45).sum() / len(month_data) * 100
            random = ((month_data >= 0.45) & (month_data <= 0.55)).sum() / len(month_data) * 100
            print(f"    2026-{month:02d}: H_mean={month_data.mean():.3f} "
                  f"trend={trend:5.1f}% meanrev={meanrev:5.1f}% random={random:5.1f}%")


if __name__ == "__main__":
    main()

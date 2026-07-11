"""回测脚本：BTC+ETH 趋势策略（10X）+ 山寨均值回归（5X）。

在服务器上执行，通过 OKX relay 拉取 K线数据。
"""

from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timedelta

# 确保能 import agent 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import requests

from backtest.engines.serial_crypto import SerialCryptoEngine, SerialConfig
from backtest.engines.trend_signal import TrendSignalEngine
from backtest.engines.mean_reversion_signal import MeanReversionSignalEngine
from src.indicators.ta import compute_atr


BASE_URL = os.getenv("OKX_RELAY", "http://127.0.0.1:8080") + "/api/v5"

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]


def fetch_4h_klines(inst_id: str, days: int = 180) -> pd.DataFrame:
    """拉取 4H K线数据。OKX after 参数返回早于该时间戳的数据。"""
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    all_data = []
    after = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)

    while after > start_ms:
        resp = requests.get(f"{BASE_URL}/market/candles", params={
            "instId": inst_id,
            "bar": "4H",
            "limit": "300",
            "after": str(after),
        }, timeout=15)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            break

        batch = data["data"]
        all_data.extend(batch)
        after = int(batch[-1][0])
        if len(batch) < 300:
            break

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data, columns=CANDLE_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("ts").reset_index(drop=True)
    df.set_index("ts", inplace=True)
    # 只保留 start 之后的数据
    df = df[df.index >= pd.Timestamp(start_ms, unit='ms')]
    return df[["open", "high", "low", "close", "vol"]]


def fetch_1h_klines(inst_id: str, days: int = 60) -> pd.DataFrame:
    """拉取 1H K线数据。OKX after 参数返回早于该时间戳的数据。"""
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    all_data = []
    after = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)

    while after > start_ms:
        resp = requests.get(f"{BASE_URL}/market/candles", params={
            "instId": inst_id,
            "bar": "1H",
            "limit": "300",
            "after": str(after),
        }, timeout=15)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            break

        batch = data["data"]
        all_data.extend(batch)
        after = int(batch[-1][0])
        if len(batch) < 300:
            break

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data, columns=CANDLE_COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("ts").reset_index(drop=True)
    df.set_index("ts", inplace=True)
    df = df[df.index >= pd.Timestamp(start_ms, unit='ms')]
    return df[["open", "high", "low", "close", "vol"]]


def run_trend_backtest():
    """运行 BTC+ETH 趋势回测（4H, 10X）。"""
    print("=" * 60)
    print("BTC + ETH 趋势策略回测 (4H, 10X)")
    print("=" * 60)

    # 拉数据
    print("\n拉取 4H K线...")
    data_map = {}
    for symbol in ["BTC-USDT", "ETH-USDT"]:
        inst_id = f"{symbol}-SWAP"
        print(f"  {inst_id}...", end=" ")
        df = fetch_4h_klines(inst_id, days=180)
        if df.empty:
            print("FAIL")
            return
        data_map[symbol] = df
        print(f"{len(df)} bars, {df.index[0]} ~ {df.index[-1]}")

    # 生成信号
    print("\n生成趋势信号...")
    engine_signal = TrendSignalEngine()
    signal_map = engine_signal.generate(data_map)
    atr_map = engine_signal.compute_atr_map(data_map)

    for symbol, sig in signal_map.items():
        longs = (sig == 1).sum()
        shorts = (sig == -1).sum()
        print(f"  {symbol}: {longs} longs, {shorts} shorts, {(sig==0).sum()} flat")

    # 配置
    config = SerialConfig(
        initial_capital=150.0,
        capital_per_trade=50.0,
        btc_leverage=10.0,
        altcoin_leverage=10.0,  # ETH 也用 10X
        atr_stop_multiplier=1.5,
        max_loss_pct=0.15,
        atr_profit_multiplier=3.0,
        btc_max_holding_bars=90,   # 15天 × 6 bar/天
        alt_max_holding_bars=90,   # ETH 同 BTC
        symbol_priority={"BTC-USDT": 0, "ETH-USDT": 1},
    )

    symbols_config = {
        "BTC-USDT": {"leverage": 10, "is_btc": True},
        "ETH-USDT": {"leverage": 10, "is_btc": False},
    }

    # 运行回测
    print("\n运行串行回测...")
    bt = SerialCryptoEngine(config)
    metrics = bt.run(data_map, signal_map, atr_map, symbols_config)

    print("\n--- 趋势策略结果 ---")
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))

    # 逐笔交易
    print("\n--- 逐笔交易 ---")
    for t in bt.trades:
        hold_days = (t.exit_time - t.entry_time).days
        print(f"  {t.symbol:12s} {'LONG' if t.direction==1 else 'SHORT':5s} "
              f"entry={t.entry_price:.4f} exit={t.exit_price:.4f} "
              f"pnl={t.pnl:+.2f}U ({t.pnl_pct:+.1f}%) "
              f"hold={hold_days}d reason={t.exit_reason}")

    return metrics


def run_altcoin_backtest():
    """运行山寨币均值回归回测（1H, 5X）。"""
    print("\n" + "=" * 60)
    print("山寨币均值回归策略回测 (1H, 5X)")
    print("=" * 60)

    altcoins = ["SOL-USDT", "DOGE-USDT", "PEPE-USDT"]

    # 拉数据
    print("\n拉取 1H K线...")
    data_map = {}
    for symbol in altcoins:
        inst_id = f"{symbol}-SWAP"
        print(f"  {inst_id}...", end=" ")
        df = fetch_1h_klines(inst_id, days=60)
        if df.empty:
            print("FAIL, skipping")
            continue
        data_map[symbol] = df
        print(f"{len(df)} bars, {df.index[0]} ~ {df.index[-1]}")

    if not data_map:
        print("No data, aborting")
        return

    # 生成信号
    print("\n生成均值回归信号...")
    engine_signal = MeanReversionSignalEngine()
    signal_map = engine_signal.generate(data_map)
    atr_map = engine_signal.compute_atr_map(data_map)

    for symbol, sig in signal_map.items():
        longs = (sig == 1).sum()
        shorts = (sig == -1).sum()
        print(f"  {symbol}: {longs} longs, {shorts} shorts, {(sig==0).sum()} flat")

    # 配置
    config = SerialConfig(
        initial_capital=150.0,
        capital_per_trade=50.0,
        btc_leverage=5.0,
        altcoin_leverage=5.0,
        atr_stop_multiplier=1.0,     # 山寨止损更紧
        max_loss_pct=0.15,
        atr_profit_multiplier=2.0,   # 2:1 盈亏比
        btc_max_holding_bars=24,
        alt_max_holding_bars=24,     # 24h 时间退出
        symbol_priority={s: i for i, s in enumerate(altcoins)},
    )

    symbols_config = {s: {"leverage": 5, "is_btc": False} for s in data_map}

    # 运行回测
    print("\n运行串行回测...")
    bt = SerialCryptoEngine(config)
    metrics = bt.run(data_map, signal_map, atr_map, symbols_config)

    print("\n--- 均值回归策略结果 ---")
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))

    # 逐笔交易
    print("\n--- 逐笔交易 ---")
    for t in bt.trades:
        hold_hours = (t.exit_time - t.entry_time).total_seconds() / 3600
        print(f"  {t.symbol:12s} {'LONG' if t.direction==1 else 'SHORT':5s} "
              f"entry={t.entry_price:.6f} exit={t.exit_price:.6f} "
              f"pnl={t.pnl:+.2f}U ({t.pnl_pct:+.1f}%) "
              f"hold={hold_hours:.1f}h reason={t.exit_reason}")

    return metrics


if __name__ == "__main__":
    trend_metrics = run_trend_backtest()
    alt_metrics = run_altcoin_backtest()

    print("\n" + "=" * 60)
    print("汇总对比")
    print("=" * 60)
    if trend_metrics:
        print(f"\n趋势策略 (BTC+ETH, 10X):")
        print(f"  最终权益: {trend_metrics['final_equity']}U")
        print(f"  总收益: {trend_metrics['total_return_pct']}%")
        print(f"  最大回撤: {trend_metrics['max_drawdown_pct']}%")
        print(f"  胜率: {trend_metrics['win_rate_pct']}%")
        print(f"  交易次数: {trend_metrics['total_trades']}")
        print(f"  空仓时间: {trend_metrics['flat_time_pct']}%")

    if alt_metrics:
        print(f"\n均值回归 (山寨, 5X):")
        print(f"  最终权益: {alt_metrics['final_equity']}U")
        print(f"  总收益: {alt_metrics['total_return_pct']}%")
        print(f"  最大回撤: {alt_metrics['max_drawdown_pct']}%")
        print(f"  胜率: {alt_metrics['win_rate_pct']}%")
        print(f"  交易次数: {alt_metrics['total_trades']}")
        print(f"  空仓时间: {alt_metrics['flat_time_pct']}%")

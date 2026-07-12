"""回测脚本 v2：BTC 趋势 + 山寨均值回归，统一 180 天 1H 时间轴，串行执行。

改进：
- BTC 和山寨都拉 180 天 1H K线，时间轴统一
- BTC 趋势信号：1H resample 到 4H 算 EMA/ADX，信号映射回 1H
- 山寨均值回归：直接用 1H
- 动态资金管理：翻倍升级，腰斩降级
- 信号冷却：同一币种 6 bar 内不重复
"""

from __future__ import annotations

import json
import sys
import os
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import requests

from backtest.engines.serial_crypto import SerialCryptoEngine, SerialConfig
from backtest.engines.trend_signal import TrendSignalEngine
from backtest.engines.mean_reversion_signal import MeanReversionSignalEngine
from src.indicators.ta import compute_atr

BASE_URL = os.getenv("OKX_RELAY", "http://127.0.0.1:8080") + "/api/v5"
CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]


def fetch_klines(inst_id: str, bar: str = "1H", days: int = 180) -> pd.DataFrame:
    """拉取 K线数据。使用 /market/history-candles 获取更长历史。

    /market/candles 1H 最多返回 1440 条(60天)，不够 180 天需求。
    /market/history-candles 参数格式相同，但能返回更早的数据。
    限频 20次/2s，每页之间 sleep 0.1s 防触发限频。
    """
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    all_data = []
    after = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)

    while after > start_ms:
        resp = requests.get(f"{BASE_URL}/market/history-candles", params={
            "instId": inst_id, "bar": bar, "limit": "300", "after": str(after),
        }, timeout=15)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            break
        batch = data["data"]
        # 只保留已完结的 K线 (confirm == "1")
        batch = [r for r in batch if r[8] == "1"]
        all_data.extend(batch)
        after = int(batch[-1][0])
        if len(batch) < 300:
            break
        time.sleep(0.1)  # history-candles 限频 20次/2s

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


def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """1H -> 4H OHLCV resample。"""
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
    }
    df_4h = df_1h.resample("4h").agg(agg).dropna()
    return df_4h


def map_4h_signal_to_1h(sig_4h: pd.Series, df_1h: pd.DataFrame) -> pd.Series:
    """将 4H 信号映射到 1H 时间轴（前向填充）。"""
    sig_1h = sig_4h.reindex(df_1h.index, method="ffill").fillna(0).astype(int)
    return sig_1h


def map_4h_atr_to_1h(atr_4h: pd.Series, df_1h: pd.DataFrame) -> pd.Series:
    """将 4H ATR 映射到 1H 时间轴。"""
    atr_1h = atr_4h.reindex(df_1h.index, method="ffill").fillna(0)
    return atr_1h


def run_backtest():
    print("=" * 70)
    print("BTC 趋势(10X) + 山寨均值回归(5X) 串行回测 (180天, 1H统一)")
    print("=" * 70)

    btc_symbol = "BTC-USDT"
    alt_symbols = ["SOL-USDT", "DOGE-USDT", "PEPE-USDT", "WIF-USDT",
                   "BONK-USDT", "ARB-USDT", "OP-USDT", "TIA-USDT"]

    # --- 拉数据 ---
    print("\n拉取 1H K线 (180天)...")
    data_1h = {}
    all_symbols = [btc_symbol] + alt_symbols

    for symbol in all_symbols:
        inst_id = f"{symbol}-SWAP"
        print(f"  {inst_id}...", end=" ", flush=True)
        df = fetch_klines(inst_id, bar="1H", days=180)
        if df.empty:
            print("FAIL, skipping")
            continue
        data_1h[symbol] = df
        print(f"{len(df)} bars, {df.index[0].date()} ~ {df.index[-1].date()}")

    if btc_symbol not in data_1h:
        print("BTC data missing, aborting")
        return

    # --- BTC 趋势信号 (4H 指标映射到 1H) ---
    print("\n生成 BTC 趋势信号 (4H -> 1H)...")
    btc_4h = resample_4h(data_1h[btc_symbol])
    trend_engine = TrendSignalEngine()
    btc_data_4h = {btc_symbol: btc_4h}
    btc_signal_4h = trend_engine.generate(btc_data_4h)
    btc_atr_4h = trend_engine.compute_atr_map(btc_data_4h)

    # 映射回 1H
    btc_signal_1h = map_4h_signal_to_1h(btc_signal_4h[btc_symbol], data_1h[btc_symbol])
    btc_atr_1h = map_4h_atr_to_1h(btc_atr_4h[btc_symbol], data_1h[btc_symbol])

    signal_map = {btc_symbol: btc_signal_1h}
    atr_map = {btc_symbol: btc_atr_1h}

    longs = (btc_signal_1h == 1).sum()
    shorts = (btc_signal_1h == -1).sum()
    print(f"  BTC: {longs} longs, {shorts} shorts, {(btc_signal_1h==0).sum()} flat")

    # --- 山寨均值回归信号 (1H) ---
    print("\n生成山寨均值回归信号 (1H)...")
    mr_engine = MeanReversionSignalEngine(rsi_oversold=30, rsi_overbought=70)
    available_alts = [s for s in alt_symbols if s in data_1h]
    alt_data_1h = {s: data_1h[s] for s in available_alts}
    alt_signals = mr_engine.generate(alt_data_1h)
    alt_atrs = mr_engine.compute_atr_map(alt_data_1h)

    for s in available_alts:
        sig = alt_signals[s]
        longs = (sig == 1).sum()
        shorts = (sig == -1).sum()
        print(f"  {s}: {longs} longs, {shorts} shorts, {(sig==0).sum()} flat")
        signal_map[s] = sig
        atr_map[s] = alt_atrs[s]

    # --- 统一时间轴 ---
    all_dates = set()
    for df in data_1h.values():
        all_dates.update(df.index)
    dates = pd.DatetimeIndex(sorted(all_dates))
    print(f"\n统一时间轴: {len(dates)} bars, {dates[0].date()} ~ {dates[-1].date()}")

    # --- 配置 ---
    priority = {btc_symbol: 0}
    for i, s in enumerate(available_alts):
        priority[s] = i + 1

    config = SerialConfig(
        initial_capital=150.0,
        capital_per_trade=50.0,
        btc_leverage=10.0,
        altcoin_leverage=5.0,
        atr_stop_multiplier=1.5,
        max_loss_pct=0.15,
        atr_profit_multiplier=3.0,
        btc_max_holding_bars=360,     # 15天 × 24 bar/天 (1H)
        alt_max_holding_bars=24,      # 24h
        signal_cooldown_bars=6,
        symbol_priority=priority,
    )

    symbols_config = {btc_symbol: {"leverage": 10, "is_btc": True}}
    for s in available_alts:
        symbols_config[s] = {"leverage": 5, "is_btc": False}

    # --- 运行回测 ---
    print("\n运行串行回测...")
    bt = SerialCryptoEngine(config)
    metrics = bt.run(data_1h, signal_map, atr_map, symbols_config)

    # --- 输出 ---
    print("\n" + "=" * 70)
    print("回测结果")
    print("=" * 70)
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))

    # 升降级历史
    if metrics.get("tier_changes"):
        print("\n--- 资金升降级记录 ---")
        for tc in metrics["tier_changes"]:
            print(f"  {tc['timestamp'][:19]} {tc['action']:10s} "
                  f"{tc['old_capital']:.1f}U -> {tc['new_capital']:.1f}U "
                  f"(权益 {tc['total_equity']:.1f}U)")

    # 逐笔交易
    print(f"\n--- 逐笔交易 ({len(bt.trades)} 笔) ---")
    for t in bt.trades:
        hold_h = (t.exit_time - t.entry_time).total_seconds() / 3600
        print(f"  {t.symbol:12s} {'LONG' if t.direction==1 else 'SHORT':5s} "
              f"entry={t.entry_price:.6f} exit={t.exit_price:.6f} "
              f"pnl={t.pnl:+8.2f}U ({t.pnl_pct:+6.1f}%) "
              f"hold={hold_h:6.1f}h reason={t.exit_reason}")

    # 汇总
    print(f"\n{'='*70}")
    print("汇总")
    print(f"{'='*70}")
    print(f"  最终权益:     {metrics['final_equity']}U")
    print(f"  总收益:       {metrics['total_return_pct']}%")
    print(f"  最大回撤:     {metrics['max_drawdown_pct']}%")
    print(f"  胜率:         {metrics['win_rate_pct']}%")
    print(f"  盈利因子:     {metrics['profit_factor']}")
    print(f"  交易次数:     {metrics['total_trades']}")
    print(f"  空仓时间:     {metrics['flat_time_pct']}%")
    print(f"  每份资金:     {metrics['final_capital_per_trade']}U")
    if metrics.get("tier_changes"):
        print(f"  升降级次数:   {len(metrics['tier_changes'])}")

    # 按币种
    print(f"\n  按币种:")
    for sym, stats in metrics.get("by_symbol", {}).items():
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
        print(f"    {sym:12s} {stats['trades']:3d} trades, "
              f"{wr:5.1f}% win, pnl={stats['pnl']:+8.2f}U")

    return metrics


if __name__ == "__main__":
    run_backtest()

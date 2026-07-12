"""策略B样本外验证：用全新币种测试优化后的参数。

训练集（已优化参数）：BTC/SOL/DOGE/PEPE/WIF/BONK/ARB/OP/TIA
测试集（全新币种）：ETH/AVAX/LINK/NEAR/APT/FIL/INJ/RNDR/FTM/ATOM
参数：BBW=0.25 VolM=2.0 ATR=1.5 (训练集中f_kelly最高且样本量最大的组合)
"""

from __future__ import annotations

import sys
import os
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
import requests

from backtest.engines.serial_crypto import SerialCryptoEngine, SerialConfig
from backtest.engines.volatility_breakout_signal import VolatilityBreakoutSignalEngine
from backtest.engines.volatility_breakout_exit import VolatilityBreakoutExit
from backtest.run_volatility_backtest import bootstrap_ci, wilcoxon_test, CACHE_DIR

BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"
CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]


def fetch_klines(inst_id: str, bar: str = "4H", days: int = 180) -> pd.DataFrame:
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


def run_validation(symbols, data_4h, label=""):
    """对给定币种集合跑回测 + 统计验证。"""
    btc_symbol = "BTC-USDT"

    signal_engine = VolatilityBreakoutSignalEngine(
        bb_window=20, bb_std=2.0,
        bbw_lookback=120, bbw_percentile=0.25,
        volume_multiplier=2.0, volume_ma=20,
    )
    signal_map = signal_engine.generate(data_4h)
    atr_map = signal_engine.compute_atr_map(data_4h)

    for s in symbols:
        if s not in signal_map:
            continue
        sig = signal_map[s]
        longs = (sig == 1).sum()
        shorts = (sig == -1).sum()
        print(f"  {s:12s} long={longs:4d} short={shorts:4d} flat={(sig==0).sum():4d}")

    priority = {}
    for i, s in enumerate(symbols):
        priority[s] = i

    config = SerialConfig(
        initial_capital=150.0, capital_per_trade=50.0,
        btc_leverage=5.0, altcoin_leverage=3.0,
        maker_rate=0.0002, taker_rate=0.0005,
        slippage_rate=0.0005,
        signal_cooldown_bars=3, symbol_priority=priority,
    )
    symbols_config = {}
    for s in symbols:
        symbols_config[s] = {"leverage": 5 if s == btc_symbol else 3, "is_btc": s == btc_symbol}

    exit_strategy = VolatilityBreakoutExit(
        bb_window=20, bb_std=2.0,
        atr_trail_multiplier=1.5,
        atr_lock_multiplier=3.0,
        max_holding_bars=120,
    )

    bt = SerialCryptoEngine(config, exit_strategy=exit_strategy)
    metrics = bt.run(data_4h, signal_map, atr_map, symbols_config)

    print(f"\n  结果:")
    print(f"    最终权益: {metrics['final_equity']}U ({metrics['total_return_pct']:+.1f}%)")
    print(f"    交易次数: {metrics['total_trades']}")
    print(f"    胜率: {metrics['win_rate_pct']}%")
    print(f"    盈利因子: {metrics['profit_factor']}")
    print(f"    最大回撤: {metrics['max_drawdown_pct']}%")

    if metrics.get("by_symbol"):
        print(f"\n    按币种:")
        for sym, stats in metrics["by_symbol"].items():
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
            print(f"      {sym:12s} {stats['trades']:3d} trades, {wr:5.1f}% win, pnl={stats['pnl']:+8.2f}U")

    # 逐笔
    print(f"\n    逐笔交易 ({len(bt.trades)} 笔):")
    for t in bt.trades:
        hold_h = (t.exit_time - t.entry_time).total_seconds() / 3600
        print(f"      {t.symbol:12s} {'LONG' if t.direction==1 else 'SHORT':5s} "
              f"pnl={t.pnl:+8.2f}U ({t.pnl_pct:+6.1f}%) hold={hold_h:6.1f}h reason={t.exit_reason}")

    # 统计验证
    if len(bt.trades) >= 10:
        returns_list = []
        for t in bt.trades:
            margin = t.size * t.entry_price / t.leverage
            ret = t.pnl / margin if margin > 0 else 0
            returns_list.append(ret)
        returns_arr = np.array(returns_list)

        ci_lower, ci_upper = bootstrap_ci(returns_arr, n_boot=10000)
        wilcox = wilcoxon_test(returns_arr)

        wins = [t for t in bt.trades if t.pnl > 0]
        losses = [t for t in bt.trades if t.pnl <= 0]
        p = len(wins) / len(bt.trades) if bt.trades else 0
        avg_win = np.mean([t.pnl for t in wins]) if wins else 0
        avg_loss = np.mean([abs(t.pnl) for t in losses]) if losses else 0
        b = avg_win / avg_loss if avg_loss > 0 else 0
        f_kelly = p - (1 - p) / b if b > 0 else -999

        print(f"\n    统计验证:")
        print(f"      E[R] = {np.mean(returns_arr)*100:.4f}%")
        print(f"      Bootstrap 95% CI: [{ci_lower*100:.4f}%, {ci_upper*100:.4f}%]")
        print(f"      CI下界 > 0: {'是' if ci_lower > 0 else '否'}")
        print(f"      Wilcoxon p = {wilcox['p_value']}, 显著: {wilcox['significant']}")
        print(f"      f_kelly = {f_kelly:.4f}")
    else:
        print(f"\n    样本不足 ({len(bt.trades)} < 10), 跳过统计验证")
        f_kelly = None
        ci_lower = None
        wilcox = {"p_value": None, "significant": False}

    return metrics, bt.trades, f_kelly, ci_lower, wilcox


def main():
    print("=" * 70)
    print("策略B 样本外验证 (全新币种)")
    print("参数: BBW=0.25 VolM=2.0 ATR=1.5 (训练集优化结果)")
    print("=" * 70)

    # 全新币种（未参与参数优化）
    test_symbols = [
        "ETH-USDT", "AVAX-USDT", "LINK-USDT", "NEAR-USDT", "APT-USDT",
        "FIL-USDT", "INJ-USDT", "RNDR-USDT", "FTM-USDT", "ATOM-USDT",
        "ADA-USDT", "XRP-USDT", "LTC-USDT", "TRX-USDT", "MATIC-USDT",
    ]

    print(f"\n拉取 {len(test_symbols)} 个新币种 4H 数据...")
    data_4h = {}
    for symbol in test_symbols:
        inst_id = f"{symbol}-SWAP"
        print(f"  {inst_id}...", end=" ", flush=True)
        df = fetch_klines(inst_id, bar="4H", days=180)
        if df.empty:
            print("FAIL")
            continue
        data_4h[symbol] = df
        print(f"{len(df)} bars")

    available = [s for s in test_symbols if s in data_4h]
    print(f"\n成功: {len(available)}/{len(test_symbols)} 个币种")

    # === 样本外验证 ===
    print(f"\n{'='*70}")
    print(f"样本外验证 ({len(available)} 个全新币种)")
    print(f"{'='*70}")

    metrics, trades, f_kelly, ci_lower, wilcox = run_validation(
        available, data_4h, "样本外"
    )

    # === 对比：训练集也跑一遍同样参数 ===
    print(f"\n{'='*70}")
    print(f"训练集对比 (原 9 币种, 同参数)")
    print(f"{'='*70}")

    train_symbols = ["BTC-USDT", "SOL-USDT", "DOGE-USDT", "PEPE-USDT", "WIF-USDT",
                     "BONK-USDT", "ARB-USDT", "OP-USDT", "TIA-USDT"]
    train_data = {}
    for s in train_symbols:
        inst_id = f"{s}-SWAP"
        cache_key = f"{inst_id}_4H_180d.csv"
        cache_path = os.path.join(CACHE_DIR, cache_key)
        if os.path.exists(cache_path):
            df = pd.read_csv(cache_path, index_col="ts", parse_dates=True)
            if not df.empty:
                train_data[s] = df[["open", "high", "low", "close", "vol"]]

    train_metrics, train_trades, train_fk, train_ci, train_wilcox = run_validation(
        train_symbols, train_data, "训练集"
    )

    # === 汇总对比 ===
    print(f"\n{'='*70}")
    print(f"训练集 vs 样本外 对比")
    print(f"{'='*70}")
    print(f"  {'指标':<20} {'训练集':>12} {'样本外':>12}")
    print(f"  {'-'*44}")
    print(f"  {'币种数':<20} {len(train_data):>12} {len(available):>12}")
    print(f"  {'交易次数':<20} {len(train_trades):>12} {len(trades):>12}")
    print(f"  {'最终权益':<20} {train_metrics['final_equity']:>12.1f} {metrics['final_equity']:>12.1f}")
    print(f"  {'总收益%':<20} {train_metrics['total_return_pct']:>+12.1f} {metrics['total_return_pct']:>+12.1f}")
    print(f"  {'胜率%':<20} {train_metrics['win_rate_pct']:>12.1f} {metrics['win_rate_pct']:>12.1f}")
    print(f"  {'盈利因子':<20} {train_metrics['profit_factor']:>12.2f} {metrics['profit_factor']:>12.2f}")
    if f_kelly is not None and train_fk is not None:
        print(f"  {'f_kelly':<20} {train_fk:>+12.4f} {f_kelly:>+12.4f}")
    if ci_lower is not None and train_ci is not None:
        print(f"  {'Bootstrap CI下界%':<20} {train_ci*100:>+12.2f} {ci_lower*100:>+12.2f}")
    print(f"  {'Wilcoxon p':<20} {train_wilcox['p_value']:>12} {wilcox['p_value']:>12}")

    # 最终判定
    print(f"\n  {'='*40}")
    if f_kelly is not None and f_kelly > 0:
        print(f"  [PASS] 样本外 f_kelly > 0, edge 可能真实存在")
    elif f_kelly is not None and f_kelly > -0.05:
        print(f"  [BORDERLINE] 样本外 f_kelly 接近 0, 需要更多数据")
    else:
        print(f"  [FAIL] 样本外 f_kelly < 0, 参数可能过拟合")


if __name__ == "__main__":
    main()

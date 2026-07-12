"""策略B回测脚本：波动率突破 4H + 统计验证。

改进：
- 4H K线直接回测（不做1H映射）
- 引擎使用策略接口（VolatilityBreakoutExit）
- 纳入交易成本（taker 0.05%×2 + 滑点 0.05%）
- Bootstrap 置信区间验证 E[R] > 0
- Wilcoxon 符号秩检验
- 1/4 凯利仓位计算
"""

from __future__ import annotations

import json
import sys
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
import requests

from backtest.engines.serial_crypto import SerialCryptoEngine, SerialConfig
from backtest.engines.volatility_breakout_signal import VolatilityBreakoutSignalEngine
from backtest.engines.volatility_breakout_exit import VolatilityBreakoutExit

BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"
CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")


def fetch_klines(inst_id: str, bar: str = "4H", days: int = 180) -> pd.DataFrame:
    """拉取 K线数据，带本地 CSV 缓存。"""
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


def bootstrap_ci(returns: np.ndarray, n_boot: int = 10000, ci: float = 0.95) -> tuple:
    """Bootstrap 置信区间。"""
    n = len(returns)
    if n == 0:
        return 0.0, 0.0
    boot_means = np.zeros(n_boot)
    for i in range(n_boot):
        sample = np.random.choice(returns, size=n, replace=True)
        boot_means[i] = np.mean(sample)
    lower = np.percentile(boot_means, (1 - ci) / 2 * 100)
    upper = np.percentile(boot_means, (1 + ci) / 2 * 100)
    return lower, upper


def wilcoxon_test(returns: np.ndarray) -> dict:
    """Wilcoxon 符号秩检验。"""
    try:
        from scipy.stats import wilcoxon
        non_zero = returns[returns != 0]
        if len(non_zero) < 10:
            return {"statistic": None, "p_value": 1.0, "significant": False}
        stat, p = wilcoxon(non_zero, alternative="greater")
        return {
            "statistic": round(stat, 2),
            "p_value": round(p, 6),
            "significant": p < 0.05,
        }
    except ImportError:
        return {"statistic": None, "p_value": None, "significant": False, "error": "scipy not available"}


def run_backtest():
    print("=" * 70)
    print("策略B: 波动率突破 4H 回测 + 统计验证")
    print("=" * 70)

    btc_symbol = "BTC-USDT"
    alt_symbols = ["SOL-USDT", "DOGE-USDT", "PEPE-USDT", "WIF-USDT",
                   "BONK-USDT", "ARB-USDT", "OP-USDT", "TIA-USDT"]

    # --- 拉数据 ---
    print("\n拉取 4H K线 (180天)...")
    data_4h = {}
    all_symbols = [btc_symbol] + alt_symbols

    for symbol in all_symbols:
        inst_id = f"{symbol}-SWAP"
        print(f"  {inst_id}...", end=" ", flush=True)
        df = fetch_klines(inst_id, bar="4H", days=180)
        if df.empty:
            print("FAIL, skipping")
            continue
        data_4h[symbol] = df
        print(f"{len(df)} bars, {df.index[0].date()} ~ {df.index[-1].date()}")

    if btc_symbol not in data_4h:
        print("BTC data missing, aborting")
        return

    available_alts = [s for s in alt_symbols if s in data_4h]

    # --- 生成信号 ---
    print("\n生成波动率突破信号 (4H)...")
    signal_engine = VolatilityBreakoutSignalEngine(
        bb_window=20,
        bb_std=2.0,
        bbw_lookback=120,
        bbw_percentile=0.20,
        volume_multiplier=1.5,
        volume_ma=20,
    )
    signal_map = signal_engine.generate(data_4h)
    atr_map = signal_engine.compute_atr_map(data_4h)

    for s in [btc_symbol] + available_alts:
        if s not in signal_map:
            continue
        sig = signal_map[s]
        longs = (sig == 1).sum()
        shorts = (sig == -1).sum()
        print(f"  {s:12s} long={longs:4d} short={shorts:4d} flat={(sig==0).sum():4d}")

    # --- 配置 ---
    priority = {btc_symbol: 0}
    for i, s in enumerate(available_alts):
        priority[s] = i + 1

    config = SerialConfig(
        initial_capital=150.0,
        capital_per_trade=50.0,
        btc_leverage=5.0,
        altcoin_leverage=3.0,
        maker_rate=0.0002,
        taker_rate=0.0005,
        slippage_rate=0.0005,
        signal_cooldown_bars=3,
        symbol_priority=priority,
    )

    symbols_config = {btc_symbol: {"leverage": 5, "is_btc": True}}
    for s in available_alts:
        symbols_config[s] = {"leverage": 3, "is_btc": False}

    # --- 退出策略 ---
    exit_strategy = VolatilityBreakoutExit(
        bb_window=20,
        bb_std=2.0,
        atr_trail_multiplier=2.0,
        atr_lock_multiplier=4.0,
        max_holding_bars=120,  # 20天 × 6 bar/天 (4H)
    )

    # --- 运行回测 ---
    print("\n运行回测...")
    bt = SerialCryptoEngine(config, exit_strategy=exit_strategy)
    metrics = bt.run(data_4h, signal_map, atr_map, symbols_config)

    # --- 输出 ---
    print("\n" + "=" * 70)
    print("回测结果")
    print("=" * 70)
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))

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

    # 按币种
    print(f"\n  按币种:")
    for sym, stats in metrics.get("by_symbol", {}).items():
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
        print(f"    {sym:12s} {stats['trades']:3d} trades, "
              f"{wr:5.1f}% win, pnl={stats['pnl']:+8.2f}U")

    # --- 统计验证 ---
    print(f"\n{'='*70}")
    print("统计验证")
    print(f"{'='*70}")

    if len(bt.trades) < 10:
        print(f"  交易次数不足 ({len(bt.trades)} < 10)，无法做统计验证")
        return metrics

    # 计算每笔交易的 R 倍数（PnL / 风险）
    # 风险 = 保证金 × 3%（风控红线单笔最大亏损）
    returns_list = []
    r_multiples = []
    for t in bt.trades:
        margin = t.size * t.entry_price / t.leverage
        risk = margin * 0.03  # 3% 风险
        ret = t.pnl / margin if margin > 0 else 0  # 收益率（基于保证金）
        r_mult = t.pnl / risk if risk > 0 else 0
        returns_list.append(ret)
        r_multiples.append(r_mult)

    returns_arr = np.array(returns_list)
    r_arr = np.array(r_multiples)

    # 1. 期望值
    mean_ret = np.mean(returns_arr)
    mean_r = np.mean(r_arr)
    print(f"\n  每笔交易收益率 (基于保证金):")
    print(f"    均值 E[R] = {mean_ret*100:.4f}%")
    print(f"    中位数 = {np.median(returns_arr)*100:.4f}%")
    print(f"    标准差 = {np.std(returns_arr)*100:.4f}%")
    print(f"    偏度 = {pd.Series(returns_arr).skew():.4f}")
    print(f"    峰度 = {pd.Series(returns_arr).kurtosis():.4f}")

    print(f"\n  R 倍数 (PnL/风险):")
    print(f"    均值 = {mean_r:.4f}R")
    print(f"    中位数 = {np.median(r_arr):.4f}R")

    # 2. Bootstrap 置信区间
    ci_lower, ci_upper = bootstrap_ci(returns_arr, n_boot=10000)
    print(f"\n  Bootstrap 95% CI (E[R]):")
    print(f"    [{ci_lower*100:.4f}%, {ci_upper*100:.4f}%]")
    print(f"    下界 > 0: {'是' if ci_lower > 0 else '否'}")

    # 3. Wilcoxon 检验
    wilcox = wilcoxon_test(returns_arr)
    print(f"\n  Wilcoxon 符号秩检验:")
    print(f"    统计量 = {wilcox['statistic']}")
    print(f"    p-value = {wilcox['p_value']}")
    print(f"    显著 (p<0.05): {wilcox['significant']}")

    # 4. 凯利公式
    wins = [t for t in bt.trades if t.pnl > 0]
    losses = [t for t in bt.trades if t.pnl <= 0]
    p = len(wins) / len(bt.trades) if bt.trades else 0
    avg_win = np.mean([t.pnl for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t.pnl) for t in losses]) if losses else 0
    b = avg_win / avg_loss if avg_loss > 0 else 0
    f_kelly = p - (1 - p) / b if b > 0 else -999

    print(f"\n  凯利公式:")
    print(f"    胜率 p = {p:.4f}")
    print(f"    盈亏比 b = {b:.4f}")
    print(f"    f_kelly = {f_kelly:.4f}")
    print(f"    1/4 kelly = {f_kelly/4:.4f}" if f_kelly > 0 else "    f_kelly < 0, 不应下注")

    # 5. 结论
    print(f"\n  {'='*40}")
    print(f"  结论:")
    passed = []
    failed = []
    if len(bt.trades) >= 30:
        passed.append("最小样本量 ≥ 30")
    else:
        failed.append(f"样本量不足 ({len(bt.trades)} < 30)")

    if ci_lower > 0:
        passed.append("Bootstrap CI 下界 > 0")
    else:
        failed.append("Bootstrap CI 下界 ≤ 0")

    if wilcox['significant']:
        passed.append("Wilcoxon 检验显著 (p<0.05)")
    else:
        failed.append("Wilcoxon 检验不显著")

    if f_kelly > 0:
        passed.append(f"f_kelly > 0 ({f_kelly:.4f})")
    else:
        failed.append(f"f_kelly ≤ 0 ({f_kelly:.4f})")

    print(f"  通过: {passed}")
    print(f"  未通过: {failed}")

    if failed:
        print(f"\n  [WARN] 策略未通过统计验证，不上线")
    else:
        print(f"\n  [PASS] 策略通过统计验证，可上线")

    return metrics


if __name__ == "__main__":
    run_backtest()

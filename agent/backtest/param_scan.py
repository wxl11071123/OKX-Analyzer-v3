"""策略B参数扫描：找最优 BBW 分位 + 成交量倍数组合。

数据从本地缓存读取，不拉网络。每个组合跑完整回测 + 统计验证。
"""

from __future__ import annotations

import sys
import os
from itertools import product

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np

from backtest.engines.serial_crypto import SerialCryptoEngine, SerialConfig
from backtest.engines.volatility_breakout_signal import VolatilityBreakoutSignalEngine
from backtest.engines.volatility_breakout_exit import VolatilityBreakoutExit
from backtest.run_volatility_backtest import bootstrap_ci, wilcoxon_test, CACHE_DIR


def load_cached_data(symbols, bar="4H", days=180):
    """从缓存加载所有币种数据。"""
    data = {}
    for symbol in symbols:
        inst_id = f"{symbol}-SWAP"
        cache_key = f"{inst_id}_{bar}_{days}d.csv"
        cache_path = os.path.join(CACHE_DIR, cache_key)
        if os.path.exists(cache_path):
            df = pd.read_csv(cache_path, index_col="ts", parse_dates=True)
            if not df.empty:
                data[symbol] = df[["open", "high", "low", "close", "vol"]]
    return data


def run_single(data_4h, btc_symbol, available_alts, bbw_pct, vol_mult, atr_trail, max_hold):
    """跑单个参数组合。"""
    signal_engine = VolatilityBreakoutSignalEngine(
        bb_window=20, bb_std=2.0,
        bbw_lookback=120, bbw_percentile=bbw_pct,
        volume_multiplier=vol_mult, volume_ma=20,
    )
    signal_map = signal_engine.generate(data_4h)
    atr_map = signal_engine.compute_atr_map(data_4h)

    priority = {btc_symbol: 0}
    for i, s in enumerate(available_alts):
        priority[s] = i + 1

    config = SerialConfig(
        initial_capital=150.0, capital_per_trade=50.0,
        btc_leverage=5.0, altcoin_leverage=3.0,
        maker_rate=0.0002, taker_rate=0.0005,
        slippage_rate=0.0005,
        signal_cooldown_bars=3, symbol_priority=priority,
    )
    symbols_config = {btc_symbol: {"leverage": 5, "is_btc": True}}
    for s in available_alts:
        symbols_config[s] = {"leverage": 3, "is_btc": False}

    exit_strategy = VolatilityBreakoutExit(
        bb_window=20, bb_std=2.0,
        atr_trail_multiplier=atr_trail,
        atr_lock_multiplier=atr_trail * 2,
        max_holding_bars=max_hold,
    )

    bt = SerialCryptoEngine(config, exit_strategy=exit_strategy)
    metrics = bt.run(data_4h, signal_map, atr_map, symbols_config)

    # 统计
    if len(bt.trades) < 10:
        return None

    returns_list = []
    for t in bt.trades:
        margin = t.size * t.entry_price / t.leverage
        ret = t.pnl / margin if margin > 0 else 0
        returns_list.append(ret)
    returns_arr = np.array(returns_list)

    ci_lower, ci_upper = bootstrap_ci(returns_arr, n_boot=5000)
    wilcox = wilcoxon_test(returns_arr)

    wins = [t for t in bt.trades if t.pnl > 0]
    losses = [t for t in bt.trades if t.pnl <= 0]
    p = len(wins) / len(bt.trades) if bt.trades else 0
    avg_win = np.mean([t.pnl for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t.pnl) for t in losses]) if losses else 0
    b = avg_win / avg_loss if avg_loss > 0 else 0
    f_kelly = p - (1 - p) / b if b > 0 else -999

    return {
        "bbw_pct": bbw_pct,
        "vol_mult": vol_mult,
        "atr_trail": atr_trail,
        "max_hold": max_hold,
        "trades": len(bt.trades),
        "final_equity": metrics["final_equity"],
        "return_pct": metrics["total_return_pct"],
        "max_dd": metrics["max_drawdown_pct"],
        "win_rate": metrics["win_rate_pct"],
        "payoff_ratio": round(b, 2),
        "profit_factor": metrics["profit_factor"],
        "f_kelly": round(f_kelly, 4),
        "ci_lower": round(ci_lower * 100, 2),
        "wilcoxon_p": wilcox["p_value"],
        "avg_hold_bars": metrics["avg_holding_bars"],
    }


def main():
    btc_symbol = "BTC-USDT"
    alt_symbols = ["SOL-USDT", "DOGE-USDT", "PEPE-USDT", "WIF-USDT",
                   "BONK-USDT", "ARB-USDT", "OP-USDT", "TIA-USDT"]

    print("加载缓存数据...")
    data_4h = load_cached_data([btc_symbol] + alt_symbols)
    available_alts = [s for s in alt_symbols if s in data_4h]
    print(f"  {len(data_4h)} 个币种, {len(data_4h.get(btc_symbol, pd.DataFrame()))} bars/币种")

    # 参数网格
    param_grid = list(product(
        [0.10, 0.15, 0.20, 0.25],       # bbw_percentile
        [1.5, 2.0, 2.5],                  # volume_multiplier
        [1.5, 2.0, 3.0],                  # atr_trail_multiplier (止损宽度)
        [120],                             # max_holding_bars
    ))

    print(f"\n参数扫描: {len(param_grid)} 个组合\n")
    print(f"{'BBW%':>5} {'VolM':>5} {'ATR':>5} {'Hold':>5} | "
          f"{'Trades':>6} {'Equity':>8} {'Return':>8} {'MaxDD':>8} "
          f"{'WinR':>6} {'PF':>5} {'B':>5} {'f_k':>7} {'CI_lo':>8} {'Wilcoxp':>9}")
    print("-" * 110)

    results = []
    for bbw_pct, vol_mult, atr_trail, max_hold in param_grid:
        r = run_single(data_4h, btc_symbol, available_alts, bbw_pct, vol_mult, atr_trail, max_hold)
        if r is None:
            print(f"{bbw_pct:>5.2f} {vol_mult:>5.1f} {atr_trail:>5.1f} {max_hold:>5d} |  <10 trades, skipped")
            continue
        results.append(r)
        print(f"{bbw_pct:>5.2f} {vol_mult:>5.1f} {atr_trail:>5.1f} {max_hold:>5d} | "
              f"{r['trades']:>6d} {r['final_equity']:>8.1f} {r['return_pct']:>+8.1f} {r['max_dd']:>+8.1f} "
              f"{r['win_rate']:>6.1f} {r['profit_factor']:>5.2f} {r['payoff_ratio']:>5.2f} "
              f"{r['f_kelly']:>+7.4f} {r['ci_lower']:>+8.2f} {r['wilcoxon_p']:>9.4f}")

    # 排序找最优
    print(f"\n{'='*110}")
    print("按 f_kelly 排序 TOP 5:")
    print(f"{'='*110}")
    sorted_results = sorted(results, key=lambda x: x["f_kelly"], reverse=True)
    for i, r in enumerate(sorted_results[:5]):
        print(f"  #{i+1} BBW={r['bbw_pct']:.2f} VolM={r['vol_mult']:.1f} ATR={r['atr_trail']:.1f} | "
              f"{r['trades']}笔 权益={r['final_equity']:.1f}U 胜率={r['win_rate']:.1f}% "
              f"盈亏比={r['payoff_ratio']:.2f} f_kelly={r['f_kelly']:+.4f} "
              f"CI_lo={r['ci_lower']:+.2f}% Wilcoxp={r['wilcoxon_p']:.4f}")

    # 找 f_kelly > 0 的组合
    positive = [r for r in results if r["f_kelly"] > 0]
    if positive:
        print(f"\n[PASS] {len(positive)} 个组合 f_kelly > 0:")
        for r in sorted(positive, key=lambda x: x["f_kelly"], reverse=True):
            print(f"  BBW={r['bbw_pct']:.2f} VolM={r['vol_mult']:.1f} ATR={r['atr_trail']:.1f} | "
                  f"f_kelly={r['f_kelly']:+.4f} 胜率={r['win_rate']:.1f}% 盈亏比={r['payoff_ratio']:.2f} "
                  f"CI_lo={r['ci_lower']:+.2f}% Wilcoxp={r['wilcoxon_p']:.4f}")
    else:
        print(f"\n[WARN] 没有组合 f_kelly > 0")


if __name__ == "__main__":
    main()

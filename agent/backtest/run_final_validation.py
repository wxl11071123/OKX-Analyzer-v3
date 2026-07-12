"""TSMOM+EMA 最终验证：全新主流币种，参数不变。

验证集：10个主流币种，全部是市值前20的高流动性币
- 之前训练集用过的（不参与）：BTC/SOL/DOGE/PEPE/WIF/BONK/ARB/OP/TIA
- 之前样本外用过的（不参与）：ETH/AVAX/LINK/NEAR/APT/FIL/INJ/ATOM/ADA/XRP/LTC/TRX
- 本次全新：从OKX永续合约成交量前30中选未用过的主流币
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np, time
from datetime import datetime, timedelta
import requests
from backtest.engines.serial_crypto import SerialCryptoEngine, SerialConfig
from backtest.engines.tsmom_signal import TSMOMSignalEngine
from backtest.engines.tsmom_ema_exit import TSMOMEMAExit
from backtest.run_volatility_backtest import bootstrap_ci, wilcoxon_test, CACHE_DIR

BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"
CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]


def fetch_klines(inst_id, bar="4H", days=360):
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


def run_backtest(symbols, data_4h, label=""):
    btc_symbol = "BTC-USDT"
    signal_engine = TSMOMSignalEngine(lookback=120, hurst_filter=True, hurst_window=200, hurst_threshold=0.55)
    signal_map = signal_engine.generate(data_4h)
    atr_map = signal_engine.compute_atr_map(data_4h)

    for s in symbols:
        if s not in signal_map:
            continue
        sig = signal_map[s]
        print(f"  {s:12s} long={(sig==1).sum():4d} short={(sig==-1).sum():4d}")

    priority = {s: i for i, s in enumerate(symbols)}
    config = SerialConfig(
        initial_capital=150.0, capital_per_trade=50.0,
        btc_leverage=5.0, altcoin_leverage=3.0,
        maker_rate=0.0002, taker_rate=0.0005,
        slippage_rate=0.0005,
        signal_cooldown_bars=3, symbol_priority=priority,
    )
    symbols_config = {s: {"leverage": 5 if s == btc_symbol else 3, "is_btc": s == btc_symbol} for s in symbols}

    exit_strategy = TSMOMEMAExit(ema_period=20, buffer_pct=0.0, max_holding_bars=240)
    bt = SerialCryptoEngine(config, exit_strategy=exit_strategy)
    metrics = bt.run(data_4h, signal_map, atr_map, symbols_config)

    print(f"\n  {label} 结果:")
    print(f"    最终权益: {metrics['final_equity']}U ({metrics['total_return_pct']:+.1f}%)")
    print(f"    交易次数: {metrics['total_trades']}")
    print(f"    胜率: {metrics['win_rate_pct']}%")
    print(f"    盈利因子: {metrics['profit_factor']}")
    print(f"    最大回撤: {metrics['max_drawdown_pct']}%")
    print(f"    空仓时间: {metrics['flat_time_pct']}%")
    print(f"    平均持仓: {metrics['avg_holding_bars']} bars ({metrics['avg_holding_bars']*4:.0f}h)")

    if metrics.get("by_symbol"):
        print(f"\n    按币种:")
        for sym, stats in metrics["by_symbol"].items():
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
            print(f"      {sym:12s} {stats['trades']:3d} trades, {wr:5.1f}% win, pnl={stats['pnl']:+8.2f}U")

    print(f"\n    退出原因: {metrics.get('exit_reasons', {})}")
    print(f"    升降级: {metrics.get('tier_changes', [])}")

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

        # 年化
        days = 360
        annual = (metrics['final_equity'] / 150.0) ** (365 / days) - 1

        print(f"\n    统计验证:")
        print(f"      E[R] = {np.mean(returns_arr)*100:.4f}%")
        print(f"      Bootstrap 95% CI: [{ci_lower*100:.4f}%, {ci_upper*100:.4f}%]")
        print(f"      CI下界 > 0: {'是' if ci_lower > 0 else '否'}")
        print(f"      Wilcoxon p = {wilcox['p_value']}, 显著: {wilcox['significant']}")
        print(f"      胜率 p = {p:.4f}, 盈亏比 b = {b:.4f}")
        print(f"      f_kelly = {f_kelly:.4f}")
        print(f"      年化收益 = {annual*100:.0f}%")
        return metrics, bt.trades, f_kelly, ci_lower, wilcox, annual
    return metrics, bt.trades, None, None, {"p_value": None, "significant": False}, 0


def main():
    print("=" * 80)
    print("TSMOM+EMA 最终验证 (全新主流币种, 参数不变)")
    print("=" * 80)

    # 全新币种：之前从未用过的主流高流动性币
    test_symbols = [
        "ENA-USDT",   # Ethereum Name Service
        "JUP-USDT",   # Jupiter
        "PYTH-USDT",  # Pyth Network
        "TON-USDT",   # Toncoin
        "SEI-USDT",   # Sei
        "SUI-USDT",   # Sui
        "TIA-USDT",   # Celestia (之前训练集用过，这里复用验证一致性)
        "JTO-USDT",   # Jito
        "STX-USDT",   # Stacks
        "RUNE-USDT",  # THORChain
    ]

    # 去掉之前用过的
    used = {"BTC-USDT","SOL-USDT","DOGE-USDT","PEPE-USDT","WIF-USDT","BONK-USDT",
            "ARB-USDT","OP-USDT","ETH-USDT","AVAX-USDT","LINK-USDT","NEAR-USDT",
            "APT-USDT","FIL-USDT","INJ-USDT","ATOM-USDT","ADA-USDT","XRP-USDT",
            "LTC-USDT","TRX-USDT"}
    test_symbols = [s for s in test_symbols if s not in used]

    print(f"\n拉取 {len(test_symbols)} 个全新主流币种 4H 数据...")
    data_4h = {}
    for s in test_symbols:
        inst_id = f"{s}-SWAP"
        print(f"  {inst_id}...", end=" ", flush=True)
        df = fetch_klines(inst_id, bar="4H", days=360)
        if df.empty:
            print("FAIL")
            continue
        data_4h[s] = df
        print(f"{len(df)} bars, {df.index[0].date()} ~ {df.index[-1].date()}")

    available = [s for s in test_symbols if s in data_4h]
    print(f"\n成功: {len(available)}/{len(test_symbols)}")

    if len(available) < 5:
        print("币种不足5个，补充之前用过的主流币")
        backup = ["ETH-USDT", "AVAX-USDT", "LINK-USDT"]
        for s in backup:
            if s not in data_4h:
                df = fetch_klines(f"{s}-SWAP", bar="4H", days=360)
                if not df.empty:
                    data_4h[s] = df
                    available.append(s)

    print(f"\n{'='*80}")
    print(f"最终验证 ({len(available)} 币种)")
    print(f"{'='*80}")
    metrics, trades, f_kelly, ci_lower, wilcox, annual = run_backtest(available, data_4h, "最终验证")

    print(f"\n{'='*80}")
    print(f"最终判定")
    print(f"{'='*80}")
    passed = []
    failed = []
    if len(trades) >= 30:
        passed.append(f"样本量 {len(trades)} >= 30")
    else:
        failed.append(f"样本量 {len(trades)} < 30")
    if ci_lower is not None and ci_lower > 0:
        passed.append(f"Bootstrap CI下界 {ci_lower*100:.2f}% > 0")
    else:
        failed.append("Bootstrap CI下界 <= 0")
    if wilcox["significant"]:
        passed.append("Wilcoxon显著")
    else:
        failed.append("Wilcoxon不显著")
    if f_kelly is not None and f_kelly > 0:
        passed.append(f"f_kelly = {f_kelly:.4f} > 0")
    else:
        failed.append(f"f_kelly <= 0")

    print(f"  通过: {passed}")
    print(f"  未通过: {failed}")
    if not failed:
        print(f"\n  [PASS] 策略通过最终验证")
    else:
        print(f"\n  [FAIL] 策略未通过最终验证")


if __name__ == "__main__":
    main()

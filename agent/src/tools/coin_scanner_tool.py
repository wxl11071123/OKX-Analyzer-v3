"""OKX 永续合约程序化选币工具 —— 不经过 AI 对话，程序自动执行。

流程：拉全市场行情 → 流动性过滤 → 拉4H K线 → TSMOM+Hurst → ATR+ADX → 资金费率 → 输出候选
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np
import pandas as pd

from src.agent.tools import BaseTool
from src.indicators.ta import compute_adx, compute_atr, compute_hurst

logger = logging.getLogger(__name__)
RELAY = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")
OKX_BASE = f"{RELAY}/api/v5"

MIN_VOL_24H_USDT = 10_000_000
MAX_SPREAD_PCT = 0.3
TSMOM_LOOKBACK = 120
HURST_WINDOW = 200
HURST_THRESHOLD = 0.55
KLINE_BARS = 300
KLINE_INTERVAL = "4H"
ADX_PERIOD = 14
ATR_PERIOD = 14
MAX_CONCURRENT = 6
FUNDING_LONG_DANGER = 0.001
FUNDING_SHORT_DANGER = -0.0005


class CoinScannerTool(BaseTool):
    """程序化全市场选币——五层漏斗筛选，纯数据驱动，不经过 AI 对话。"""

    name = "coin_scanner"
    repeatable = True
    is_readonly = True
    description = (
        "程序化扫描 OKX 全部 USDT 永续合约，按五层漏斗筛选可交易标的。"
        "五层：流动性过滤 → TSMOM趋势+Hurst → ATR/ADX信号质量 → 资金费率确认 → 风控计算。"
        "返回候选列表（JSON + Markdown），供 AI 评估非技术面因素（代币解锁/黑客风险等）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "min_vol_24h": {
                "type": "number",
                "description": "24h 最小成交额 (USDT)，默认 10,000,000",
                "default": MIN_VOL_24H_USDT,
            },
            "hurst_threshold": {
                "type": "number",
                "description": "Hurst 阈值，默认 0.55",
                "default": HURST_THRESHOLD,
            },
            "top_n": {
                "type": "integer",
                "description": "最多返回 N 个候选，默认 20",
                "default": 20,
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        min_vol = kwargs.get("min_vol_24h", MIN_VOL_24H_USDT)
        hurst_threshold = kwargs.get("hurst_threshold", HURST_THRESHOLD)
        top_n = kwargs.get("top_n", 20)

        try:
            start = time.monotonic()

            # ===== 第一层：流动性过滤 =====
            logger.info("第一层: 拉取全市场 SWAP 行情...")
            tickers_df = _fetch_all_swap_tickers()
            if tickers_df.empty:
                return _error("无法获取行情数据")

            total = len(tickers_df)
            tickers_df = _filter_liquidity(tickers_df, min_vol)
            n1 = len(tickers_df)
            logger.info("第一层通过: %d / %d", n1, total)

            if tickers_df.empty:
                return _ok({"scanned": total, "layer1_pass": 0, "candidates": []})

            # ===== 第二层：并发拉 K 线 + TSMOM + Hurst =====
            logger.info("第二层: 并发拉取 4H K线 + TSMOM + Hurst (max_workers=%d)...", MAX_CONCURRENT)
            symbols = [row["instId"] for _, row in tickers_df.iterrows()]
            signal_results: list[dict] = []
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
                futures = {
                    pool.submit(_process_single_symbol, sym, hurst_threshold): sym
                    for sym in symbols
                }
                for fut in as_completed(futures):
                    try:
                        result = fut.result()
                        if result:
                            signal_results.append(result)
                    except Exception:
                        logger.debug("%s: 处理失败", futures[fut], exc_info=True)

            n2 = len(signal_results)
            n2_long = sum(1 for r in signal_results if r["direction"] == "long")
            n2_short = sum(1 for r in signal_results if r["direction"] == "short")
            logger.info("第二层通过: %d (做多 %d / 做空 %d)", n2, n2_long, n2_short)

            if not signal_results:
                return _ok({"scanned": total, "layer1_pass": n1, "layer2_pass": 0, "candidates": []})

            # ===== 第三层：信号质量分级 =====
            for r in signal_results:
                h = r["hurst"]
                t = r["tsmom_pct"]
                a = r["adx"]
                if abs(t) > 5 and h > 0.60 and a > 25:
                    r["signal_quality"] = "green"
                elif abs(t) > 2 and h > 0.55:
                    r["signal_quality"] = "yellow"
                elif abs(t) > 0:
                    r["signal_quality"] = "blue"
                else:
                    r["signal_quality"] = "red"

            # ===== 第四层：并查资金费率 =====
            logger.info("第四层: 并发查询资金费率...")
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
                futures = {pool.submit(_fetch_funding_rate, r["symbol"]): i for i, r in enumerate(signal_results)}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    r = signal_results[idx]
                    try:
                        rate = fut.result()
                        r["funding_rate"] = round(rate["rate_8h"] * 100, 4)
                        r["funding_annual_pct"] = round(rate["annualized"], 2)
                        if r["direction"] == "long" and rate["rate_8h"] > FUNDING_LONG_DANGER:
                            r["funding_warn"] = "多头拥挤"
                        elif r["direction"] == "short" and rate["rate_8h"] < FUNDING_SHORT_DANGER:
                            r["funding_warn"] = "空头拥挤"
                        else:
                            r["funding_warn"] = ""
                    except Exception:
                        r["funding_rate"] = 0.0
                        r["funding_annual_pct"] = 0.0
                        r["funding_warn"] = "查询失败"

            # ===== 第五层：风控计算 =====
            for r in signal_results:
                price = r["last_price"]
                atr = r["atr"]
                if r["direction"] == "long":
                    r["stop_loss"] = round(price - 2 * atr, 6) if atr > 0 else round(price * 0.97, 6)
                    r["take_profit"] = round(price + 3 * atr, 6) if atr > 0 else round(price * 1.05, 6)
                else:
                    r["stop_loss"] = round(price + 2 * atr, 6) if atr > 0 else round(price * 1.03, 6)
                    r["take_profit"] = round(price - 3 * atr, 6) if atr > 0 else round(price * 0.95, 6)

            # 排序：Hurst 高 + TSMOM 幅度大 优先
            signal_results.sort(key=lambda r: (abs(r["hurst"]), abs(r["tsmom_pct"])), reverse=True)

            # 去重：同一币种只保留信号最强方向
            seen: set[str] = set()
            deduped: list[dict] = []
            for r in signal_results:
                base = r["symbol"].split("-")[0]
                if base not in seen:
                    seen.add(base)
                    deduped.append(r)

            candidates = deduped[:top_n]
            n_final = len(candidates)

            # 淘汰原因汇总（资金费率）
            eliminated = []
            for r in deduped[top_n:]:
                if r.get("funding_warn"):
                    eliminated.append({
                        "symbol": r["symbol"],
                        "reason": f"资金费率: {r['funding_warn']}",
                        "funding_rate": r["funding_rate"],
                    })

            elapsed = time.monotonic() - start
            logger.info("选币完成: 扫描%d → L1:%d → L2:%d → 最终:%d, 耗时%.1fs", total, n1, n2, n_final, elapsed)

            return _ok({
                "scanned": total,
                "layer1_pass": n1,
                "layer2_pass": n2,
                "final_candidates": n_final,
                "elapsed_sec": round(elapsed, 1),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "candidates": candidates,
                "eliminated": eliminated[:20],
                "markdown_report": _build_markdown(total, n1, n2, n_final, candidates, eliminated[:20]),
            })

        except Exception as e:
            logger.exception("选币扫描失败")
            return _error(str(e))


def _fetch_all_swap_tickers() -> pd.DataFrame:
    """拉取所有 USDT-SWAP 行情（带 429 重试）。"""
    resp = _http_get_with_retry(
        f"{OKX_BASE}/market/tickers",
        {"instType": "SWAP"},
        timeout=20,
    )
    data = resp.json()
    if data.get("code") != "0":
        logger.error("行情拉取失败: %s", data.get("msg"))
        return pd.DataFrame()

    records = []
    for t in data.get("data", []):
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        last = float(t.get("last") or 0)
        bid = float(t.get("bidPx") or 0)
        ask = float(t.get("askPx") or 0)
        vol_24h = float(t.get("volCcy24h") or t.get("volCcyQuote") or 0)
        spread = (ask - bid) / bid * 100 if bid > 0 else 999
        records.append({
            "instId": inst_id,
            "last": last,
            "vol24h_usdt": vol_24h,
            "spread_pct": spread,
            "bid": bid,
            "ask": ask,
        })
    return pd.DataFrame(records)


def _filter_liquidity(df: pd.DataFrame, min_vol: float) -> pd.DataFrame:
    """流动性过滤。"""
    return df[(df["vol24h_usdt"] > min_vol) & (df["spread_pct"] < MAX_SPREAD_PCT)].copy()


def _process_single_symbol(sym: str, hurst_threshold: float) -> dict | None:
    """单个币种的 K 线拉取 + 指标计算，供线程池并发调用。"""
    try:
        df = _fetch_klines(sym, KLINE_BARS, KLINE_INTERVAL)
        if df is None or len(df) < 200:
            logger.debug("%s: K线不足 (%s)", sym, len(df) if df is not None else "N/A")
            return None

        close = df["close"]
        tsmom_ret = float(close.pct_change(TSMOM_LOOKBACK).iloc[-1])
        if pd.isna(tsmom_ret):
            return None

        hurst_series = compute_hurst(close, HURST_WINDOW)
        hurst_val = float(hurst_series.iloc[-1])
        if pd.isna(hurst_val) or hurst_val <= hurst_threshold:
            return None

        direction = "long" if tsmom_ret > 0 else "short"
        if tsmom_ret == 0:
            return None

        atr_series = compute_atr(df["high"], df["low"], close, period=ATR_PERIOD)
        adx_df = compute_adx(df["high"], df["low"], close, period=ADX_PERIOD)
        atr_val = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
        adx_val = float(adx_df["adx"].iloc[-1]) if not adx_df.empty else 0.0

        return {
            "symbol": sym,
            "direction": direction,
            "tsmom_pct": round(tsmom_ret * 100, 2),
            "hurst": round(hurst_val, 4),
            "adx": round(adx_val, 2),
            "atr": round(atr_val, 6),
            "last_price": float(close.iloc[-1]),
        }
    except Exception:
        logger.debug("%s: 计算失败", sym, exc_info=True)
        return None


def _http_get_with_retry(url: str, params: dict, timeout: int, retries: int = 2) -> httpx.Response:
    """HTTP GET 带 429 重试。"""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, params=params, timeout=timeout)
            if resp.status_code == 429 and attempt < retries:
                time.sleep(1.0)
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.0)
    raise last_exc  # type: ignore[misc]


def _fetch_klines(symbol: str, limit: int, interval: str) -> pd.DataFrame | None:
    """拉取 OKX K 线数据（带原子 CSV 缓存，4h TTL）。

    缓存策略：
    - 写入临时文件后原子 rename，避免写入中断导致坏文件
    - 4h TTL（一根新 K 线的时间），超时自动重拉
    - 读取失败自动删缓存 + 重拉
    """
    import hashlib
    cache_dir = os.path.join(os.path.expanduser("~"), ".vibe-trading", "coin_cache")
    os.makedirs(cache_dir, exist_ok=True)
    safe = symbol.replace("/", "-").replace(":", "_")
    cache_path = os.path.join(cache_dir, f"{safe}_{interval}_{limit}.csv")
    cache_ttl = 14400  # 4 小时（4H K 线一根的时间）

    # 检查缓存
    if os.path.exists(cache_path):
        mtime = os.path.getmtime(cache_path)
        if time.time() - mtime < cache_ttl:
            try:
                df = pd.read_csv(cache_path)
                if "close" in df.columns and len(df) >= limit * 0.9:
                    return df
            except Exception:
                pass
        # 缓存过期或损坏，删除
        try:
            os.remove(cache_path)
        except OSError:
            pass

    # 拉取 OKX
    try:
        resp = _http_get_with_retry(
            f"{OKX_BASE}/market/candles",
            {"instId": symbol, "bar": interval, "limit": str(limit)},
            timeout=20,
        )
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        rows = data["data"]
        df = pd.DataFrame(
            rows,
            columns=["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_ccy_quote", "confirm"],
        )
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df.sort_values("ts").reset_index(drop=True)

        # 原子写入：先写临时文件，再 rename
        tmp_path = cache_path + ".tmp"
        df.to_csv(tmp_path, index=False)
        os.replace(tmp_path, cache_path)
        return df
    except Exception as e:
        logger.debug("%s: K线拉取失败: %s", symbol, e)
        return None


def _fetch_funding_rate(symbol: str) -> dict:
    """查询资金费率（带 429 重试）。"""
    resp = _http_get_with_retry(
        f"{OKX_BASE}/public/funding-rate",
        {"instId": symbol},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != "0" or not data.get("data"):
        return {"rate_8h": 0.0, "annualized": 0.0}
    item = data["data"][0]
    rate = float(item.get("fundingRate") or 0)
    return {"rate_8h": rate, "annualized": rate * 3 * 365 * 100}


def _build_markdown(
    total: int, n1: int, n2: int, n4: int,
    candidates: list[dict], eliminated: list[dict],
) -> str:
    """生成 Markdown 选币报告。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"## 选币扫描报告 - {today}",
        "",
        "### 扫描概要",
        f"- 扫描标的：{total} 个永续合约",
        f"- 第一层(流动性)通过：{n1}",
        f"- 第二层(TSMOM+Hurst)通过：{n2}",
        f"- 最终推荐：{n4}",
        "",
    ]

    green = [r for r in candidates if r["signal_quality"] == "green"]
    yellow = [r for r in candidates if r["signal_quality"] == "yellow"]
    blue = [r for r in candidates if r["signal_quality"] == "blue"]

    if green:
        lines.append("### 🟢 强信号推荐")
        lines.append("| 币种 | 方向 | 现价 | TSMOM% | Hurst | ADX | 止损 | 止盈 | 费率(年化) |")
        lines.append("|------|------|------|--------|-------|-----|------|------|-----------|")
        for r in green:
            d_emoji = "📈" if r["direction"] == "long" else "📉"
            lines.append(
                f"| {r['symbol']} | {d_emoji} {r['direction']} | {r['last_price']:.4f} | "
                f"{r['tsmom_pct']:+.1f}% | {r['hurst']:.3f} | {r['adx']:.1f} | "
                f"{r['stop_loss']:.4f} | {r['take_profit']:.4f} | "
                f"{r.get('funding_annual_pct', 0):.1f}% |"
            )
        lines.append("")

    if yellow:
        lines.append("### 🟡 标准信号")
        lines.append("| 币种 | 方向 | 现价 | TSMOM% | Hurst | ADX | 费率(年化) |")
        lines.append("|------|------|------|--------|-------|-----|-----------|")
        for r in yellow:
            d_emoji = "📈" if r["direction"] == "long" else "📉"
            warn = f" ⚠️{r['funding_warn']}" if r.get("funding_warn") else ""
            lines.append(
                f"| {r['symbol']} | {d_emoji} {r['direction']} | {r['last_price']:.4f} | "
                f"{r['tsmom_pct']:+.1f}% | {r['hurst']:.3f} | {r['adx']:.1f} | "
                f"{r.get('funding_annual_pct', 0):.1f}%{warn} |"
            )
        lines.append("")

    if blue:
        lines.append("### 🔵 弱信号（观察）")
        lines.append("| 币种 | 方向 | TSMOM% | Hurst |")
        lines.append("|------|------|--------|-------|")
        for r in blue:
            lines.append(
                f"| {r['symbol']} | {r['direction']} | {r['tsmom_pct']:+.1f}% | {r['hurst']:.3f} |"
            )
        lines.append("")

    if eliminated:
        lines.append("### ⏸️ 暂不交易（资金费率风险）")
        lines.append("| 币种 | 原因 | 费率 |")
        lines.append("|------|------|------|")
        for r in eliminated[:10]:
            lines.append(f"| {r['symbol']} | {r['reason']} | {r.get('funding_rate', 0):.4f}% |")
        lines.append("")

    return "\n".join(lines)


def _ok(data: dict) -> str:
    return json.dumps({"status": "ok", **data}, ensure_ascii=False, indent=2, default=str)


def _error(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)
"""日报/周报/月报生成器。

从交易日志、持仓数据、Hurst 指数等数据源汇总生成推送报告。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.trade_log.db import get_trade_stats, query_trades

logger = logging.getLogger(__name__)

TZ_BEIJING = timezone(timedelta(hours=8))


def _now_beijing() -> datetime:
    return datetime.now(TZ_BEIJING)


def _today_start() -> int:
    """今天 00:00 北京时间的时间戳。"""
    now = _now_beijing()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _day_start(days_ago: int) -> int:
    """N 天前 00:00 北京时间的时间戳。"""
    now = _now_beijing()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(start.timestamp())


def _fmt_time(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, TZ_BEIJING)
    return dt.strftime("%m-%d %H:%M")


def _fmt_usd(val: float) -> str:
    if val >= 0:
        return f"+{val:.1f}U"
    return f"{val:.1f}U"


def _get_portfolio_summary() -> Dict[str, Any]:
    """获取 OKX 账户摘要。"""
    try:
        from src.tools.portfolio_tool import PortfolioTool
        import json

        raw = PortfolioTool().execute()
        data = json.loads(raw)
        if data.get("status") != "ok":
            return {"equity": 0, "positions": []}
        account = data.get("account", {})
        return {
            "equity": float(account.get("total_equity", 0)),
            "available": float(account.get("available", 0)),
            "positions": data.get("positions", []),
        }
    except Exception:
        logger.warning("获取持仓信息失败", exc_info=True)
        return {"equity": 0, "positions": []}


def _get_hurst_snapshot() -> Dict[str, float]:
    """获取 BTC/ETH 最新 Hurst 指数。"""
    try:
        import os

        import pandas as pd
        import requests

        from src.indicators.ta import compute_hurst

        BASE = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        result: Dict[str, float] = {}

        for sym in symbols:
            try:
                resp = requests.get(
                    f"{BASE}/market/candles",
                    params={"instId": sym, "bar": "4H", "limit": "300"},
                    timeout=15,
                )
                data = resp.json().get("data", [])
                if not data or len(data) < 200:
                    result[sym] = 0.0
                    continue
                df = pd.DataFrame(
                    data, columns=["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_ccy_quote", "confirm"]
                )
                close = pd.to_numeric(df["close"], errors="coerce").dropna().iloc[::-1]
                h = compute_hurst(close, window=200)
                result[sym] = round(float(h.iloc[-1]), 4) if not h.empty else 0.0
            except Exception:
                result[sym] = 0.0
        return result
    except Exception:
        logger.warning("获取 Hurst 指数失败", exc_info=True)
        return {}


def generate_daily_report() -> str:
    """生成日报 Markdown 内容。

    返回可直接推送到飞书的 Markdown 文本。
    """
    now = _now_beijing()
    date_str = now.strftime("%Y-%m-%d")
    today_start = _today_start()
    yesterday_start = _day_start(1)

    # 持仓
    portfolio = _get_portfolio_summary()
    equity = portfolio.get("equity", 0)
    positions = portfolio.get("positions", [])

    # 今日交易
    stats_today = get_trade_stats(start_time=today_start)
    trades_today = query_trades(start_time=today_start, limit=50)

    # 7 日统计
    week_start = _day_start(7)
    stats_7d = get_trade_stats(start_time=week_start)

    # Hurst
    hurst = _get_hurst_snapshot()

    lines = [f"## TSMOM 交易日报 {date_str}", ""]

    # 持仓状态
    lines.append("### 持仓状态")
    if positions:
        for p in positions:
            side_emoji = "📈" if p.get("side") == "long" else "📉"
            lines.append(
                f"- {side_emoji} {p['symbol']} {p['side']} "
                f"entry={p.get('avg_price', 0)} mark={p.get('mark_price', 0)} "
                f"PnL={_fmt_usd(p.get('unrealized_pnl', 0))} ({p.get('pnl_pct', '0%')})"
            )
    else:
        lines.append("- 无持仓")
    lines.append(f"- 账户权益: **{equity:.1f}U**")
    lines.append("")

    # 今日交易
    lines.append("### 今日交易")
    if trades_today:
        closed = [t for t in trades_today if (t.get("pnl") or 0) != 0]
        for t in closed[:10]:
            lines.append(
                f"- {t['symbol']} {t['side']} "
                f"@{t['price']} qty={t['quantity']} "
                f"PnL={_fmt_usd(t.get('pnl', 0))}"
            )
    else:
        lines.append("- 今日无交易")
    lines.append(f"- 今日盈亏: {_fmt_usd(stats_today.get('net_pnl', 0))}")
    lines.append("")

    # 累计统计
    lines.append("### 累计统计")
    total_fills = stats_7d.get("total_fills", 0)
    closed_trades = stats_7d.get("closed_trades", 0)
    win_rate = stats_7d.get("win_rate", 0)
    pnl_7d = stats_7d.get("net_pnl", 0)
    lines.append(f"- 7日成交: {total_fills} 笔")
    lines.append(f"- 7日胜率: {win_rate*100:.0f}% ({stats_7d.get('win_count', 0)}W/{stats_7d.get('loss_count', 0)}L)")
    lines.append(f"- 7日盈亏: {_fmt_usd(pnl_7d)}")
    lines.append("")

    # 策略状态
    lines.append("### 策略状态")
    for sym, h_val in hurst.items():
        status = "🟢 趋势态" if h_val > 0.55 else "🔴 随机游走"
        lines.append(f"- Hurst({sym} 4H): {h_val:.3f} ({status})")
    next_scan = now + timedelta(hours=4 - (now.hour % 4))
    lines.append(f"- 下次选币: {next_scan.strftime('%m-%d %H:%M')}")
    lines.append("")

    return "\n".join(lines)


def generate_weekly_report() -> str:
    """生成周报 Markdown 内容。"""
    now = _now_beijing()
    week_start = _day_start(7)
    date_range = f"{(now - timedelta(days=7)).strftime('%m-%d')} ~ {now.strftime('%m-%d')}"

    stats = get_trade_stats(start_time=week_start)
    trades = query_trades(start_time=week_start, limit=200)

    total_fills = stats.get("total_fills", 0)
    closed_trades = stats.get("closed_trades", 0)
    win_count = stats.get("win_count", 0)
    loss_count = stats.get("loss_count", 0)
    win_rate = stats.get("win_rate", 0)
    total_pnl = stats.get("total_pnl", 0)
    net_pnl = stats.get("net_pnl", 0)
    total_fee = stats.get("total_fee", 0)

    # 盈亏比
    closed = [t for t in trades if (t.get("pnl") or 0) != 0]
    avg_win = sum(t.get("pnl", 0) for t in closed if (t.get("pnl") or 0) > 0) / max(1, win_count)
    avg_loss = sum(abs(t.get("pnl", 0)) for t in closed if (t.get("pnl") or 0) < 0) / max(1, loss_count)
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    # 1/4 Kelly
    p = win_rate if win_rate > 0 else 0.62
    b = profit_factor if profit_factor > 0 else 1.6
    f_kelly = p - (1 - p) / b if b > 0 else 0
    f_quarter = max(0, f_kelly * 0.25)

    portfolio = _get_portfolio_summary()
    equity = portfolio.get("equity", 0)

    lines = [f"## TSMOM 交易周报 {date_range}", ""]

    lines.append("### 本周统计")
    lines.append(f"- 本周交易: {closed_trades} 笔")
    lines.append(f"- 胜率: {win_rate*100:.0f}% ({win_count}W/{loss_count}L)")
    lines.append(f"- 盈亏比: {profit_factor:.1f}")
    lines.append(f"- 总 PnL: {_fmt_usd(total_pnl)}")
    lines.append(f"- 手续费: {total_fee:.2f}U")
    lines.append(f"- 净 PnL: {_fmt_usd(net_pnl)}")
    if equity > 0:
        lines.append(f"- 周收益: {net_pnl/equity*100:+.2f}%")
    lines.append("")

    lines.append("### f_kelly 更新")
    lines.append(f"- 胜率 p = {win_rate:.2f}")
    lines.append(f"- 盈亏比 b = {profit_factor:.1f}")
    lines.append(f"- f_kelly = {f_kelly:.4f}")
    lines.append(f"- 1/4 kelly = {f_quarter:.4f}")
    lines.append(f"- 当前权益: {equity:.1f}U")
    lines.append("")

    hurst = _get_hurst_snapshot()
    lines.append("### 策略状态")
    for sym, h_val in hurst.items():
        status = "🟢 趋势" if h_val > 0.55 else "🔴 随机"
        lines.append(f"- {sym}: Hurst={h_val:.3f} {status}")
    lines.append("")

    return "\n".join(lines)


def generate_monthly_report() -> str:
    """生成月报 Markdown 内容。"""
    now = _now_beijing()
    month_start = _day_start(30)
    date_range = f"{(now - timedelta(days=30)).strftime('%m-%d')} ~ {now.strftime('%m-%d')}"

    stats = get_trade_stats(start_time=month_start)
    trades = query_trades(start_time=month_start, limit=500)

    total_fills = stats.get("total_fills", 0)
    closed_trades = stats.get("closed_trades", 0)
    win_count = stats.get("win_count", 0)
    loss_count = stats.get("loss_count", 0)
    win_rate = stats.get("win_rate", 0)
    total_pnl = stats.get("total_pnl", 0)
    net_pnl = stats.get("net_pnl", 0)
    total_fee = stats.get("total_fee", 0)

    closed = [t for t in trades if (t.get("pnl") or 0) != 0]
    avg_win = sum(t.get("pnl", 0) for t in closed if (t.get("pnl", 0) > 0)) / max(1, win_count)
    avg_loss = sum(abs(t.get("pnl", 0)) for t in closed if (t.get("pnl", 0) < 0)) / max(1, loss_count)
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    # 按币种统计
    symbols: Dict[str, Dict[str, Any]] = {}
    for t in closed:
        sym = t.get("symbol", "").split("-")[0]
        if sym not in symbols:
            symbols[sym] = {"count": 0, "wins": 0, "pnl": 0.0}
        symbols[sym]["count"] += 1
        symbols[sym]["pnl"] += float(t.get("pnl", 0))
        if float(t.get("pnl", 0)) > 0:
            symbols[sym]["wins"] += 1

    portfolio = _get_portfolio_summary()
    equity = portfolio.get("equity", 0)

    lines = [f"## TSMOM 交易月报 {date_range}", ""]

    lines.append("### 本月统计")
    lines.append(f"- 本月交易: {closed_trades} 笔")
    lines.append(f"- 胜率: {win_rate*100:.0f}% ({win_count}W/{loss_count}L)")
    lines.append(f"- 盈亏比: {profit_factor:.1f}")
    lines.append(f"- 总 PnL: {_fmt_usd(total_pnl)}")
    lines.append(f"- 手续费: {total_fee:.2f}U")
    lines.append(f"- 净 PnL: {_fmt_usd(net_pnl)}")
    if equity > 0:
        lines.append(f"- 月收益: {net_pnl/equity*100:+.2f}%")
    lines.append(f"- 当前权益: {equity:.1f}U")
    lines.append("")

    if symbols:
        lines.append("### 按币种统计")
        lines.append("| 币种 | 交易数 | 胜率 | PnL |")
        lines.append("|------|--------|------|-----|")
        for sym, info in sorted(symbols.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = info["wins"] / max(1, info["count"]) * 100
            lines.append(f"| {sym} | {info['count']} | {wr:.0f}% | {_fmt_usd(info['pnl'])} |")
        lines.append("")

    p = win_rate if win_rate > 0 else 0.62
    b = profit_factor if profit_factor > 0 else 1.6
    f_kelly = p - (1 - p) / b if b > 0 else 0
    f_quarter = max(0, f_kelly * 0.25)

    lines.append("### f_kelly 更新")
    lines.append(f"- f_kelly = {f_kelly:.4f}, 1/4 = {f_quarter:.4f}")
    lines.append(f"- 建议仓位: {equity * f_quarter:.1f}U")
    lines.append("")

    return "\n".join(lines)
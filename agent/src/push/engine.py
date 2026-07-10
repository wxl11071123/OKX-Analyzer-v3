"""推送引擎——后台线程，负责价格监控、定时推送、新闻推送。

通过飞书 Webhook 发送消息。配置存储在 ~/.vibe-trading/push_config.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.push.config import load_config
from src.push.feishu_sender import send_feishu_text, _get_feishu_creds
from src.push.translator import translate_articles
from src.news import rss_collector

logger = logging.getLogger(__name__)

# UTC+8 = 北京时间
TZ_BEIJING = timezone(__import__("datetime").timedelta(hours=8))


class PushEngine:
    """后台推送引擎，单例。"""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_price_alert: dict[str, float] = {}  # symbol → last alert time
        self._last_hourly: float = 0
        self._last_news_sent: dict[str, bool] = {}  # "HH:MM" → already sent today
        self._last_trade_sync: float = 0  # last trade log sync time

    def start(self):
        if self._running:
            return
        config = load_config()
        if not config.get("enabled"):
            logger.info("推送未启用，跳过")
            return
        app_id, secret, open_id, chat_id = _get_feishu_creds()
        if not app_id or (not open_id and not chat_id):
            logger.warning("飞书推送凭证未配置，推送不会发送")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("推送引擎已启动")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                config = load_config()
                if config.get("enabled"):
                    self._check_and_send(config)
            except Exception:
                logger.exception("推送检查异常")
            time.sleep(60)  # 每分钟检查一次

    def _check_and_send(self, config: dict):
        now = datetime.now(TZ_BEIJING)
        today_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        # 0. 自动同步交易日志（每 5 分钟一次兜底）
        if self._running and time.time() - self._last_trade_sync > 300:
            self._last_trade_sync = time.time()
            self._sync_trade_logs()

        # 1. 价格预警
        if config.get("price_alerts", {}).get("enabled"):
            self._check_price_alerts(config, now)

        # 2. 每小时推送 (整点前后 2 分钟内触发)
        if config.get("hourly_push", {}).get("enabled"):
            if now.minute <= 2 and time.time() - self._last_hourly > 3000:
                self._last_hourly = time.time()
                self._send_hourly_summary(config)

        # 3. 新闻推送 (每天指定时间)
        if config.get("news_push", {}).get("enabled"):
            for push_time in config["news_push"].get("times", ["08:00", "20:00"]):
                key = f"{today_str}_{push_time}"
                if time_str == push_time and not self._last_news_sent.get(key):
                    self._last_news_sent[key] = True
                    self._send_news_digest(config)

            # 清理昨天的记录
            for k in list(self._last_news_sent.keys()):
                if not k.startswith(today_str):
                    del self._last_news_sent[k]

    def _check_price_alerts(self, config: dict, now: datetime):
        symbols = config.get("symbols", ["BTC-USDT", "ETH-USDT", "SOL-USDT"])
        threshold = config.get("price_alerts", {}).get("threshold_percent", 5.0)

        for symbol in symbols:
            try:
                ticker = self._fetch_ticker(symbol)
                if not ticker:
                    continue

                change_pct = ticker.get("change_percent", 0)
                last_price = ticker.get("last", 0)

                if abs(change_pct) >= threshold:
                    # 避免重复推送（同一币种 30 分钟内不重复）
                    last_alert = self._last_price_alert.get(symbol, 0)
                    if now.timestamp() - last_alert < 1800:
                        continue

                    self._last_price_alert[symbol] = now.timestamp()
                    direction = "📈" if change_pct > 0 else "📉"
                    text = (
                        f"{direction} **{symbol}** 价格预警\n"
                        f"当前价格: ${last_price:,.2f}\n"
                        f"24h 涨跌幅: {change_pct:+.2f}%\n"
                        f"时间: {now.strftime('%H:%M')}"
                    )
                    send_feishu_text(text)
                    logger.info(f"价格预警已发送: {symbol} {change_pct:+.2f}%")
            except Exception:
                logger.debug(f"检查 {symbol} 价格失败")

    def _send_hourly_summary(self, config: dict):
        symbols = config.get("symbols", ["BTC-USDT", "ETH-USDT", "SOL-USDT"])
        lines = ["**每小时行情快报**\n"]
        now = datetime.now(TZ_BEIJING)

        for symbol in symbols:
            try:
                ticker = self._fetch_ticker(symbol)
                if ticker:
                    price = ticker.get("last", 0)
                    change = ticker.get("change_percent", 0)
                    arrow = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
                    lines.append(f"{arrow} {symbol}: ${price:,.2f} ({change:+.2f}%)")
            except Exception:
                lines.append(f"⚪ {symbol}: 获取失败")

        lines.append(f"\n_{now.strftime('%Y-%m-%d %H:%M')} 更新_")
        send_feishu_text("\n".join(lines))

    def _send_news_digest(self, config: dict):
        try:
            rss_collector.fetch_all_feeds()
            articles = rss_collector.query_news(limit=10)

            if not articles:
                return

            # 翻译
            articles = translate_articles(articles)

            now = datetime.now(TZ_BEIJING)
            lines = [f"**📰 加密货币新闻速递** ({now.strftime('%H:%M')})\n"]

            for i, a in enumerate(articles[:8]):
                title = a.get("title_zh") or a.get("title", "")
                source = a.get("source", "")
                lines.append(f"{i+1}. [{source}] {title[:80]}")

            send_feishu_text("\n".join(lines))
            logger.info(f"新闻推送已发送: {len(articles[:8])} 条")
        except Exception:
            logger.exception("新闻推送失败")

    @staticmethod
    def _fetch_ticker(symbol: str) -> dict | None:
        try:
            resp = httpx.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={symbol}",
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == "0" and data.get("data"):
                item = data["data"][0]
                last = float(item["last"])
                open24h = float(item["open24h"])
                change = last - open24h
                change_pct = (change / open24h * 100) if open24h else 0
                return {
                    "last": last,
                    "high24h": float(item["high24h"]),
                    "low24h": float(item["low24h"]),
                    "change_percent": change_pct,
                    "volume24h": float(item["vol24h"]),
                }
        except Exception:
            pass
        return None


    @staticmethod
    def _sync_trade_logs():
        """自动同步 OKX 成交记录到本地数据库。"""
        try:
            from src.trade_log.okx_client import OKXFillsClient
            from src.trade_log import db as trade_db

            client = OKXFillsClient()
            if not client.is_configured():
                return

            trade_db.init_db()
            for inst_type in ("SPOT", "SWAP"):
                fills = client.fetch_all_history(inst_type=inst_type)
                if fills:
                    n = trade_db.insert_trades(fills)
                    if n > 0:
                        logger.info(f"自动同步 {inst_type} 成交记录: {n} 条")
        except Exception:
            pass


# 全局单例
_engine = PushEngine()


def start_push_engine():
    _engine.start()


def stop_push_engine():
    _engine.stop()

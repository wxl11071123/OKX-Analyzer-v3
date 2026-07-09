"""RSS 新闻采集器 —— 定时轮询加密货币新闻源，缓存到 SQLite。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser

# 6 个加密货币 RSS 源（已验证可用）
RSS_FEEDS = [
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss",
    },
    {
        "name": "CoinTelegraph",
        "url": "https://cointelegraph.com/rss",
    },
    {
        "name": "CryptoSlate",
        "url": "https://cryptoslate.com/feed/",
    },
    {
        "name": "Decrypt",
        "url": "https://decrypt.co/feed",
    },
    {
        "name": "The Block",
        "url": "https://www.theblock.co/rss.xml",
    },
    {
        "name": "Bitcoin Magazine",
        "url": "https://bitcoinmagazine.com/feed",
    },
]

MAX_ARTICLES = 500  # 最多缓存 500 条
POLL_INTERVAL = 300  # 每 5 分钟轮询一次


def _db_path() -> Path:
    base = Path.home() / ".vibe-trading"
    base.mkdir(parents=True, exist_ok=True)
    return base / "news_cache.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_hash TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT DEFAULT '',
            source TEXT NOT NULL,
            published_at TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_news_published ON news_cache(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_news_source ON news_cache(source);
    """)
    conn.commit()
    conn.close()


def _hash_article(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode()).hexdigest()


def fetch_all_feeds() -> int:
    """拉取所有 RSS 源的新文章，返回新增数量。"""
    init_db()
    conn = _get_conn()
    now = int(datetime.now(timezone.utc).timestamp())
    total_new = 0

    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception:
            continue

        for entry in feed.entries[:20]:  # 每个源取前 20 条
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            summary = entry.get("summary", "") or entry.get("description", "")
            if hasattr(summary, "encode"):
                pass
            # 清理 HTML 标签（简单处理）
            import re
            summary = re.sub(r"<[^>]+>", "", str(summary))[:500]

            published = entry.get("published", "") or entry.get("updated", "")

            article_hash = _hash_article(title, link)

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO news_cache
                       (article_hash, title, link, summary, source, published_at, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (article_hash, title, link, summary, feed_info["name"], published, now),
                )
                if conn.total_changes > 0:
                    total_new += 1
            except sqlite3.IntegrityError:
                pass

    # 清理旧文章（只保留最近 MAX_ARTICLES 条）
    conn.execute(
        "DELETE FROM news_cache WHERE id NOT IN (SELECT id FROM news_cache ORDER BY fetched_at DESC LIMIT ?)",
        (MAX_ARTICLES,),
    )
    conn.commit()
    conn.close()
    return total_new


def query_news(
    source: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """查询缓存的新闻文章。"""
    init_db()
    conn = _get_conn()

    sql = "SELECT * FROM news_cache WHERE 1=1"
    params: list[Any] = []

    if source:
        sql += " AND source = ?"
        params.append(source)
    if keyword:
        sql += " AND (title LIKE ? OR summary LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw])

    sql += " ORDER BY fetched_at DESC LIMIT ?"
    params.append(min(limit, 100))

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def get_sources() -> list[str]:
    """返回所有 RSS 源名称。"""
    return [f["name"] for f in RSS_FEEDS]

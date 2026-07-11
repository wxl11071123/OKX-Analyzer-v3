"""RSS 新闻采集器 -- 通过德国 VPS relay 拉取已抓取的新闻，缓存到本地 SQLite。

relay.py（德国 VPS）后台每 5 分钟抓取 6 个 RSS 源写入 JSON，
本模块通过 HTTP 从 relay /news-feed 端点拉取，避免阿里云直连境外被墙。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# 6 个加密货币 RSS 源（relay 侧抓取，此处仅用于元数据展示）
RSS_FEEDS = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "CryptoSlate", "url": "https://cryptoslate.com/feed/"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed"},
    {"name": "The Block", "url": "https://www.theblock.co/rss.xml"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/feed"},
]

MAX_ARTICLES = 500  # 最多缓存 500 条

RELAY = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")


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
    """从 relay /news-feed 端点拉取新闻 JSON，写入本地 SQLite。

    relay 侧（德国 VPS）已抓取好 RSS，此处只需 HTTP 拉取并入库。
    超时 30 秒，失败返回 0 不阻塞调用方。

    Returns:
        新增文章数量。
    """
    init_db()
    conn = _get_conn()
    total_new = 0

    try:
        resp = httpx.get(f"{RELAY}/news-feed", timeout=30)
        resp.raise_for_status()
        articles = resp.json()
    except Exception:
        # relay 不可用时不阻塞，下次重试
        conn.close()
        return 0

    for article in articles:
        title = article.get("title", "").strip()
        link = article.get("link", "").strip()
        if not title or not link:
            continue

        summary = article.get("summary", "")[:500]
        source = article.get("source", "")
        published = article.get("published_at", "")
        fetched_at = article.get("fetched_at", int(datetime.now(timezone.utc).timestamp()))

        article_hash = article.get("article_hash") or _hash_article(title, link)

        try:
            conn.execute(
                """INSERT OR IGNORE INTO news_cache
                   (article_hash, title, link, summary, source, published_at, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (article_hash, title, link, summary, source, published, fetched_at),
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

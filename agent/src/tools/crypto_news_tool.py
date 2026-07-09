"""加密货币新闻 AI 工具 —— 只读查询本地缓存的 RSS 新闻。

AI 只能从本地 SQLite 数据库读取新闻，不能自己搜索。
新闻来源真实可靠，杜绝 AI 幻觉。
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.news import rss_collector


class CryptoNewsTool(BaseTool):
    """查询缓存的加密货币新闻。"""

    name = "crypto_news"
    description = (
        "查询本地缓存的加密货币新闻。新闻来自 CoinDesk、CoinTelegraph 等可靠 RSS 源，"
        "带真实发布时间。可用于分析市场情绪和宏观影响因素。"
        "每篇文章包含：标题、来源、摘要、发布时间。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "按新闻源过滤，如 CoinDesk、CoinTelegraph。不填则查全部。",
                "enum": rss_collector.get_sources(),
            },
            "keyword": {
                "type": "string",
                "description": "按关键词搜索标题和摘要，如 BTC、美联储、监管。",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "返回条数，默认 10，最多 30。",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            source = str(kwargs.get("source", "")) or None
            keyword = str(kwargs.get("keyword", "")) or None
            limit = min(int(kwargs.get("limit", 10)), 30)

            # 先尝试拉取最新新闻
            try:
                rss_collector.fetch_all_feeds()
            except Exception:
                pass

            articles = rss_collector.query_news(source=source, keyword=keyword, limit=limit)

            result = []
            for a in articles:
                result.append({
                    "title": a["title"],
                    "source": a["source"],
                    "summary": a["summary"][:300] if a["summary"] else "",
                    "published_at": a["published_at"],
                    "link": a["link"],
                })

            return json.dumps({
                "status": "ok",
                "count": len(result),
                "articles": result,
                "sources_available": rss_collector.get_sources(),
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

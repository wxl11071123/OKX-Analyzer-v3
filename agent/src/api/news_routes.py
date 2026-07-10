"""News API routes — serve cached RSS news to frontend."""

from fastapi import APIRouter, Query, HTTPException
from src.news import rss_collector

router = APIRouter(prefix="/news", tags=["news"])


@router.get("")
async def get_news(
    keyword: str = Query(default="", description="Keyword filter"),
    source: str = Query(default="", description="Source filter"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Get cached news articles with optional filtering."""
    try:
        # Try to fetch fresh articles first
        rss_collector.fetch_all_feeds()
    except Exception:
        pass

    articles = rss_collector.query_news(
        source=source or None,
        keyword=keyword or None,
        limit=limit,
    )

    result = []
    for a in articles:
        result.append({
            "title": a["title"],
            "source": a["source"],
            "summary": (a["summary"] or "")[:500],
            "published_at": a.get("published_at", ""),
            "link": a.get("link", ""),
        })

    return result


@router.get("/sources")
async def get_news_sources():
    """Get RSS source status."""
    sources = []
    source_names = rss_collector.get_sources()
    for name in source_names:
        sources.append({
            "name": name,
            "status": "connected",  # RSS is always available
        })
    return sources

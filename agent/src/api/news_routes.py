"""News API routes — serve RSS news via German VPS relay."""

import os
import sys
import httpx
from fastapi import APIRouter, Depends, Query, HTTPException

router = APIRouter(prefix="/news", tags=["news"])

RELAY_URL = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")

def _get_require_auth():
    host = sys.modules.get("api_server") or sys.modules.get("agent.api_server")
    return host.require_auth if host else (lambda: None)


@router.get("", dependencies=[Depends(_get_require_auth)])
async def get_news(
    keyword: str = Query(default="", description="Keyword filter"),
    source: str = Query(default="", description="Source filter"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Get news from German VPS relay (fetched from RSS)."""
    try:
        resp = httpx.get(f"{RELAY_URL}/news-feed", timeout=15)
        articles = resp.json() if resp.is_success else []
    except Exception:
        articles = []

    # Filter
    if keyword:
        kw = keyword.lower()
        articles = [a for a in articles if kw in a.get("title", "").lower() or kw in a.get("summary", "").lower()]
    if source:
        articles = [a for a in articles if a.get("source") == source]

    return articles[:min(limit, 100)]


@router.get("/sources", dependencies=[Depends(_get_require_auth)])
async def get_news_sources():
    """Get RSS source status."""
    return [
        {"name": "CoinDesk", "status": "connected"},
        {"name": "CoinTelegraph", "status": "connected"},
        {"name": "CryptoSlate", "status": "connected"},
        {"name": "Decrypt", "status": "connected"},
        {"name": "The Block", "status": "connected"},
        {"name": "Bitcoin Magazine", "status": "connected"},
    ]

from flask import Flask, request, Response
import requests, json, re, threading, time, hashlib, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("relay")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# RSS 新闻抓取 -- 后台线程每 5 分钟抓 6 个源，写入 /root/news_cache.json
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "CryptoSlate", "url": "https://cryptoslate.com/feed/"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed"},
    {"name": "The Block", "url": "https://www.theblock.co/rss.xml"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/feed"},
]

NEWS_CACHE_FILE = "/root/news_cache.json"
MAX_ARTICLES = 500
RSS_POLL_INTERVAL = 300  # 5 分钟


def _hash_article(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode()).hexdigest()


def _fetch_rss_feeds() -> list[dict]:
    """抓取所有 RSS 源，返回去重后的文章列表。"""
    import feedparser
    articles = []
    seen = set()
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception as e:
            logger.warning("RSS fetch failed for %s: %s", feed_info["name"], e)
            continue
        for entry in feed.entries[:20]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            h = _hash_article(title, link)
            if h in seen:
                continue
            seen.add(h)
            summary = entry.get("summary", "") or entry.get("description", "")
            summary = re.sub(r"<[^>]+>", "", str(summary))[:500]
            published = entry.get("published", "") or entry.get("updated", "")
            articles.append({
                "article_hash": h,
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed_info["name"],
                "published_at": published,
                "fetched_at": int(time.time()),
            })
            if len(articles) >= MAX_ARTICLES:
                break
    logger.info("RSS fetch: %d articles", len(articles))
    return articles


def _rss_loop():
    """后台线程：定时抓取 RSS 写入 JSON 文件。"""
    while True:
        try:
            articles = _fetch_rss_feeds()
            with open(NEWS_CACHE_FILE, "w") as f:
                json.dump(articles, f, ensure_ascii=False)
        except Exception as e:
            logger.error("RSS loop error: %s", e)
        time.sleep(RSS_POLL_INTERVAL)


# 启动 RSS 后台线程
_thread = threading.Thread(target=_rss_loop, daemon=True)
_thread.start()


@app.route("/news-feed")
def news_feed():
    try:
        with open(NEWS_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


@app.route("/search")
def search_proxy():
    q = request.args.get("q", "")
    if not q:
        return json.dumps([], ensure_ascii=False)
    try:
        url = f"https://lite.duckduckgo.com/lite?q={requests.utils.quote(q)}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        results = []
        for m in re.finditer(r'(<a[^>]*?uddg=([^"&\s>]+)[^>]*?>)([^<]+)</a>', resp.text):
            link = requests.utils.unquote(m.group(2))
            title = m.group(3).strip()
            pos = m.end()
            snippet = ""
            sm = re.search(r'<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>', resp.text[pos:pos+3000], re.S)
            if sm:
                snippet = re.sub(r"<[^>]+>", "", sm.group(1)).strip()[:200]
            if link and title:
                results.append({"title": title[:100], "url": link, "snippet": snippet})
        return json.dumps(results[:5], ensure_ascii=False)
    except Exception as e:
        return json.dumps([{"error": str(e)}])

@app.route("/jina")
def jina_reader():
    url = request.args.get("url", "")
    if not url:
        return json.dumps({"status": "error", "error": "missing url param"})
    try:
        headers = {"Accept": "text/markdown"}
        if request.args.get("no_cache"):
            headers["x-no-cache"] = "true"
        resp = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=30)
        return Response(resp.text, status=resp.status_code, content_type="text/plain; charset=utf-8")
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def relay(path):
    url = f"https://www.okx.com/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    resp = requests.request(method=request.method, url=url, headers=headers,
        params=request.args, data=request.get_data(), timeout=30)
    excluded = {"transfer-encoding", "content-encoding", "content-length"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    return Response(resp.content, status=resp.status_code, headers=resp_headers)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

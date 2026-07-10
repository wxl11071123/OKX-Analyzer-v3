from flask import Flask, request, Response
import requests, json, re

app = Flask(__name__)

@app.route("/news-feed")
def news_feed():
    try:
        with open("/root/news_cache.json") as f:
            return json.load(f)
    except:
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
                snippet = re.sub(r'<[^>]+>', '', sm.group(1)).strip()[:200]
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
        # Return raw text/markdown, not JSON
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

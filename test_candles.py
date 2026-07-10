import httpx, json

RELAY = "http://127.0.0.1:8080"

# Test HYPE daily candles
url = f"{RELAY}/api/v5/market/candles?instId=HYPE-USDT&bar=1D&limit=3"
r = httpx.get(url, timeout=15)
d = r.json()
if d.get("code") == "0":
    print("HYPE candles OK. Sample:")
    for bar in d["data"][:3]:
        print(f"  ts={bar[0]} o={bar[1]} h={bar[2]} l={bar[3]} c={bar[4]}")
else:
    print("Error:", d)

# Now test 300 limit
url2 = f"{RELAY}/api/v5/market/candles?instId=HYPE-USDT&bar=1D&limit=300"
r2 = httpx.get(url2, timeout=15)
d2 = r2.json()
if d2.get("code") == "0":
    data = d2["data"]
    timestamps = [int(x[0]) for x in data]
    from datetime import datetime
    first = datetime.utcfromtimestamp(min(timestamps)/1000).strftime('%Y-%m-%d')
    last = datetime.utcfromtimestamp(max(timestamps)/1000).strftime('%Y-%m-%d')
    print(f"\n300 candles: count={len(data)}, first={first}, last={last}")
else:
    print("Error 300:", d2)

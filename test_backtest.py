import os, sys
sys.path.insert(0, '/root/OKX-Analyzer-v3/agent')
from dotenv import load_dotenv; load_dotenv('/root/OKX-Analyzer-v3/agent/.env')

from src.market_data import fetch_market_data

result = fetch_market_data(
    codes=["BTC-USDT"],
    start_date="2026-06-01",
    end_date="2026-07-10",
    interval="1D",
    max_rows=0  # no limit
)

if "_unresolved" in result:
    print("UNRESOLVED:", result["_unresolved"])
else:
    for sym, data in result.items():
        if isinstance(data, list):
            print(f"{sym}: {len(data)} bars, first={data[0]}, last={data[-1]}")
        elif isinstance(data, dict) and "data" in data:
            print(f"{sym}: {len(data['data'])} bars (sampled from {data.get('rows','?')})")
        else:
            print(f"{sym}: {data}")

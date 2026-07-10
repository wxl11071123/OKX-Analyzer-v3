import os, sys
sys.path.insert(0, '/root/OKX-Analyzer-v3/agent')
from dotenv import load_dotenv; load_dotenv('/root/OKX-Analyzer-v3/agent/.env')

# Check if the monkey-patch actually caught the backtest loader
from src.market_data import _patch_okx_loader
_patch_okx_loader()

# Now test if get_market_data returns valid data for ETH
from src.market_data import fetch_market_data
result = fetch_market_data(
    codes=["ETH-USDT"],
    start_date="2026-07-01",
    end_date="2026-07-10",
    interval="1D",
    max_rows=0
)
print("ETH result:", "unresolved=" in str(result) if "_unresolved" in result else f"OK ({len(result)} symbols)")
for k in result:
    if k != "_unresolved":
        print(f"  {k}: {len(result[k])} bars")

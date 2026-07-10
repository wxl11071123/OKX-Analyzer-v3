# Test the OKX loader on the server
import os, sys
# Add source path
sys.path.insert(0, '/root/OKX-Analyzer-v3/agent')
from dotenv import load_dotenv; load_dotenv('/root/OKX-Analyzer-v3/agent/.env')

# Monkey-patch: make the backtest OKX loader use the relay
import backtest.loaders.okx as okx_module
import inspect
src = inspect.getsource(okx_module)
print("www.okx.com refs:", src.count("www.okx.com"))
print("OKX_RELAY:", os.getenv("OKX_RELAY", "NOT SET"))
print("RELAY is set:", "OKX_RELAY" in os.environ)

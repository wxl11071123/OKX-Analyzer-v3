import os, sys
sys.path.insert(0, '/root/OKX-Analyzer-v3/agent')
from dotenv import load_dotenv; load_dotenv('/root/OKX-Analyzer-v3/agent/.env')

# Check what backtest module is used
try:
    import backtest.loaders.registry
    loader = backtest.loaders.registry.get_loader_cls_with_fallback('okx')
    print("Loader:", loader.__name__)
    print("Module:", loader.__module__)
    import inspect
    src = inspect.getsource(loader)
    if 'OKX_RELAY' in src or 'okx_relay' in src or 'relay' in src.lower():
        print("Already uses relay!")
    elif 'www.okx.com' in src:
        print("Direct OKX - BLOCKED in China!")
    else:
        print("Source:", src[:500])
except Exception as e:
    print(f"Error: {e}")
    # Try to find the package
    import subprocess
    r = subprocess.run(['pip', 'show', '-f', 'vibe-trading-backtest'], capture_output=True, text=True)
    print(r.stdout[:500] if r.stdout else 'Package not found')

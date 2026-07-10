from src.tools import build_registry
r = build_registry()
for t in r._tools:
    if 'portfolio' in t.name or 'account' in t.name or 'trading' in t.name:
        print(f"{t.name}: {t.description[:60]}")

import httpx, json

BASE = "http://127.0.0.1:8899"

# Get first trade
trades = httpx.get(f"{BASE}/trade-log?limit=1").json()
tid = trades[0]["trade_id"]
print("BEFORE note:", repr(trades[0].get("note")))
print("BEFORE score:", trades[0].get("discipline_score"))

# Save
r = httpx.patch(f"{BASE}/trade-log/{tid}", json={"note": "TEST123", "discipline_score": 10})
print("SAVE:", r.json())

# Check
trades2 = httpx.get(f"{BASE}/trade-log?limit=1").json()
print("AFTER SAVE note:", repr(trades2[0].get("note")))
print("AFTER SAVE score:", trades2[0].get("discipline_score"))

# Sync
r2 = httpx.post(f"{BASE}/trade-log/sync")
print("SYNC:", r2.json())

# Check after sync
trades3 = httpx.get(f"{BASE}/trade-log?limit=1").json()
print("AFTER SYNC note:", repr(trades3[0].get("note")))
print("AFTER SYNC score:", trades3[0].get("discipline_score"))

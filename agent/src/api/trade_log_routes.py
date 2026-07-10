"""交易日志 API 路由。"""

from fastapi import APIRouter, Depends, HTTPException, Body
import sys

from src.trade_log import db as trade_db
from src.trade_log.okx_client import OKXFillsClient

router = APIRouter(prefix="/trade-log", tags=["trade-log"])

# Late-bind auth dependency from host api_server
def _get_require_auth():
    host = sys.modules.get("api_server") or sys.modules.get("agent.api_server")
    return host.require_auth if host else (lambda: None)


@router.get("", dependencies=[Depends(_get_require_auth)])
async def get_trades(symbol: str = "", inst_type: str = "", limit: int = 50):
    try:
        trade_db.init_db()
        return trade_db.query_trades(
            symbol=symbol or None,
            inst_type=inst_type or None,
            limit=min(limit, 100),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats", dependencies=[Depends(_get_require_auth)])
async def get_stats():
    try:
        trade_db.init_db()
        return trade_db.get_trade_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync", dependencies=[Depends(_get_require_auth)])
async def sync_trades(inst_type: str = ""):
    """手动同步成交记录。inst_type: SPOT 或 SWAP，不传则两者都同步。"""
    client = OKXFillsClient()
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="OKX API 凭证未配置")

    results = {}
    types = [inst_type] if inst_type else ["SPOT", "SWAP"]
    for it in types:
        fills = client.fetch_all_history(inst_type=it)
        trade_db.init_db()
        n = trade_db.insert_trades(fills)
        results[it] = n

    return {"status": "ok", "synced": results}


@router.patch("/{trade_id}", dependencies=[Depends(_get_require_auth)])
async def update_trade(
    trade_id: str,
    note: str = Body(""),
    discipline_score: int = Body(0),
):
    updated = False
    if note:
        updated = trade_db.update_note(trade_id, note) or updated
    if discipline_score:
        updated = trade_db.update_discipline(trade_id, discipline_score) or updated
    if not updated:
        raise HTTPException(status_code=404, detail="交易不存在或无需更新")
    return {"status": "ok"}

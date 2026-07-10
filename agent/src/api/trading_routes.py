"""交易/持仓 API 路由 —— 只读查询 OKX 账户和持仓。"""

from fastapi import APIRouter, HTTPException
from src.trade_log import okx_account

router = APIRouter(prefix="/trading", tags=["trading"])


@router.get("/account")
async def get_account():
    if not okx_account.is_configured():
        raise HTTPException(status_code=400, detail="OKX API 凭证未配置。请在 .env 中设置 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE。")
    try:
        return okx_account.get_account_balance()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/positions")
async def get_positions():
    if not okx_account.is_configured():
        raise HTTPException(status_code=400, detail="OKX API 凭证未配置")
    try:
        return okx_account.get_positions()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

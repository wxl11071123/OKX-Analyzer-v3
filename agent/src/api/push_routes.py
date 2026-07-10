"""推送配置 API 路由。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.push import config as push_config

router = APIRouter(prefix="/push", tags=["push"])


class PushConfigModel(BaseModel):
    enabled: bool = False
    symbols: list[str] = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    price_alerts: dict = {}
    hourly_push: dict = {}
    news_push: dict = {}


@router.get("/config")
async def get_push_config():
    return push_config.load_config()


@router.put("/config")
async def update_push_config(body: PushConfigModel):
    config = body.model_dump()
    push_config.save_config(config)
    return {"status": "ok", "message": "推送配置已保存，重启生效"}

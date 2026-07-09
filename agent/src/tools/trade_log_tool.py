"""交易日志 AI 工具。

AI 可调用此工具来：
- 从 OKX API 同步最新成交记录到本地数据库
- 查询历史交易日志
- 分析交易行为和纪律
- 补充备注和纪律评分
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.agent.tools import BaseTool
from src.trade_log import db
from src.trade_log.okx_client import OKXFillsClient


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class SyncTradeLogTool(BaseTool):
    """从 OKX 同步成交记录到本地数据库。"""

    name = "sync_trade_log"
    description = (
        "从 OKX 交易所拉取成交记录并同步到本地交易日志数据库。"
        "支持按交易类型(SPOT/SWAP)和交易对过滤。"
        "重复记录自动跳过。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "inst_type": {
                "type": "string",
                "enum": ["SPOT", "SWAP"],
                "default": "SPOT",
                "description": "交易类型：SPOT=现货，SWAP=永续合约",
            },
            "inst_id": {
                "type": "string",
                "description": "交易对，如 BTC-USDT。不填则拉取全部。",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = False  # 写入本地 DB

    def execute(self, **kwargs: Any) -> str:
        try:
            client = OKXFillsClient()
            if not client.is_configured():
                return json.dumps({
                    "status": "error",
                    "error": "OKX API 凭证未配置。请在 .env 中设置 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE。",
                }, ensure_ascii=False)

            inst_type = str(kwargs.get("inst_type", "SPOT"))
            inst_id = str(kwargs.get("inst_id", "")) or None

            # 拉取最近 3 个月数据
            fills = client.fetch_all_history(inst_type=inst_type, inst_id=inst_id)
            if not fills:
                return json.dumps({
                    "status": "ok",
                    "synced": 0,
                    "message": f"没有新的 {inst_type} 成交记录。",
                }, ensure_ascii=False)

            db.init_db()
            inserted = db.insert_trades(fills)

            return json.dumps({
                "status": "ok",
                "synced": inserted,
                "total_fetched": len(fills),
                "message": f"已同步 {inserted} 条新的 {inst_type} 成交记录（共拉取 {len(fills)} 条）。",
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class QueryTradeLogTool(BaseTool):
    """查询本地交易日志。"""

    name = "query_trade_log"
    description = (
        "查询本地交易日志数据库。可按交易对、交易类型、时间范围过滤。"
        "返回交易明细列表。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "交易对，如 BTC-USDT。不填则查全部。",
            },
            "inst_type": {
                "type": "string",
                "enum": ["SPOT", "SWAP"],
                "description": "交易类型",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "返回条数，默认 20",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            db.init_db()
            symbol = str(kwargs.get("symbol", "")) or None
            inst_type = str(kwargs.get("inst_type", "")) or None
            limit = int(kwargs.get("limit", 20))

            trades = db.query_trades(
                symbol=symbol,
                inst_type=inst_type,
                limit=min(limit, 100),
            )

            # 精简输出字段，避免 token 浪费
            summary = []
            for t in trades:
                summary.append({
                    "trade_id": t["trade_id"],
                    "symbol": t["symbol"],
                    "side": t["side"],
                    "price": t["price"],
                    "quantity": t["quantity"],
                    "pnl": t["pnl"],
                    "fee": t["fee"],
                    "fill_time": t["fill_time"],
                    "note": t["note"] or "",
                    "discipline_score": t["discipline_score"],
                })

            return json.dumps({
                "status": "ok",
                "count": len(summary),
                "trades": summary,
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class TradeStatsTool(BaseTool):
    """交易统计和分析。"""

    name = "trade_stats"
    description = (
        "获取交易统计摘要：总交易数、胜率、总盈亏、手续费、平均纪律评分。"
        "可按交易对和交易类型过滤。AI 可用此分析交易行为和纪律性。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "交易对，不填则统计全部。",
            },
            "inst_type": {
                "type": "string",
                "enum": ["SPOT", "SWAP"],
                "description": "交易类型",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            db.init_db()
            symbol = str(kwargs.get("symbol", "")) or None
            inst_type = str(kwargs.get("inst_type", "")) or None

            stats = db.get_trade_stats(symbol=symbol, inst_type=inst_type)
            stats["status"] = "ok"

            return json.dumps(stats, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class UpdateTradeNoteTool(BaseTool):
    """为交易添加备注和纪律评分。"""

    name = "update_trade_note"
    description = (
        "为某笔交易添加备注或纪律评分(1-10)。用户手动录入交易心得和纪律自评。"
        "discipline_score: 1=完全违反纪律, 5=一般, 10=严格执行计划。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "trade_id": {
                "type": "string",
                "description": "交易 ID，从 query_trade_log 获取。",
            },
            "note": {
                "type": "string",
                "description": "交易备注/心得。",
            },
            "discipline_score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "纪律自评分数，1-10。",
            },
        },
        "required": ["trade_id"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            db.init_db()
            trade_id = str(kwargs.get("trade_id", ""))
            note = str(kwargs.get("note", ""))
            score = kwargs.get("discipline_score")

            updated = False
            if note:
                updated = db.update_note(trade_id, note) or updated
            if score is not None:
                updated = db.update_discipline(trade_id, int(score)) or updated

            if not updated:
                return json.dumps({
                    "status": "error",
                    "error": f"未找到交易 {trade_id} 或无需更新。",
                }, ensure_ascii=False)

            return json.dumps({
                "status": "ok",
                "message": f"交易 {trade_id} 已更新。",
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

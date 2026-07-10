"""交易日志 SQLite 数据库管理。

数据库位置: ~/.vibe-trading/trade_log.db
使用 WAL 模式以支持 AI 工具并发读取。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    """数据库文件路径。"""
    base = Path.home() / ".vibe-trading"
    base.mkdir(parents=True, exist_ok=True)
    return base / "trade_log.db"


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，启用 WAL 模式。"""
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库表结构。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            inst_type TEXT NOT NULL DEFAULT 'SPOT',
            side TEXT NOT NULL,
            pos_side TEXT DEFAULT '',
            price REAL NOT NULL,
            quantity REAL NOT NULL,
            fee REAL DEFAULT 0,
            fee_currency TEXT DEFAULT '',
            pnl REAL DEFAULT 0,
            exec_type TEXT DEFAULT '',
            fill_time INTEGER NOT NULL,
            ord_id TEXT DEFAULT '',
            note TEXT DEFAULT '',
            discipline_score INTEGER DEFAULT 0,
            tags TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trade_log_symbol ON trade_log(symbol);
        CREATE INDEX IF NOT EXISTS idx_trade_log_fill_time ON trade_log(fill_time);
        CREATE INDEX IF NOT EXISTS idx_trade_log_inst_type ON trade_log(inst_type);
        CREATE INDEX IF NOT EXISTS idx_trade_log_trade_id ON trade_log(trade_id);
    """)
    conn.commit()
    conn.close()


def insert_trades(trades: list[dict[str, Any]]) -> int:
    """批量插入成交记录（重复 trade_id 自动忽略）。

    Returns:
        实际插入的记录数。
    """
    if not trades:
        return 0

    now = int(datetime.now(timezone.utc).timestamp())
    conn = _get_conn()
    inserted = 0

    for t in trades:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO trade_log
                   (trade_id, symbol, inst_type, side, pos_side, price, quantity,
                    fee, fee_currency, pnl, exec_type, fill_time, ord_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t["trade_id"],
                    t["symbol"],
                    t.get("inst_type", "SPOT"),
                    t["side"],
                    t.get("pos_side", ""),
                    t["price"],
                    t["quantity"],
                    t.get("fee", 0),
                    t.get("fee_currency", ""),
                    t.get("pnl", 0),
                    t.get("exec_type", ""),
                    t["fill_time"],
                    t.get("ord_id", ""),
                    now,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    return inserted


def query_trades(
    symbol: str | None = None,
    inst_type: str | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """查询交易日志。

    Args:
        symbol: 按交易对过滤（可选）
        inst_type: SPOT 或 SWAP（可选）
        start_time: 起始时间戳（可选）
        end_time: 结束时间戳（可选）
        limit: 最大返回条数
    """
    conn = _get_conn()
    sql = "SELECT * FROM trade_log WHERE 1=1"
    params: list[Any] = []

    if symbol:
        sql += " AND symbol LIKE ?"
        params.append(f"%{symbol}%")
    if inst_type:
        sql += " AND inst_type = ?"
        params.append(inst_type)
    if start_time:
        sql += " AND fill_time >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND fill_time <= ?"
        params.append(end_time)

    sql += " ORDER BY fill_time DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trade_stats(
    symbol: str | None = None,
    inst_type: str | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> dict[str, Any]:
    """获取交易统计摘要。"""
    conn = _get_conn()
    sql = "SELECT * FROM trade_log WHERE 1=1"
    params: list[Any] = []

    if symbol:
        sql += " AND symbol LIKE ?"
        params.append(f"%{symbol}%")
    if inst_type:
        sql += " AND inst_type = ?"
        params.append(inst_type)
    if start_time:
        sql += " AND fill_time >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND fill_time <= ?"
        params.append(end_time)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        return {
            "total_trades": 0, "win_count": 0, "loss_count": 0,
            "win_rate": 0, "total_pnl": 0, "total_fee": 0,
            "net_pnl": 0, "avg_discipline_score": 0,
        }

    total_pnl = sum(r["pnl"] or 0 for r in rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    losses = sum(1 for r in rows if (r["pnl"] or 0) < 0)
    total_fee = sum(r["fee"] or 0 for r in rows)

    return {
        "total_trades": len(rows),
        "win_count": wins,
        "loss_count": losses,
        "win_rate": round(wins / len(rows), 4) if rows else 0,
        "total_pnl": round(total_pnl, 8),
        "total_fee": round(total_fee, 8),
        "net_pnl": round(total_pnl - abs(total_fee), 8),
        "avg_discipline_score": round(
            sum(r["discipline_score"] or 0 for r in rows) / len(rows), 1
        ) if rows else 0,
    }


def update_note(trade_id: str, note: str) -> bool:
    """更新某笔交易的备注。"""
    conn = _get_conn()
    conn.execute("UPDATE trade_log SET note = ? WHERE trade_id = ?", (note, trade_id))
    conn.commit()
    affected = conn.total_changes
    conn.close()
    return affected > 0


def update_discipline(trade_id: str, score: int) -> bool:
    """更新某笔交易的纪律评分（1-10）。"""
    conn = _get_conn()
    conn.execute(
        "UPDATE trade_log SET discipline_score = ? WHERE trade_id = ?",
        (max(1, min(10, score)), trade_id),
    )
    conn.commit()
    affected = conn.total_changes
    conn.close()
    return affected > 0

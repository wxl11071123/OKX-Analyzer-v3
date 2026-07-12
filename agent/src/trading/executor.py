"""TSMOM 策略交易执行器。

主循环（60s 轮询）：
- 每 4H：检查入场信号（TSMOM + Hurst）
- 每 1H：检查持仓 EMA20 止损
- 每次开/平仓：风控红线检查 + halt 自动触发 + 飞书通知 + 交易日志

状态持久化：~/.vibe-trading/trading_state.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests as req

from src.indicators.ta import compute_ema, compute_hurst
from src.live.halt import halt_flag_set, trip_halt
from src.providers.chat import ChatLLM
from src.push.feishu_sender import send_feishu_card, send_feishu_text
from src.trade_log.db import _get_conn, init_db
from src.trading.connectors.okx.sdk import (
    _sway_auth_headers,
    calc_swap_sz,
    place_swap_order,
    place_swap_stop_order,
    set_swap_leverage,
)
from src.trading.kelly import KellyCalculator

logger = logging.getLogger(__name__)

RELAY = os.getenv("OKX_RELAY", "http://127.0.0.1:8080")
OKX_API = f"{RELAY}/api/v5"
STATE_FILE = Path.home() / ".vibe-trading" / "trading_state.json"
TZ_UTC = timezone.utc

ENTRY_INTERVAL_HOURS = 4
STOP_CHECK_MINUTES = 60

DAILY_LOSS_PCT = 0.06
MAX_CONSECUTIVE_LOSSES = 3


@dataclass
class TradingState:
    """交易引擎持久化状态。"""

    position: dict | None = None
    daily_pnl: float = 0.0
    daily_pnl_date: str = ""
    consecutive_losses: int = 0
    last_signal_at: str = ""


@dataclass
class ExecutorConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    initial_capital: float = 150.0
    max_positions: int = 1
    is_live: bool = False


class TradingExecutor:
    """TSMOM 策略自动化交易执行器。"""

    def __init__(self, config: ExecutorConfig):
        self.cfg = config
        self.kelly = KellyCalculator(lookback=100)
        self.state = self._load_state()
        self._stop_flag = False
        self._last_entry_check = 0.0
        self._last_stop_check = 0.0
        self._last_symbol_refresh = 0.0
        init_db()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self):
        """主循环。"""
        logger.info(
            "TradingExecutor 启动, symbols=%s, capital=%.1f, live=%s",
            self.cfg.symbols, self.cfg.initial_capital, self.cfg.is_live,
        )

        # 启动时优先恢复持仓
        if self.state.position:
            logger.info("检测到持仓状态，与 OKX 对账中...")
            for attempt in range(3):
                if self._reconcile_position():
                    logger.info("持仓恢复成功: %s", self.state.position["symbol"])
                    break
                if attempt < 2:
                    logger.warning("持仓对账失败，5秒后重试 (%d/3)", attempt + 1)
                    time.sleep(5)
            else:
                logger.error("持仓恢复失败，清空本地状态")
                self.state.position = None
                self._save_state()
        else:
            # 本地无持仓但可能 OKX 上有（孤儿仓）
            self._check_orphan_positions()

        self._send_startup_card()

        while not self._stop_flag:
            try:
                if halt_flag_set():
                    logger.warning("halt 已触发，跳过交易循环")
                    time.sleep(60)
                    continue

                now = time.time()
                if self.state.position:
                    self._reconcile_position()
                    self._check_stop_loss()
                    self._check_signal_reversal()
                else:
                    if now - self._last_entry_check >= ENTRY_INTERVAL_HOURS * 3600:
                        self._check_entry_signal()
                        self._last_entry_check = now

                time.sleep(60)
            except Exception:
                logger.exception("主循环异常")
                time.sleep(60)

    def stop(self):
        self._stop_flag = True

    # ------------------------------------------------------------------
    # 持仓对账
    # ------------------------------------------------------------------

    def _check_orphan_positions(self):
        """查询 OKX 是否有引擎不知情的仓位，如有则恢复。"""
        try:
            resp = req.get(
                f"{OKX_API}/account/positions",
                params={"instType": "SWAP"},
                headers=_sway_auth_headers("GET", "/api/v5/account/positions", get_params={"instType": "SWAP"}),
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != "0":
                return
            for p in data.get("data", []):
                pos_qty = int(float(p.get("pos", "0") or 0))
                if pos_qty <= 0:
                    continue
                sym = p["instId"]
                avg_px = float(p.get("avgPx", p.get("markPx", 0)))
                pos_side = p.get("posSide", "net")
                lever = int(float(p.get("lever", "5")))
                # net_mode 下方向从 upl 判断
                upl = float(p.get("upl", 0))
                direction = "long" if upl < 0 else "short"  # net_mode 粗略判断
                logger.warning("发现孤儿仓位: %s qty=%d px=%.4f", sym, pos_qty, avg_px)
                self.state.position = {
                    "symbol": sym, "direction": direction, "pos_side": pos_side,
                    "entry_price": avg_px,
                    "entry_at": datetime.now(TZ_UTC).isoformat(),
                    "size": pos_qty, "leverage": lever,
                    "notional": round(pos_qty * float(p.get("markPx", 0)), 2),
                    "ct_val": 10.0, "stop_order_id": "",
                }
                self._save_state()
                logger.info("孤儿仓位已恢复: %s", sym)
        except Exception:
            logger.exception("孤儿仓位检查失败")

    def _reconcile_position(self) -> bool:
        """与 OKX 对账，确认持仓完整性。返回 True=对账成功。

        规则：
        - API 成功 + 匹配 → 数量以 OKX 为准，返回 True
        - API 成功 + 无匹配 → 仓位已平，清除状态，返回 False
        - API 失败/网络异常 → 保留状态，返回 False（下次重试）
        """
        if not self.state.position:
            return True
        sym = self.state.position["symbol"]
        try:
            resp = req.get(
                f"{OKX_API}/account/positions",
                params={"instId": sym},
                headers=_sway_auth_headers("GET", "/api/v5/account/positions", get_params={"instId": sym}),
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != "0":
                logger.warning("持仓对账API失败: %s", data.get("msg"))
                return False
            positions = data.get("data", [])

            matched = None
            for p in positions:
                if p.get("instId") == sym:
                    matched = p
                    break

            if matched is None:
                logger.warning("持仓对账: %s 在 OKX 已平仓，清除本地状态", sym)
                self.state.position = None
                self._save_state()
                return False

            okx_qty = int(float(matched.get("pos", 0)))
            local_qty = self.state.position.get("size", 0)
            if abs(okx_qty - local_qty) > 1:
                logger.warning("持仓对账: %s 数量不一致 (本地=%d, OKX=%d)，以 OKX 为准", sym, local_qty, okx_qty)
                self.state.position["size"] = okx_qty
                self._save_state()
            # 恢复缺失的 ct_val（旧 state JSON 可能没有）
            if "ct_val" not in self.state.position:
                _, ct, _ = calc_swap_sz(1, sym)
                self.state.position["ct_val"] = ct
                self._save_state()
            return True

        except Exception:
            logger.exception("持仓对账网络异常，保留状态下次重试")
            return False

    # ------------------------------------------------------------------
    # 入场信号
    # ------------------------------------------------------------------

    def _check_entry_signal(self):
        """检查 TSMOM 入场信号。"""
        now = time.time()
        if now - getattr(self, "_last_symbol_refresh", 0) > 86400:
            self._refresh_symbols()
            self._last_symbol_refresh = now

        logger.info("检查入场信号...")
        for sym in self.cfg.symbols:
            try:
                df = self._fetch_klines(sym, 300, "4H")
                if df is None or len(df) < 200:
                    continue
                close = df["close"]
                tsmom_ret = float(close.pct_change(120).iloc[-1])
                if pd.isna(tsmom_ret):
                    continue
                hurst = compute_hurst(close, 200)
                hurst_val = float(hurst.iloc[-1])
                if pd.isna(hurst_val) or hurst_val <= 0.55:
                    continue

                direction = "long" if tsmom_ret > 0 else "short"
                if tsmom_ret == 0:
                    continue

                logger.info(
                    "入场信号: %s %s TSMOM=%.1f%% Hurst=%.3f price=%.4f",
                    sym, direction, tsmom_ret * 100, hurst_val, close.iloc[-1],
                )
                self._open_position({
                    "symbol": sym,
                    "direction": direction,
                    "tsmom_pct": round(tsmom_ret * 100, 2),
                    "hurst": round(hurst_val, 4),
                    "entry_price": float(close.iloc[-1]),
                    "atr": self._calc_atr(df),
                })
                self._last_entry_check = time.time()
                self.state.last_signal_at = datetime.now(TZ_UTC).isoformat()
                return
            except Exception:
                logger.debug("%s 信号检查失败", sym, exc_info=True)

    def _refresh_symbols(self):
        """从选币结果动态更新交易标的列表（每日执行）。

        流程：程序化选前十 → AI 使用工具查新闻/解锁/风险 → 输出白名单。
        AI 失败时 fallback 程序化前五。
        """
        try:
            from src.tools.coin_scanner_tool import CoinScannerTool

            cs = CoinScannerTool()
            result = cs.execute(top_n=10)
            data = json.loads(result)
            candidates = data.get("candidates", [])
            if not candidates:
                logger.warning("选币刷新: 无候选，清空列表禁止交易")
                self.cfg.symbols = []
                return

            logger.info("选币刷新: 程序化初筛 %d 个候选", len(candidates))

            approved = self._ai_evaluate_candidates(candidates)
            if approved is not None:
                self.cfg.symbols = approved
                if not approved:
                    logger.warning("AI 全部拒绝，白名单为空，系统空闲")
            else:
                fallback = [c["symbol"] for c in candidates[:5]]
                self.cfg.symbols = fallback
                logger.warning("AI 评估失败，回退程序化前5: %s", fallback)
                send_feishu_text(
                    "⚠️ AI 评估失败\n\n本次选币已回退程序化前五，请检查 AI 服务状态。"
                )
        except Exception:
            logger.exception("选币刷新失败，清空列表禁止交易")
            self.cfg.symbols = []

    def _ai_evaluate_candidates(self, candidates: list[dict]) -> list[str] | None:
        """调用 AI AgentLoop 逐币调查非技术面风险，返回 approved 白名单。

        AI 可使用 web_search / crypto_news 等工具查询新闻、解锁信息等。
        最后输出标准 JSON 格式。

        Returns:
            approved 列表（可能为空），AI 调用或解析失败返回 None。
        """
        import platform

        from src.agent.loop import AgentLoop
        from src.tools import build_registry

        candidates_text = ""
        for i, c in enumerate(candidates, 1):
            quality_map = {"green": "🟢强", "yellow": "🟡标准", "blue": "🔵弱"}
            q = quality_map.get(c.get("signal_quality", ""), "")
            warn = f" ⚠️{c['funding_warn']}" if c.get("funding_warn") else ""
            candidates_text += (
                f"{i}. {c['symbol']} | {c['direction']} | 现价{c['last_price']:.4f} | "
                f"TSMOM {c['tsmom_pct']:+.1f}% | Hurst {c['hurst']:.3f} | "
                f"ADX {c['adx']:.1f} | 信号 {q} | "
                f"费率{c.get('funding_rate', 0):.4f}%{warn}\n"
            )

        prompt = (
            "你是 TSMOM 自动交易系统的选币审查 AI。\n\n"
            "当前运行环境: " + platform.node() + "\n\n"
            "=== 程序化初筛结果（前十名，已按信号强度排序） ===\n"
            f"{candidates_text}\n"
            "=== 你的调查评估任务 ===\n"
            "你需要对上述每个候选币种进行调查，评估其非技术面风险，然后决定批准或拒绝。\n\n"
            "调查步骤（逐个币种执行）：\n"
            "1. 用 web_search 搜索「[币名] token unlock 2026」查看近期是否有代币解锁\n"
            "2. 用 crypto_news 搜索该币名的关键词，查看是否有负面新闻\n"
            "3. 用 web_search 搜索「[币名] hack exploit 2026」查看是否有安全事件\n"
            "4. 用 web_search 搜索「[币名] delist delisting 2026」查看是否有退市风险\n\n"
            "高效原则：\n"
            "- 不用每个币都搜全部4步，如果前两步没发现问题，就可以通过\n"
            "- 排前面的强信号币优先调查\n"
            "- 不要因不确定而拒绝——只拒绝确认存在风险的币种\n\n"
            "审批原则：\n"
            "- 没有明显负面信息的币种默认通过（放入 approved）\n"
            "- 存在已知代币解锁/黑客/退市/治理风险的放入 rejected，写清楚理由\n"
            "- 信号质量 green > yellow > blue，强信号优先通过\n\n"
            "最终输出——你必须输出严格的 JSON（不要加 markdown 代码块标记）：\n"
            '{"approved":["XXX-USDT-SWAP","YYY-USDT-SWAP"],'
            '"rejected":["ZZZ-USDT-SWAP"],'
            '"reasons":{"ZZZ-USDT-SWAP":"具体拒绝理由"},'
            '"summary":"总结：批准N个/拒绝M个，简要说明主要风险"}'
        )

        try:
            llm = ChatLLM()
            registry = build_registry()

            agent = AgentLoop(
                registry=registry,
                llm=llm,
                max_iterations=40,
            )

            result = agent.run(
                user_message=prompt,
                session_id="ai_screening_refresh",
            )
            content = (result.get("content") or "").strip()
            logger.debug("AI 评估原始回复: %s", content[:500])

            if not content:
                logger.warning("AI 评估返回空内容")
                return None

            # 尝试从 markdown 代码块中提取 JSON
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            if json_match:
                content = json_match.group(1).strip()

            # 尝试从内容中提取第一个完整的 JSON 对象
            if not content.startswith("{"):
                obj_start = content.find("{")
                if obj_start >= 0:
                    content = content[obj_start:]

            ai_result = json.loads(content)
            approved = ai_result.get("approved", [])
            rejected = ai_result.get("rejected", [])
            reasons = ai_result.get("reasons", {})
            summary = ai_result.get("summary", "")

            if not isinstance(approved, list):
                logger.warning("AI 返回的 approved 不是列表: %s", type(approved))
                return None

            self._send_ai_evaluation_card(
                approved, rejected, reasons, summary, candidates
            )
            return approved

        except json.JSONDecodeError:
            logger.exception(
                "AI 评估 JSON 解析失败，原始回复: %s",
                content[:500] if "content" in dir() else "N/A",
            )
            send_feishu_text(
                "⚠️ AI 评估失败\n\n"
                "AI 返回的 JSON 格式无法解析，已回退程序化选币。\n"
                "请检查 AI 模型输出是否符合约定的 JSON 格式。"
            )
            return None
        except Exception:
            logger.exception("AI 评估调用异常")
            return None

    def _send_ai_evaluation_card(
        self,
        approved: list[str],
        rejected: list[str],
        reasons: dict[str, str],
        summary: str,
        candidates: list[dict],
    ):
        """发送 AI 评估结果飞书卡片。"""
        bj_time = (datetime.now(TZ_UTC) + timedelta(hours=8)).strftime("%m-%d %H:%M")

        symbols_info: dict[str, dict] = {}
        for c in candidates:
            symbols_info[c["symbol"]] = c

        lines = [
            f"**🤖 AI 选币评估** - {bj_time} (北京)\n",
        ]

        if approved:
            lines.append("**✅ 批准交易 ({0})**\n".format(len(approved)))
            for sym in approved:
                info = symbols_info.get(sym, {})
                d = "📈" if info.get("direction") == "long" else "📉"
                q = info.get("signal_quality", "")
                q_emoji = {"green": "🟢", "yellow": "🟡", "blue": "🔵"}.get(q, "")
                lines.append(
                    f"{d} {sym} {info.get('direction', '?')} "
                    f"TSMOM {info.get('tsmom_pct', 0):+.1f}% {q_emoji}\n"
                )
            lines.append("")

        if rejected:
            lines.append(f"**❌ 拒绝交易 ({len(rejected)})**\n")
            for sym in rejected:
                reason = reasons.get(sym, "未提供理由")
                lines.append(f"• {sym}: {reason}\n")
            lines.append("")

        if summary:
            lines.append(f"**📋 评估摘要**\n{summary}\n")

        send_feishu_card("AI 选币评估", "".join(lines))

    # ------------------------------------------------------------------
    # 开仓
    # ------------------------------------------------------------------

    def _open_position(self, candidate: dict):
        """开仓：halt→kelly→leverage→order→stop order→feishu→log。"""
        try:
            self._do_open_position(candidate)
        except Exception:
            logger.exception("开仓异常 %s", candidate.get("symbol", "?"))

    def _do_open_position(self, candidate: dict):
        """开仓实现（被 try/except 包裹）。"""
        if halt_flag_set():
            logger.warning("halt 已触发，跳过开仓")
            return
        if self.state.position:
            logger.info("已有持仓 %s，跳过开仓", self.state.position["symbol"])
            return

        sym = candidate["symbol"]
        direction = candidate["direction"]
        entry_price = candidate["entry_price"]

        # Kelly sizing
        kelly_r = self.kelly.calculate()
        equity = kelly_r.get("equity", self.cfg.initial_capital)
        position_capital = kelly_r.get("position_capital", equity * 0.1)
        leverage = 5
        notional = position_capital * leverage
        sz, ct_val, min_sz = calc_swap_sz(notional, sym)

        # 双向持仓方向映射
        pos_side = "long" if direction == "long" else "short"
        side = "buy" if direction == "long" else "sell"

        logger.info("开仓: %s %s sz=%s price=%.4f notional=%.1f", sym, direction, sz, entry_price, notional)

        # 1. 设置杠杆
        lev_r = set_swap_leverage(symbol=sym, lever=str(leverage))
        if lev_r["status"] != "ok":
            logger.error("设置杠杆失败: %s", lev_r.get("error"))
            return

        # 2. 市价开仓（net_mode 不传 pos_side）
        order_r = place_swap_order(symbol=sym, side=side, sz=str(sz))
        if order_r["status"] != "ok":
            detail = order_r.get("detail", {})
            data_list = detail.get("data", [{}])
            s_code = data_list[0].get("sCode", "") if data_list else ""
            s_msg = data_list[0].get("sMsg", "") if data_list else ""
            logger.error("开仓失败: %s (sCode=%s sMsg=%s)", order_r.get("error"), s_code, s_msg)
            return

        # 3. 硬止损（ATR × 3，防崩溃）
        atr = candidate.get("atr", 0)
        if atr > 0:
            stop_price = entry_price - 3 * atr if direction == "long" else entry_price + 3 * atr
            stop_side = "sell" if direction == "long" else "buy"
            stop_r = place_swap_stop_order(
                symbol=sym, side=stop_side,
                sz=str(sz), stop_price=str(round(stop_price, 6)),
            )
            if stop_r["status"] == "ok":
                logger.info("止损单已设置: algo_id=%s stop=%.4f", stop_r.get("algo_id"), stop_price)
            else:
                detail = stop_r.get("detail", {})
                data_list = detail.get("data", [{}])
                s_code = data_list[0].get("sCode", "") if data_list else ""
                s_msg = data_list[0].get("sMsg", "") if data_list else ""
                logger.error("止损单设置失败: %s (sCode=%s sMsg=%s)", stop_r.get("error","?"), s_code, s_msg)
            stop_order_id = stop_r.get("algo_id") or stop_r.get("order_id", "")
        else:
            stop_order_id = ""

        # 4. 状态持久化
        self.state.position = {
            "symbol": sym,
            "direction": direction,
            "pos_side": pos_side,
            "entry_price": entry_price,
            "entry_at": datetime.now(TZ_UTC).isoformat(),
            "size": sz,
            "leverage": leverage,
            "notional": round(notional, 2),
            "ct_val": ct_val,
            "stop_order_id": stop_order_id,
        }
        self._save_state()

        # 5. 飞书通知 + 交易日志
        self._log_trade(sym, direction, entry_price, sz, "open")
        self._send_trade_card(sym, direction, entry_price, round(notional, 2), str(order_r.get("order_id", "")))

    # ------------------------------------------------------------------
    # 止损 / 平仓
    # ------------------------------------------------------------------

    def _check_stop_loss(self):
        """检查 EMA20 止损（每 1H）。"""
        if not self.state.position:
            return
        pos = self.state.position
        sym = pos["symbol"]
        direction = pos["direction"]

        df = self._fetch_klines(sym, 100, "1H")
        if df is None or len(df) < 20:
            return

        close = df["close"]
        ema20 = compute_ema(close, 20)
        current = float(close.iloc[-1])
        ema = float(ema20.iloc[-1])

        if direction == "long" and current < ema:
            logger.info("EMA20 止损触发: %s long 现价%.4f < EMA20 %.4f", sym, current, ema)
            self._close_position("EMA20止损")
        elif direction == "short" and current > ema:
            logger.info("EMA20 止损触发: %s short 现价%.4f > EMA20 %.4f", sym, current, ema)
            self._close_position("EMA20止损")
        self._last_stop_check = time.time()

    def _check_signal_reversal(self):
        """检查 TSMOM 信号是否反转（每 4H）。"""
        if not self.state.position:
            return
        pos = self.state.position
        sym = pos["symbol"]
        direction = pos["direction"]

        df = self._fetch_klines(sym, 300, "4H")
        if df is None or len(df) < 200:
            return

        close = df["close"]
        tsmom_ret = float(close.pct_change(120).iloc[-1])
        if pd.isna(tsmom_ret):
            return

        if (direction == "long" and tsmom_ret < 0) or (direction == "short" and tsmom_ret > 0):
            logger.info("TSMOM 信号反转: %s direction=%s tsmom=%.1f%%", sym, direction, tsmom_ret * 100)
            self._close_position("TSMOM反转")

    def _close_position(self, reason: str):
        """平仓：market order → cancel stop → pnl → risk check → feishu → log。"""
        pos = self.state.position
        sym = pos["symbol"]
        direction = pos["direction"]
        sz = str(pos["size"])
        entry_price = pos["entry_price"]
        pos_side = pos["pos_side"]

        # 平仓方向和开仓相反
        close_side = "sell" if direction == "long" else "buy"

        order_r = place_swap_order(symbol=sym, side=close_side, sz=str(sz))
        if order_r["status"] != "ok":
            logger.error("平仓失败: %s", order_r.get("error"))
            return

        # 获取当前价格算 PnL
        current_price = self._get_current_price(sym)
        ct_val = pos.get("ct_val", 10.0)
        if direction == "long":
            pnl = (current_price - entry_price) * pos["size"] * ct_val
        else:
            pnl = (entry_price - current_price) * pos["size"] * ct_val

        logger.info("平仓: %s %s reason=%s pnl=%.2f", sym, direction, reason, pnl)

        # 风控红线检查
        self._check_risk_limits(pnl)

        # 日志
        self._log_trade(sym, direction, entry_price, pos["size"], "close", pnl)

        # 通知
        hold_hours = 0.0
        if pos.get("entry_at"):
            entry_dt = datetime.fromisoformat(pos["entry_at"])
            hold_hours = (datetime.now(TZ_UTC) - entry_dt).total_seconds() / 3600
        self._send_close_card(sym, direction, entry_price, current_price, pnl, reason, hold_hours)

        # 清空状态
        self.state.position = None
        self._save_state()

    # ------------------------------------------------------------------
    # 风控
    # ------------------------------------------------------------------

    def _check_risk_limits(self, pnl: float):
        """风控红线检查，触发自动 halt。"""
        today = datetime.now(TZ_UTC).strftime("%Y-%m-%d")
        if self.state.daily_pnl_date != today:
            self.state.daily_pnl = 0.0
            self.state.daily_pnl_date = today

        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        equity = self.cfg.initial_capital
        kelly_r = self.kelly.calculate()
        if kelly_r.get("equity", 0) > 0:
            equity = kelly_r["equity"]

        if equity > 0 and abs(self.state.daily_pnl) / equity > DAILY_LOSS_PCT:
            trip_halt("auto", f"日亏损>{DAILY_LOSS_PCT*100:.0f}% PnL={self.state.daily_pnl:.1f}")
            self._send_alert(f"🚨 自动停止: 日亏损超过{DAILY_LOSS_PCT*100:.0f}%")

        if self.state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            trip_halt("auto", f"连续{MAX_CONSECUTIVE_LOSSES}笔止损")
            self._send_alert(f"🚨 自动停止: 连续{MAX_CONSECUTIVE_LOSSES}笔止损")

        self._save_state()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _fetch_klines(self, symbol: str, limit: int, interval: str) -> pd.DataFrame | None:
        try:
            resp = req.get(
                f"{OKX_API}/market/candles",
                params={"instId": symbol, "bar": interval, "limit": str(limit)},
                timeout=20,
            )
            data = resp.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            rows = data["data"]
            df = pd.DataFrame(rows, columns=[
                "ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_ccy_quote", "confirm",
            ])
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df.sort_values("ts").reset_index(drop=True)
            return df
        except Exception:
            return None

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
        from src.indicators.ta import compute_atr
        try:
            s = compute_atr(df["high"], df["low"], df["close"], period)
            return float(s.iloc[-1]) if not s.empty else 0.0
        except Exception:
            return 0.0

    def _get_current_price(self, symbol: str) -> float:
        try:
            resp = req.get(f"{OKX_API}/market/ticker", params={"instId": symbol}, timeout=5)
            data = resp.json()
            items = data.get("data", [])
            if items:
                return float(items[0].get("last") or 0)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _get_ct_val(symbol: str) -> float:
        base = symbol.split("-")[0]
        return 0.01 if base in ("BTC", "ETH") else 10.0

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _load_state(self) -> TradingState:
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                return TradingState(
                    position=raw.get("position"),
                    daily_pnl=raw.get("daily_pnl", 0.0),
                    daily_pnl_date=raw.get("daily_pnl_date", ""),
                    consecutive_losses=raw.get("consecutive_losses", 0),
                    last_signal_at=raw.get("last_signal_at", ""),
                )
            except Exception:
                logger.exception("加载状态失败")
        return TradingState()

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "position": self.state.position,
            "daily_pnl": self.state.daily_pnl,
            "daily_pnl_date": self.state.daily_pnl_date,
            "consecutive_losses": self.state.consecutive_losses,
            "last_signal_at": self.state.last_signal_at,
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # 通知 + 日志
    # ------------------------------------------------------------------

    def _log_trade(self, symbol: str, direction: str, price: float, size: int,
                   trade_type: str, pnl: float = 0.0):
        try:
            conn = _get_conn()
            now = int(datetime.now(TZ_UTC).timestamp())
            trade_id = f"exec_{now}_{symbol.replace('-','_')}_{trade_type}"
            conn.execute(
                """INSERT OR IGNORE INTO trade_log
                   (trade_id, symbol, inst_type, side, pos_side, price, quantity,
                    pnl, exec_type, fill_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_id, symbol, "SWAP",
                    "buy" if direction == "long" else "sell",
                    "long" if direction == "long" else "short",
                    price, size, pnl, trade_type, now, now,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.exception("交易日志写入失败")

    def _send_trade_card(self, symbol: str, direction: str, price: float,
                         notional: float, order_id: str):
        from src.push.feishu_sender import send_feishu_card
        d_emoji = "📈" if direction == "long" else "📉"
        bj_time = (datetime.now(TZ_UTC) + timedelta(hours=8)).strftime("%m-%d %H:%M")
        text = (
            f"**{d_emoji} 开仓 {symbol} {direction}**\n\n"
            f"时间: {bj_time} (北京)\n"
            f"入场价: {price:.4f}\n"
            f"名义价值: {notional:.1f}U\n"
            f"订单号: {order_id}\n"
        )
        send_feishu_card(f"开仓 {symbol}", text)

    def _send_close_card(self, symbol: str, direction: str, entry: float,
                         exit_price: float, pnl: float, reason: str, hold_hours: float = 0):
        from src.push.feishu_sender import send_feishu_card
        d_emoji = "📈" if direction == "long" else "📉"
        pnl_symbol = "+" if pnl >= 0 else "-"
        bj_time = (datetime.now(TZ_UTC) + timedelta(hours=8)).strftime("%m-%d %H:%M")
        text = (
            f"**{d_emoji} 平仓 {symbol} {direction}**\n\n"
            f"时间: {bj_time} (北京)\n"
            f"入场: {entry:.4f} → 出场: {exit_price:.4f}\n"
            f"盈亏: {pnl_symbol}{abs(pnl):.2f}U\n"
            f"持仓: {hold_hours:.1f}h\n"
            f"原因: {reason}\n"
        )
        send_feishu_card(f"平仓 {symbol}", text)

    def _send_startup_card(self):
        from src.push.feishu_sender import send_feishu_card
        env = "LIVE" if self.cfg.is_live else "DEMO"
        text = (
            f"**TSMOM 引擎启动**\n\n"
            f"环境: {env}\n"
            f"币种: {', '.join(self.cfg.symbols)}\n"
            f"时间: {datetime.now(TZ_UTC).strftime('%Y-%m-%d %H:%M')} (UTC)\n"
        )
        send_feishu_card("引擎启动", text)

    def _send_alert(self, msg: str):
        from src.push.feishu_sender import send_feishu_text
        send_feishu_text(msg)
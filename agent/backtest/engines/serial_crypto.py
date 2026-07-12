"""串行加密合约回测引擎。

一次只持有一个仓位。信号队列按币种优先级排序（BTC > ETH > 山寨）。
支持：策略接口化退出逻辑、资金费率、强平检查。

与旧版的区别：
- 退出逻辑（止损/止盈/时间退出/信号退出）抽象为 ExitStrategy 协议
- 每个策略引擎实现自己的退出逻辑，引擎只负责调用
- 仓位管理支持 1/4 凯利动态计算
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.engines._market_hooks import (
    calc_crypto_funding_fee,
    check_crypto_liquidation,
)
from backtest.models import EquitySnapshot, Position, TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """退出决策。"""
    should_exit: bool
    exit_price: float = 0.0
    reason: str = ""
    new_stop_price: Optional[float] = None  # 移动止损时更新


class ExitStrategy(ABC):
    """退出策略接口。每个策略引擎实现自己的退出逻辑。"""

    @abstractmethod
    def on_open(
        self,
        pos: Position,
        entry_bar: pd.Series,
        atr: float,
        config: "SerialConfig",
    ) -> float:
        """开仓时调用，返回初始止损价。

        Args:
            pos: 刚创建的持仓。
            entry_bar: 入场K线数据。
            atr: 入场时的ATR值。
            config: 引擎配置。

        Returns:
            初始止损价。
        """
        ...

    @abstractmethod
    def on_bar(
        self,
        pos: Position,
        bar: pd.Series,
        ts: pd.Timestamp,
        bar_idx: int,
        entry_bar_idx: int,
        stop_price: float,
        atr_at_entry: float,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
        config: "SerialConfig",
    ) -> ExitDecision:
        """每个bar检查是否退出。

        Args:
            pos: 当前持仓。
            bar: 当前K线数据。
            ts: 当前时间戳。
            bar_idx: 当前bar索引。
            entry_bar_idx: 入场bar索引。
            stop_price: 当前止损价。
            atr_at_entry: 入场时ATR。
            data_map: 所有币种数据。
            signal_map: 所有币种信号。
            config: 引擎配置。

        Returns:
            退出决策。
        """
        ...


@dataclass
class SerialConfig:
    """串行引擎配置。"""
    initial_capital: float = 150.0
    capital_per_trade: float = 50.0          # 每份资金（动态升降级，初始值）
    btc_leverage: float = 5.0
    altcoin_leverage: float = 3.0
    maker_rate: float = 0.0002
    taker_rate: float = 0.0005
    slippage_rate: float = 0.0005
    funding_rate: float = 0.0001
    # 信号冷却：同一币种 N bar 内不重复发信号
    signal_cooldown_bars: int = 6
    # 币种优先级：数字越小优先级越高
    symbol_priority: dict = field(default_factory=lambda: {
        "BTC-USDT": 0,
    })
    # 升降级阈值（基于总权益）
    upgrade_threshold: float = 2.0            # 总资金翻倍 -> 每份资金翻倍
    downgrade_threshold: float = 0.5          # 总资金腰斩 -> 每份资金减半
    tier_bases: tuple = (75.0, 150.0, 300.0, 600.0, 1200.0)  # 标准档位
    # 1/4 凯利仓位管理（如果启用，覆盖固定 capital_per_trade）
    use_kelly: bool = False
    kelly_fraction: float = 0.25              # 1/4 凯利
    kelly_win_rate: float = 0.0               # 回测统计的胜率
    kelly_payoff_ratio: float = 0.0           # 回测统计的盈亏比 W/L


@dataclass
class PendingSignal:
    """排队中的信号。"""
    symbol: str
    direction: int          # 1 多, -1 空
    timestamp: pd.Timestamp
    atr: float              # 入场时的 ATR，用于计算止损
    entry_price: float


class SerialCryptoEngine:
    """串行加密合约回测引擎。

    核心约束：同一时间最多持有一个仓位。
    信号按币种优先级排队，平仓后自动取队首信号开仓。
    退出逻辑委托给 ExitStrategy 策略接口。
    """

    def __init__(self, config: SerialConfig, exit_strategy: Optional[ExitStrategy] = None):
        self.cfg = config
        self.exit_strategy = exit_strategy
        self.capital: float = config.initial_capital
        self.position: Optional[Position] = None
        self.trades: List[TradeRecord] = []
        self.equity_snapshots: List[EquitySnapshot] = []
        self._funding_applied: set = set()
        self._funding_daily_done: set = set()

        # 持仓附加状态
        self._stop_price: float = 0.0
        self._atr_at_entry: float = 0.0
        self._entry_bar_idx: int = 0
        self._bar_idx: int = 0

        # 信号冷却：symbol -> 上次信号产生的 bar_idx
        self._last_signal_bar: dict[str, int] = {}

        # 动态资金管理
        self._current_tier_capital: float = config.capital_per_trade  # 当前每份资金
        self._tier_history: list[dict] = []  # 升降级记录

        # 信号队列
        self._signal_queue: deque[PendingSignal] = deque()

    def run(
        self,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
        atr_map: Dict[str, pd.Series],
        symbols_config: Dict[str, dict],
    ) -> Dict[str, Any]:
        """运行串行回测。

        Args:
            data_map: symbol -> OHLCV DataFrame, index 为时间。
            signal_map: symbol -> signal Series (1/0/-1)。
            atr_map: symbol -> ATR Series。
            symbols_config: symbol -> {"leverage": float, "is_btc": bool}

        Returns:
            回测指标字典。
        """
        # 合并所有时间戳
        all_dates: set = set()
        for df in data_map.values():
            all_dates.update(df.index)
        dates = pd.DatetimeIndex(sorted(all_dates))

        for i, ts in enumerate(dates):
            self._bar_idx = i

            # 1. 检查当前持仓：止损/锁利/时间退出/EMA交叉（开仓后的下一个 bar 才检查）
            if self.position is not None and i > self._entry_bar_idx:
                self._check_position(ts, i, data_map, signal_map)

            # 2. 如果空仓，从队列取信号开仓
            if self.position is None:
                self._try_open_from_queue(ts, i, data_map, symbols_config)

            # 3. 采集新信号（当前 bar 产生的信号入队）
            self._collect_signals(ts, signal_map, atr_map, data_map)

            # 4. 资金费率 + 强平检查（开仓后的下一个 bar 才检查）
            if self.position is not None and i > self._entry_bar_idx:
                self._apply_funding_and_liq(ts, data_map)

            # 5. 记录权益快照
            equity = self._calc_equity(ts, data_map)
            self.equity_snapshots.append(EquitySnapshot(
                timestamp=ts,
                capital=self.capital,
                unrealized=equity - self.capital - self._position_margin(),
                equity=equity,
                positions=1 if self.position else 0,
            ))

            # 破产检查
            if equity <= 0:
                logger.warning("Equity <= 0 at %s, backtest terminated", ts)
                break

        # 强制平仓
        if self.position is not None and len(dates) > 0:
            last_ts = dates[-1]
            price = self._get_price(self.position.symbol, last_ts, data_map)
            self._close_position(price, last_ts, "end_of_backtest")

        # 计算指标
        equity_series = pd.Series(
            [s.equity for s in self.equity_snapshots],
            index=[s.timestamp for s in self.equity_snapshots],
        )
        return self._calc_metrics(equity_series)

    def _collect_signals(
        self,
        ts: pd.Timestamp,
        signal_map: Dict[str, pd.Series],
        atr_map: Dict[str, pd.Series],
        data_map: Dict[str, pd.DataFrame],
    ) -> None:
        """采集当前 bar 的新信号入队。"""
        for symbol, sig_series in signal_map.items():
            if ts not in sig_series.index:
                continue
            sig = sig_series.at[ts]
            if sig is None or pd.isna(sig) or sig == 0:
                continue
            direction = int(sig) if sig > 0 else -1
            if abs(direction) != 1:
                continue

            # 已经在持仓且是同一币种 -> 跳过
            if self.position and self.position.symbol == symbol:
                continue

            # 信号冷却：同一币种 N bar 内不重复发信号
            last_bar = self._last_signal_bar.get(symbol, -999)
            if self._bar_idx - last_bar < self.cfg.signal_cooldown_bars:
                continue

            # 获取 ATR 和价格
            atr_val = 0.0
            if symbol in atr_map and ts in atr_map[symbol].index:
                atr_val = float(atr_map[symbol].at[ts] or 0)
            price = self._get_price(symbol, ts, data_map)
            if price <= 0 or atr_val <= 0:
                continue

            # 去重：队列里同一币种同方向只保留一个
            already_queued = any(
                s.symbol == symbol and s.direction == direction
                for s in self._signal_queue
            )
            if already_queued:
                continue

            self._signal_queue.append(PendingSignal(
                symbol=symbol,
                direction=direction,
                timestamp=ts,
                atr=atr_val,
                entry_price=price,
            ))
            self._last_signal_bar[symbol] = self._bar_idx

    def _try_open_from_queue(
        self,
        ts: pd.Timestamp,
        bar_idx: int,
        data_map: Dict[str, pd.DataFrame],
        symbols_config: Dict[str, dict],
    ) -> None:
        """从信号队列中取最高优先级信号开仓。"""
        if not self._signal_queue:
            return

        # 按优先级排序
        priority = self.cfg.symbol_priority
        sorted_signals = sorted(
            self._signal_queue,
            key=lambda s: priority.get(s.symbol, 99),
        )

        # 取第一个
        sig = sorted_signals[0]
        self._signal_queue.clear()  # 开仓后清空队列（只取最优信号）

        # 检查信号是否过时（超过 2 bar 的信号丢弃）
        df = data_map.get(sig.symbol)
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]
        open_price = float(bar.get("open", bar.get("close", 0)))
        if open_price <= 0:
            return

        # 滑点
        slipped = open_price * (1 + sig.direction * self.cfg.slippage_rate)

        # 杠杆
        sc = symbols_config.get(sig.symbol, {})
        is_btc = sc.get("is_btc", False)
        leverage = self.cfg.btc_leverage if is_btc else self.cfg.altcoin_leverage

        # 仓位大小：1/4凯利或固定档位
        capital = self._calc_position_capital()

        # 仓位大小
        notional = capital * leverage
        size = notional / slipped

        # 手续费
        comm = size * slipped * self.cfg.taker_rate

        # 保证金（开仓时锁定）
        margin = capital  # margin = notional / leverage = capital * leverage / leverage

        # 检查资金：保证金 + 手续费 必须小于可用资金
        if margin + comm > self.capital:
            return

        # 扣除保证金和手续费
        self.capital -= margin + comm

        self.position = Position(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=slipped,
            entry_time=ts,
            size=size,
            leverage=leverage,
            entry_bar_idx=bar_idx,
            entry_commission=comm,
        )

        # 设置止损：优先用策略接口，否则用默认ATR止损
        self._atr_at_entry = sig.atr
        if self.exit_strategy is not None:
            self._stop_price = self.exit_strategy.on_open(
                self.position, bar, sig.atr, self.cfg,
            )
        else:
            stop_distance = sig.atr * 1.5
            self._stop_price = slipped - sig.direction * stop_distance
        self._entry_bar_idx = bar_idx

    def _check_position(
        self,
        ts: pd.Timestamp,
        bar_idx: int,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
    ) -> None:
        """检查当前持仓是否触发退出条件。"""
        pos = self.position
        if pos is None:
            return

        df = data_map.get(pos.symbol)
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]

        # 优先用策略接口
        if self.exit_strategy is not None:
            decision = self.exit_strategy.on_bar(
                pos, bar, ts, bar_idx, self._entry_bar_idx,
                self._stop_price, self._atr_at_entry,
                data_map, signal_map, self.cfg,
            )
            if decision.new_stop_price is not None:
                self._stop_price = decision.new_stop_price
            if decision.should_exit:
                exit_price = decision.exit_price
                if exit_price <= 0:
                    close = float(bar.get("close", 0))
                    exit_price = close
                self._close_position(exit_price, ts, decision.reason)
            return

        # 默认退出逻辑：止损 + 信号反转 + 时间退出
        high = float(bar.get("high", bar.get("close", 0)))
        low = float(bar.get("low", bar.get("close", 0)))
        close = float(bar.get("close", 0))

        # 止损
        if pos.direction == 1 and low <= self._stop_price:
            self._close_position(self._stop_price, ts, "stop_loss")
            return
        if pos.direction == -1 and high >= self._stop_price:
            self._close_position(self._stop_price, ts, "stop_loss")
            return

        # 信号反转退出
        sig_series = signal_map.get(pos.symbol)
        if sig_series is not None and ts in sig_series.index:
            sig = sig_series.at[ts]
            if sig is not None and not pd.isna(sig):
                sig_val = int(sig) if sig != 0 else 0
                if sig_val != 0 and sig_val != pos.direction:
                    self._close_position(close, ts, "signal_reverse")
                    return
                if sig_val == 0:
                    self._close_position(close, ts, "signal_exit")
                    return

    def _apply_funding_and_liq(
        self,
        ts: pd.Timestamp,
        data_map: Dict[str, pd.DataFrame],
    ) -> None:
        """资金费率和强平检查。"""
        pos = self.position
        if pos is None:
            return

        df = data_map.get(pos.symbol)
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]
        fee = calc_crypto_funding_fee(
            pos.symbol, bar, ts,
            {pos.symbol: pos},
            self.cfg.funding_rate,
            self._funding_applied,
            self._funding_daily_done,
        )
        self.capital -= fee

        if check_crypto_liquidation(pos.symbol, bar, {pos.symbol: pos}):
            mark_price = float(bar.get("close", pos.entry_price))
            self._close_position(mark_price, ts, "liquidation")

    def _close_position(self, exit_price: float, exit_time: pd.Timestamp, reason: str) -> None:
        """平仓。"""
        pos = self.position
        if pos is None:
            return

        pnl = pos.direction * pos.size * (exit_price - pos.entry_price)
        margin = pos.size * pos.entry_price / pos.leverage
        exit_comm = pos.size * exit_price * self.cfg.maker_rate
        self.capital += margin + pnl - exit_comm

        holding_bars = self._bar_idx - pos.entry_bar_idx if hasattr(self, '_bar_idx') else 0

        self.trades.append(TradeRecord(
            symbol=pos.symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            size=pos.size,
            leverage=pos.leverage,
            pnl=pnl,
            pnl_pct=pnl / margin * 100 if margin > 0 else 0.0,
            exit_reason=reason,
            holding_bars=holding_bars,
            commission=pos.entry_commission + exit_comm,
        ))
        self.position = None
        self._stop_price = 0.0
        self._atr_at_entry = 0.0

        # 平仓后检查升降级
        self._check_tier_upgrade(exit_time)

    def _calc_position_capital(self) -> float:
        """计算本次交易的保证金（1/4凯利或固定档位）。"""
        if self.cfg.use_kelly and self.cfg.kelly_win_rate > 0 and self.cfg.kelly_payoff_ratio > 0:
            p = self.cfg.kelly_win_rate
            b = self.cfg.kelly_payoff_ratio
            f_kelly = p - (1 - p) / b
            if f_kelly <= 0:
                # 负期望，用最小仓位
                return self._current_tier_capital * 0.5
            f_actual = f_kelly * self.cfg.kelly_fraction
            # 保证金 = equity × f_actual
            margin = self.capital * f_actual
            # 不超过当前档位
            return min(margin, self._current_tier_capital)
        return self._current_tier_capital

    def _check_tier_upgrade(self, ts: pd.Timestamp) -> None:
        """检查总权益是否触发升降级，调整每份资金。

        纪律 1.4：总资金翻倍 -> 重新三等分
        纪律 1.5：总资金腰斩 -> 重新三等分
        """
        old_capital = self._current_tier_capital
        old_tier_total = old_capital * 3.0
        total = self.capital

        # 找到当前总权益对应的标准档位
        tiers = self.cfg.tier_bases
        best_tier = min(tiers, key=lambda t: abs(t - total))
        new_capital = best_tier / 3.0

        # 只有跨过翻倍/腰斩阈值且新档位不同时才调整
        if new_capital == old_capital:
            return

        if total >= old_tier_total * self.cfg.upgrade_threshold and new_capital > old_capital:
            self._current_tier_capital = new_capital
            self._tier_history.append({
                "timestamp": str(ts),
                "action": "upgrade",
                "old_capital": old_capital,
                "new_capital": new_capital,
                "total_equity": round(total, 2),
            })
        elif total <= old_tier_total * self.cfg.downgrade_threshold and new_capital < old_capital:
            self._current_tier_capital = new_capital
            self._tier_history.append({
                "timestamp": str(ts),
                "action": "downgrade",
                "old_capital": old_capital,
                "new_capital": new_capital,
                "total_equity": round(total, 2),
            })

    def _position_margin(self) -> float:
        """当前持仓占用保证金。"""
        if self.position is None:
            return 0.0
        return self.position.size * self.position.entry_price / self.position.leverage

    def _calc_equity(self, ts: pd.Timestamp, data_map: Dict[str, pd.DataFrame]) -> float:
        """计算总权益。"""
        if self.position is None:
            return self.capital
        price = self._get_price(self.position.symbol, ts, data_map)
        pnl = self.position.direction * self.position.size * (price - self.position.entry_price)
        margin = self._position_margin()
        return self.capital + margin + pnl

    @staticmethod
    def _get_price(symbol: str, ts: pd.Timestamp, data_map: Dict[str, pd.DataFrame]) -> float:
        """获取某时刻收盘价。"""
        df = data_map.get(symbol)
        if df is None or ts not in df.index:
            return 0.0
        val = df.at[ts, "close"] if "close" in df.columns else 0.0
        return float(val) if pd.notna(val) else 0.0

    def _calc_metrics(self, equity_series: pd.Series) -> Dict[str, Any]:
        """计算回测指标。"""
        if equity_series.empty:
            return {}

        total_return = (equity_series.iloc[-1] / equity_series.iloc[0] - 1) * 100
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak * 100
        max_drawdown = drawdown.min()

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        win_rate = len(wins) / len(self.trades) * 100 if self.trades else 0
        avg_win = np.mean([t.pnl for t in wins]) if wins else 0
        avg_loss = np.mean([abs(t.pnl) for t in losses]) if losses else 0
        profit_factor = sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses)) if losses and sum(t.pnl for t in losses) != 0 else float('inf')
        avg_hold_bars = np.mean([t.holding_bars for t in self.trades]) if self.trades else 0

        # 空仓时间
        flat_bars = sum(1 for s in self.equity_snapshots if s.positions == 0)
        flat_pct = flat_bars / len(self.equity_snapshots) * 100 if self.equity_snapshots else 0

        # 退出原因统计
        exit_reasons: dict[str, int] = {}
        for t in self.trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

        # 按币种统计
        by_symbol: dict[str, dict] = {}
        for t in self.trades:
            if t.symbol not in by_symbol:
                by_symbol[t.symbol] = {"trades": 0, "pnl": 0.0, "wins": 0}
            by_symbol[t.symbol]["trades"] += 1
            by_symbol[t.symbol]["pnl"] += t.pnl
            if t.pnl > 0:
                by_symbol[t.symbol]["wins"] += 1

        return {
            "final_equity": round(equity_series.iloc[-1], 2),
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "win_rate_pct": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else float('inf'),
            "total_trades": len(self.trades),
            "avg_holding_bars": round(avg_hold_bars, 1),
            "flat_time_pct": round(flat_pct, 1),
            "exit_reasons": exit_reasons,
            "by_symbol": by_symbol,
            "tier_changes": self._tier_history,
            "final_capital_per_trade": round(self._current_tier_capital, 2),
        }

"""ContextBuilder: builds LLM message context for the ReAct AgentLoop."""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.agent.memory import WorkspaceMemory
from src.agent.skills import SkillsLoader
from src.agent.tools import ToolRegistry

if TYPE_CHECKING:
    from src.memory.persistent import PersistentMemory

logger = logging.getLogger(__name__)

# Post-backtest attribution thresholds (Sharpe/MaxDD bands, ≥60-day OLS window,
# holding-period buckets, p≤0.05 significance) follow standard industry and
# statistical conventions; the routing logic lives in the Backtest steps below.
_SYSTEM_PROMPT = """你是 OKX-Analyzer v3 —— 一个专注于 OKX 现货和永续合约的加密货币交易研究助手。你的职责：提供基于数据的分析和可执行的交易建议，帮助用户做出明智的决策。

## 工具参考

| 需求                     | 使用工具            | 数据来源             |
|------------------------|-------------------|----------------------|
| 实时价格、持仓           | okx_portfolio     | OKX 实时             |
| 历史 OHLCV              | get_market_data   | 多数据源             |
| 技术指标                 | compute_indicators| pandas TA            |
| 资金费率                 | okx_funding_rate  | OKX 实时             |
| 新闻资讯                 | crypto_news       | 注意核实时间戳        |
| 网络检索                 | web_search/read_url | 第三方              |

**铁律**：价格、K线、订单簿数据只能来自 okx_portfolio 或 get_market_data，其他工具不含价格数据。

## K线数据与指标

**避免截断**：get_market_data 默认最多内联返回 250 行（超出时采样）。如需完整数据集，使用 output_mode="file_cache" —— 它将完整 OHLCV 序列写入 CSV 文件，返回摘要（文件路径、行数、日期范围、3 行头尾预览）。

**计算指标**：使用 output_mode="file_cache" 获取 K线数据后，调用 compute_indicators 传入 CSV 文件路径和所需指标（ema, rsi, macd, adx, bollinger, obv）。它返回最新值及趋势/区间分类。比自己写 pandas 代码更快、更一致、更省 token。

**技术分析示例流程**：
1. get_market_data(codes=["BTC-USDT"], start_date, end_date, output_mode="file_cache") -> 获取 CSV 文件路径
2. compute_indicators(file="<路径>", indicators=["ema","rsi","macd","adx","bollinger","obv"]) -> 获取最新指标摘要
3. 综合分析：将指标趋势与持仓、新闻数据结合

## 工作流程

分析市场或币种时，按以下顺序：
1. okx_portfolio -> 检查持仓和当前价格
2. get_market_data(output_mode="file_cache") + compute_indicators -> 技术分析
3. crypto_news -> 检查市场情绪
4. query_trade_log / trade_stats -> 回顾交易历史
5. 综合研判。每个数据点须标注来源和可靠度。

## 任务

**技术分析** - 用户想查看某币种指标：
1. get_market_data(codes=[...], start_date, end_date, output_mode="file_cache") -> CSV 文件
2. compute_indicators(file="<路径>", indicators=[...]) -> 最新值 + 趋势
3. 呈现：当前价格、关键指标（EMA 趋势、RSI 区间、MACD 交叉、ADX 强度、布林带位置），附简短解读

**回测** - 用户想创建或测试策略：
1. load_skill("strategy-generate")
2. write_file("config.json") - source: "okx", codes, dates, parameters
3. write_file("code/signal_engine.py") - SignalEngine 类
4. backtest(run_dir=...) -> read_file("artifacts/metrics.csv")
5. 报告：总收益、夏普比率、最大回撤、交易次数

**交易日志回顾** - 用户询问交易历史：
1. load_skill("trade-log-review")
2. query_trade_log / trade_stats
3. 呈现统计和可执行建议

**市场分析** - 用户想了解币种或市场概况：
1. okx_portfolio 获取实时数据，crypto_news 获取市场情绪
2. get_market_data + compute_indicators 获取技术面
3. 综合研判，标注来源和可靠度

**交易决策** - 用户询问是否买入/卖出/开仓：
1. load_skill("trade-discipline")
2. 对照用户的交易纪律规则
3. 基于持仓 + 技术面 + 新闻 + 纪律给出具体建议

## 通用规则

- 开始任何任务前先加载对应 skill
- 缺少关键信息时主动询问 —— 绝不猜测
- 多行数据用 markdown 管道表格输出
- 使用 ## / ### 标题，不用 --- 分隔线
- 用用户的语言回复
- 用 remember 保存重要发现以供后续会话使用
- 呈现分析时始终标注数据来源和可靠度
{memory_section}
## 当前日期时间

{current_datetime}
"""


_MEMORY_SECTION = """
## Persistent Memory (cross-session)

{snapshot}

"""


class ContextBuilder:
    """Builds message context for AgentLoop.

    Attributes:
        registry: Tool registry.
        memory: Workspace memory.
        skills_loader: Skills loader.
    """

    def __init__(self, registry: ToolRegistry, memory: WorkspaceMemory,
                 skills_loader: Optional[SkillsLoader] = None,
                 persistent_memory: Optional[PersistentMemory] = None) -> None:
        """Initialize ContextBuilder.

        Args:
            registry: Tool registry.
            memory: Workspace memory.
            skills_loader: Skills loader (auto-created if not provided).
            persistent_memory: PersistentMemory instance for cross-session recall.
        """
        self.registry = registry
        self.memory = memory
        self.skills_loader = skills_loader or SkillsLoader()
        self._persistent_memory = persistent_memory

    def build_system_prompt(self, user_message: str = "") -> str:
        """Build system prompt.

        Injects one-line skill summaries via get_descriptions; full docs loaded on demand by load_skill.
        PersistentMemory snapshot is frozen at session start (preserves prompt cache).

        Args:
            user_message: User message (kept for API compatibility).

        Returns:
            System prompt text.
        """
        now = datetime.now(timezone.utc)

        # Build memory section only if there are saved memories
        memory_section = ""
        if self._persistent_memory and self._persistent_memory.snapshot:
            memory_section = _MEMORY_SECTION.format(
                snapshot=self._persistent_memory.snapshot,
            )

        return _SYSTEM_PROMPT.format(
            memory_section=memory_section,
            current_datetime=now.strftime("%A, %B %d, %Y %H:%M UTC"),
        )

    def build_messages(self, user_message: str, history: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Build full message list.

        Auto-recalls relevant persistent memories and injects them into the
        user message as context. This keeps the system prompt stable (cacheable)
        while providing per-query relevant memories.

        Args:
            user_message: User message.
            history: Prior conversation messages.

        Returns:
            OpenAI-format message list.
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(user_message)},
        ]
        if history:
            messages.extend(history)

        # Auto-recall: inject relevant memories into user message
        enriched = user_message
        if self._persistent_memory:
            try:
                recalls = self._persistent_memory.find_relevant(user_message, max_results=3)
                if recalls:
                    lines = [f"- **{r.title}** ({r.memory_type}): {r.body[:500]}" for r in recalls]
                    recall_block = "\n".join(lines)
                    enriched = (
                        f"<recalled-memories>\n{recall_block}\n</recalled-memories>\n\n"
                        f"{user_message}"
                    )
            except Exception as exc:
                logger.debug("Auto-recall failed: %s", exc)

        messages.append({"role": "user", "content": enriched})
        return messages

    @staticmethod
    def format_tool_result(tool_call_id: str, tool_name: str, result: str) -> Dict[str, Any]:
        """Format a tool execution result as a message."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }

    @staticmethod
    def format_assistant_tool_calls(
        tool_calls: list,
        content: Optional[str] = None,
        reasoning_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Format an assistant tool_calls message, preserving thinking text.

        Args:
            tool_calls: List of tool call objects.
            content: Final assistant text (may include inlined thinking for
                providers that stream reasoning as content).
            reasoning_content: Provider-specific reasoning field (Kimi K2.5,
                DeepSeek reasoner, Qwen thinking). Only attached to the output
                message when not None, so non-thinking providers see no change.

        Returns:
            OpenAI-format assistant message.
        """
        formatted_tool_calls = []
        has_extra_content = False
        for tc in tool_calls:
            tool_call = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            extra_content = getattr(tc, "extra_content", None)
            if extra_content:
                tool_call["extra_content"] = dict(extra_content)
                has_extra_content = True
            formatted_tool_calls.append(tool_call)

        message = {
            "role": "assistant",
            "content": content,
            "tool_calls": formatted_tool_calls,
        }
        if has_extra_content:
            message["additional_kwargs"] = {
                "tool_calls": copy.deepcopy(formatted_tool_calls),
            }
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        return message

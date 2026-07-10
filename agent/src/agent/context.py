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
_SYSTEM_PROMPT = """You are OKX-Analyzer v3 — a crypto trading research assistant specializing in OKX spot and perpetual swaps. Your purpose: provide data-backed analysis and actionable trading advice to help the user make informed decisions.

## Tool Reference

| What you need           | Use this tool     | Reliability          |
|------------------------|-------------------|----------------------|
| Live prices, portfolio | okx_portfolio     | ★★★ OKX real-time    |
| Historical OHLCV (backtest) | get_market_data | ★★★ OKX historical   |
| News articles          | crypto_news       | ★★☆ check timestamp  |
| Web research           | web_search/read_url | ★☆☆ third-party     |

**Iron rule**: prices, candles, orderbook data come ONLY from okx_portfolio or get_market_data. Other tools do NOT contain price data.

## Workflow

When analyzing a market or coin, follow this order:
1. okx_portfolio → check positions and current prices
2. crypto_news → check market sentiment
3. query_trade_log / trade_stats → review trading history
4. Synthesize. Cite source and reliability level for every data point.

## Tasks

**Backtest** — user wants to create or test a strategy:
1. load_skill("strategy-generate")
2. write_file("config.json") — source: "okx", codes, dates, parameters
3. write_file("code/signal_engine.py") — SignalEngine class
4. backtest(run_dir=...) → read_file("artifacts/metrics.csv")
5. Report: total_return, sharpe, max_drawdown, trade_count

**Trade log review** — user asks about trading history:
1. load_skill("trade-log-review")
2. query_trade_log / trade_stats
3. Present stats and actionable suggestions

**Market analysis** — user wants coin or market overview:
1. okx_portfolio for live data, crypto_news for sentiment
2. Synthesize with source citations and reliability ratings

**Before trading** — user asks whether to buy/sell/open a position:
1. load_skill("trade-discipline")
2. Check against user's discipline rules
3. Give specific advice based on portfolio + market + news + discipline

## General Rules

- Load the relevant skill BEFORE starting any task
- Ask if critical info is missing — never guess
- Output multi-row data as markdown pipe tables
- Use ## / ### headings, not --- horizontal rules
- Respond in the user's language
- Save important findings with remember for future sessions
- Always cite data source and reliability when presenting analysis
{memory_section}
## Current Date & Time

Today is {current_datetime}.
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

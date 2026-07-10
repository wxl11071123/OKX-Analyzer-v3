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
_SYSTEM_PROMPT = """You are OKX-Analyzer v3 — a crypto trading research assistant. You specialize in cryptocurrency markets (spot and perpetual swaps on OKX). 

Your capabilities: backtesting strategies, reading crypto news, analyzing trade logs, monitoring portfolio, and market research.

You have access to {skill_count} specialist skills, {tool_count} tools, and {data_source_count} crypto data sources (OKX + CCXT).

**IMPORTANT**: You CANNOT place trades, cancel orders, or execute any trading actions. You are a research and analysis tool only.

## Workflow Priority

When the user asks about markets or strategy, follow this order:
1. **Portfolio first** — load_skill("portfolio-awareness"), check positions via trading_account / trading_positions
2. **News context** — load_skill("news-awareness"), read recent news via crypto_news tool
3. **Trade history** — load_skill("trade-log-review"), review recent trades via query_trade_log / trade_stats
4. **Then analyze** — with full context, proceed to the specific task

## Task Routing

**CRITICAL: Which tool gives what data.** Before any action, know:
- **okx_portfolio** = live prices, account, positions (the ONLY source for current prices)
- **crypto_news** = news ARTICLES (text headlines, NOT prices — it has no candle/price data)
- **get_market_data** = HISTORICAL candles for backtesting (NOT current price)
- **web_search** = research articles (NEVER prices — OKX is the truth)

Mistaking crypto_news for price data is a CRITICAL ERROR. If you need a price, call okx_portfolio.

**Backtest** — user wants to create, test, or optimize a trading strategy:
1. load_skill("strategy-generate") — read the SignalEngine contract
2. write_file("config.json", ...) — source: "okx", codes: ["BTC-USDT"], dates, parameters
3. write_file("code/signal_engine.py", ...) — SignalEngine class
4. Syntax check → backtest(run_dir=...) → read_file("artifacts/metrics.csv")
5. Post-backtest analysis. Present results as markdown pipe tables.
   - Report: total_return, sharpe, max_drawdown, trade_count, benchmark comparison (BTC-USDT)
   - If strategy Sharpe ≤ 0.5 or MaxDD ≥ 40%, load_skill("backtest-diagnose")

**Trade log review** — user asks about their trading history:
1. load_skill("trade-log-review")
2. query_trade_log / trade_stats to get data
3. load_skill("trade-discipline") to check against user's rules
4. Present findings with actionable suggestions

**Market analysis** — user wants market overview or specific coin analysis:
1. Load skills: portfolio-awareness → news-awareness → technical-basic
2. **Get live data via okx_portfolio** (price, positions, account). Do NOT use get_market_data for live prices — it's for backtest historical data only.
3. Get news via crypto_news (local DB)
4. Provide comprehensive analysis with data-backed conclusions
5. **NEVER use web_search for price data** — this includes current prices, candlestick data, orderbook data, or any market quote. web_search is for NEWS and RESEARCH articles only.

**Document / web** — user provides PDF or URL:
- read_document / read_url as appropriate

## Data Source Rules (MUST FOLLOW)

**Single source of truth is OKX.** All price, portfolio, and market data comes from OKX via the okx_portfolio tool.

| Data Need | Tool | Notes |
|-----------|------|-------|
| Live price / portfolio | `okx_portfolio` | Account, positions, spot holdings |
| Historical backtest data | `get_market_data` | Only for strategy backtesting |
| News & sentiment | `crypto_news` | Local DB, RSS-aggregated |
| Web research (articles only) | `web_search` | News/research articles — NOT prices |
| Read web pages | `read_url` | Full article text |

**FORBIDDEN**: Using web_search or read_url to look up cryptocurrency prices, candlestick charts, orderbook data, or any market quote. These come from OKX only.
- Consider funding rates, leverage, and liquidation risks for SWAP strategies
- **Web search** is for NEWS and RESEARCH only, not for price data or market quotes.

## General Guidelines

- Load the relevant skill BEFORE starting any task
- Ask if critical info is missing (assets, dates, parameters). Never guess.
- Output multi-row data as markdown tables (| col | col | with |---|---| separator)
- Do NOT use --- horizontal rules; use ## / ### headings instead
- All file paths are relative to run_dir (auto-injected)
- Respond in the same language the user used
- You have persistent cross-session memory (remember tool). Save user preferences and important findings.
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
            tool_count=len(self.registry._tools),
            skill_count=len(self.skills_loader.skills),
            data_source_count=self._count_data_sources(),
            tool_descriptions=self._format_tool_descriptions(),
            skill_descriptions=self.skills_loader.get_descriptions(),
            memory_summary=self.memory.to_summary(),
            memory_section=memory_section,
            current_datetime=now.strftime("%A, %B %d, %Y %H:%M UTC"),
        )

    @staticmethod
    def _count_data_sources() -> int:
        """Count registered backtest data sources for the system prompt.

        Derived from the loader registry's ``VALID_SOURCES`` (the single source
        of truth shared with the backtest config schema) minus the ``"auto"``
        cross-market selector, so the prompt never drifts from the actual
        number of loaders. Falls back to a static count if the import fails.
        """
        try:
            from backtest.loaders.registry import VALID_SOURCES

            return len(VALID_SOURCES - {"auto"})
        except Exception:  # noqa: BLE001 - prompt count must never break startup
            return 18

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

    def _format_tool_descriptions(self) -> str:
        """Format tool descriptions."""
        lines = []
        for tool in self.registry._tools.values():
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            param_parts = []
            for pname, pschema in params.items():
                req = " (required)" if pname in required else ""
                param_parts.append(f"    - {pname}: {pschema.get('description', pschema.get('type', ''))}{req}")
            param_text = "\n".join(param_parts) if param_parts else "    (no params)"
            lines.append(f"### {tool.name}\n{tool.description}\n  Params:\n{param_text}")
        return "\n\n".join(lines)

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

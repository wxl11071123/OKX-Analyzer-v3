"""Local market data tool backed by the shared loader layer."""

from __future__ import annotations

from typing import Any

from src.agent.tools import BaseTool
from src.market_data import DEFAULT_MAX_ROWS, fetch_market_data_json


class MarketDataTool(BaseTool):
    """Fetch normalized OHLCV data through repository loaders."""

    name = "get_market_data"
    repeatable = True
    description = (
        "Fetch normalized OHLCV market data through the repository loader layer. "
        "Use this for stock, ETF, index, or crypto price bars before writing raw "
        "yfinance/OKX/Tushare scripts. "
        "Default output_mode='inline' returns data inline but truncates to 250 rows "
        "(sampled) when exceeded. Use output_mode='file_cache' to write the full "
        "dataset to a CSV file and get a summary (file path + preview) instead - "
        "then use compute_indicators tool for technical analysis on the cached file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Symbols such as ["AAPL.US"], ["700.HK"], ["BTC-USDT"].',
            },
            "start_date": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD format.",
            },
            "end_date": {
                "type": "string",
                "description": "End date in YYYY-MM-DD format.",
            },
            "source": {
                "type": "string",
                "enum": [
                    "auto",
                    "yfinance",
                    "yahoo",
                    "okx",
                    "ccxt",
                    "tushare",
                    "baostock",
                    "tencent",
                    "akshare",
                    "mootdx",
                    "eastmoney",
                    "sina",
                    "stooq",
                    "finnhub",
                    "alphavantage",
                    "tiingo",
                    "fmp",
                ],
                "description": (
                    "Data source. 'auto' detects from symbol format with fallback. "
                    "Free, no key: yfinance/yahoo (US/HK equities), okx/ccxt "
                    "(crypto), baostock/tencent/eastmoney/sina/akshare/mootdx "
                    "(China A-shares), stooq (global EOD). Key-gated REST: tushare "
                    "(China A-shares), finnhub/alphavantage/tiingo/fmp (US/global)."
                ),
                "default": "auto",
            },
            "interval": {
                "type": "string",
                "description": "Bar size, e.g. 1D, 1H, 4H, 30m.",
                "default": "1D",
            },
            "max_rows": {
                "type": "integer",
                "description": "Per-symbol row cap. Use 0 only when the full series is required.",
                "default": DEFAULT_MAX_ROWS,
            },
            "output_mode": {
                "type": "string",
                "enum": ["inline", "file_cache"],
                "description": (
                    "Output mode. 'inline' (default): returns OHLCV data inline (may truncate for large datasets). "
                    "'file_cache': writes full data to a CSV cache file and returns a summary with file path, "
                    "row count, date range, and 3-row preview (head+tail). Use 'file_cache' for large datasets "
                    "to avoid truncation, then use compute_indicators tool for analysis."
                ),
                "default": "inline",
            },
        },
        "required": ["codes", "start_date", "end_date"],
    }

    def execute(self, **kwargs: Any) -> str:
        output_mode = kwargs.get("output_mode", "inline")
        if output_mode == "file_cache":
            from src.market_data import fetch_market_data_cached_json
            return fetch_market_data_cached_json(
                codes=kwargs["codes"],
                start_date=kwargs["start_date"],
                end_date=kwargs["end_date"],
                source=kwargs.get("source", "auto"),
                interval=kwargs.get("interval", "1D"),
                max_rows=kwargs.get("max_rows", 0),
            )
        return fetch_market_data_json(
            codes=kwargs["codes"],
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            source=kwargs.get("source", "auto"),
            interval=kwargs.get("interval", "1D"),
            max_rows=kwargs.get("max_rows", DEFAULT_MAX_ROWS),
        )

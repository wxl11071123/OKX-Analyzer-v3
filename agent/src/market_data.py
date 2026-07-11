"""Shared market data helpers for MCP and local agent tools."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Monkey-patch: force backtest OKX loader to use relay ──
def _patch_okx_loader():
    """Replace backtest OKX loader's direct www.okx.com access with relay proxy."""
    relay = os.getenv("OKX_RELAY")
    if not relay:
        return
    try:
        import backtest.loaders.okx
        # The loader fetches candles from www.okx.com — replace with relay
        _orig_fetch = backtest.loaders.okx.DataLoader.fetch
        def _patched_fetch(self, *args, **kwargs):
            # Temporarily redirect import-level requests.get to use relay
            import requests as _r
            _orig_get = _r.get
            def _relayed_get(url, **kw):
                if "www.okx.com" in url:
                    url = url.replace("https://www.okx.com", relay.rstrip("/"))
                return _orig_get(url, **kw)
            try:
                _r.get = _relayed_get
                return _orig_fetch(self, *args, **kwargs)
            finally:
                _r.get = _orig_get
        backtest.loaders.okx.DataLoader.fetch = _patched_fetch
        logger.info("Patched backtest OKX loader to use relay: %s", relay)
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Failed to patch OKX loader: %s", e)

_patch_okx_loader()

DEFAULT_MAX_ROWS = 250

# Symbol -> preferred source. The matched source is the head of its market's
# fallback chain (registry.FALLBACK_CHAINS), so an unavailable preferred source
# still degrades gracefully to the rest of the chain. US/HK equities route to
# the throttle-tolerant Yahoo public endpoint first (lower IP-ban risk than the
# yfinance SDK), A-shares to the Tencent quote endpoint.
_SOURCE_PATTERNS = [
    (re.compile(r"^local:", re.I), "local"),
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt"),
]


def detect_source(code: str) -> str:
    """Infer the best loader source for a normalized symbol."""
    for pattern, source in _SOURCE_PATTERNS:
        if pattern.match(code):
            return source
    return "okx"


def get_loader(source: str):
    """Get loader class via registry with fallback support."""
    from backtest.loaders.registry import get_loader_cls_with_fallback

    return get_loader_cls_with_fallback(source)


def cap_rows(records: list, max_rows: int) -> list | dict[str, object]:
    """Bound a per-symbol row list to keep tool payloads within budget."""
    n = len(records)
    if max_rows < 0:
        max_rows = DEFAULT_MAX_ROWS
    if max_rows == 0 or n <= max_rows:
        return records
    step = math.ceil(n / max_rows)
    sampled = records[::step]
    if sampled[-1] is not records[-1]:
        sampled = sampled + [records[-1]]
    return {
        "rows": n,
        "returned": len(sampled),
        "truncated": True,
        "policy": f"every-{step}th-row (even stride; last bar pinned)",
        "hint": "narrow the date range, coarsen interval, or set max_rows=0 for all rows",
        "data": sampled,
    }


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def fetch_market_data(
    *,
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
    max_rows: int = DEFAULT_MAX_ROWS,
    loader_resolver: Callable[[str], type] = get_loader,
) -> dict[str, Any]:
    """Fetch normalized OHLCV data through the repository loader layer."""
    results: dict[str, Any] = {}

    if source == "auto":
        groups: dict[str, list[str]] = {}
        for code in codes:
            src = detect_source(code)
            groups.setdefault(src, []).append(code)
    else:
        groups = {source: list(codes)}

    for src, src_codes in groups.items():
        loader_cls = loader_resolver(src)
        loader = loader_cls()
        try:
            data_map = loader.fetch(src_codes, start_date, end_date, interval=interval)
        except Exception:
            logger.exception(
                "market-data loader %r failed for %s; codes fall through to _unresolved",
                src,
                src_codes,
            )
            data_map = {}
        for symbol, df in data_map.items():
            records = df.reset_index().to_dict(orient="records")
            for row in records:
                for key, value in row.items():
                    row[key] = _json_safe(value)
            results[symbol] = cap_rows(records, max_rows)

    unresolved = [code for code in codes if code not in results]
    if unresolved:
        results["_unresolved"] = unresolved

    return results


def fetch_market_data_json(**kwargs: Any) -> str:
    """Fetch market data and return strict JSON."""
    return json.dumps(fetch_market_data(**kwargs), ensure_ascii=False, indent=2, allow_nan=False)


def _kline_cache_dir() -> Path:
    """返回 K线缓存目录，不存在则创建。"""
    d = Path.home() / ".vibe-trading" / "kline_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(symbol: str, interval: str) -> str:
    """生成安全的缓存文件名，将 symbol 中的 ``/`` 转义为 ``-``。"""
    safe_sym = symbol.replace("/", "-")
    return f"{safe_sym}_{interval}.csv"


def fetch_market_data_cached(
    *,
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
    max_rows: int = 0,
    loader_resolver: Callable[[str], type] = get_loader,
) -> dict[str, Any]:
    """拉取 OHLCV 数据，全量写入 CSV 缓存文件，返回摘要。

    与 :func:`fetch_market_data` 相同的拉取逻辑，但不做 ``cap_rows`` 截断，
    而是将全量数据写入 CSV 文件，返回包含文件路径和预览的摘要。

    Args:
        codes: 标的代码列表。
        start_date: 开始日期 (YYYY-MM-DD)。
        end_date: 结束日期 (YYYY-MM-DD)。
        source: 数据源。
        interval: K线周期。
        max_rows: 在 cached 模式下默认 0（全量写文件，不截断）。
        loader_resolver: loader 解析函数。

    Returns:
        每个 symbol 对应一个 summary dict，包含 status/rows/interval/date_range/
        file/preview_head/preview_tail/hint。
    """
    results: dict[str, Any] = {}

    if source == "auto":
        groups: dict[str, list[str]] = {}
        for code in codes:
            src = detect_source(code)
            groups.setdefault(src, []).append(code)
    else:
        groups = {source: list(codes)}

    cache_dir = _kline_cache_dir()

    for src, src_codes in groups.items():
        loader_cls = loader_resolver(src)
        loader = loader_cls()
        try:
            data_map = loader.fetch(src_codes, start_date, end_date, interval=interval)
        except Exception:
            logger.exception(
                "market-data loader %r failed for %s; codes fall through to _unresolved",
                src,
                src_codes,
            )
            data_map = {}
        for symbol, df in data_map.items():
            records = df.reset_index().to_dict(orient="records")
            for row in records:
                for key, value in row.items():
                    row[key] = _json_safe(value)

            # 写 CSV 缓存文件
            filename = _safe_filename(symbol, interval)
            csv_path = cache_dir / filename
            try:
                df.to_csv(csv_path, index=False, encoding="utf-8")
            except Exception:
                logger.exception("Failed to write kline cache file %s", csv_path)
                # 降级到 cap_rows 截断返回
                results[symbol] = cap_rows(records, DEFAULT_MAX_ROWS)
                if isinstance(results[symbol], dict):
                    results[symbol]["fallback"] = "inline_truncated"
                continue

            # 构建摘要
            n = len(records)
            preview_head = records[:3] if n >= 3 else records
            preview_tail = records[-3:] if n >= 3 else records

            # 提取日期范围
            date_keys = [k for k in records[0].keys() if "date" in k.lower() or "ts" in k.lower()] if records else []
            date_range = {"start": None, "end": None}
            if date_keys:
                dk = date_keys[0]
                if records:
                    date_range["start"] = str(records[0].get(dk, ""))
                    date_range["end"] = str(records[-1].get(dk, ""))

            results[symbol] = {
                "status": "ok",
                "rows": n,
                "interval": interval,
                "date_range": date_range,
                "file": str(csv_path),
                "preview_head": preview_head,
                "preview_tail": preview_tail,
                "hint": "Full data saved to file. Use compute_indicators tool or read_file for analysis.",
            }

    unresolved = [code for code in codes if code not in results]
    if unresolved:
        results["_unresolved"] = unresolved

    return results


def fetch_market_data_cached_json(**kwargs: Any) -> str:
    """拉取市场数据并写入文件缓存，返回 strict JSON 摘要。"""
    return json.dumps(fetch_market_data_cached(**kwargs), ensure_ascii=False, indent=2, allow_nan=False)

"""技术指标计算工具 - 从缓存 CSV 文件计算 EMA/RSI/MACD/ADX/BB/OBV 指标摘要。"""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from src.agent.tools import BaseTool
from src.indicators.ta import (
    compute_adx,
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_obv,
    compute_rsi,
)

_SUPPORTED_INDICATORS = {"ema", "rsi", "macd", "adx", "bollinger", "obv"}


def _json_safe(value: Any) -> Any:
    """将值转换为 JSON 可序列化类型。"""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _trend_direction(series: pd.Series, threshold: float = 0.001) -> str:
    """比较最新值和3期前值，判断趋势方向。

    Args:
        series: 指标值序列。
        threshold: 变化阈值（比例），默认 0.1%。

    Returns:
        "rising" | "falling" | "flat"
    """
    if len(series) < 4:
        return "flat"
    latest = series.iloc[-1]
    prev = series.iloc[-4]
    if pd.isna(latest) or pd.isna(prev) or prev == 0:
        return "flat"
    change = (latest - prev) / abs(prev)
    if change > threshold:
        return "rising"
    if change < -threshold:
        return "falling"
    return "flat"


class IndicatorTool(BaseTool):
    """从缓存的 K线 CSV 文件计算技术指标，返回最新值与趋势摘要。"""

    name = "compute_indicators"
    repeatable = True
    is_readonly = True
    description = (
        "Compute technical indicators (EMA, RSI, MACD, ADX, Bollinger Bands, OBV) "
        "from a cached K-line CSV file. Returns latest values and trend summary. "
        "Use this after get_market_data with output_mode='file_cache' to analyze "
        "indicators without reading the full CSV."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "CSV file path from get_market_data file_cache output.",
            },
            "indicators": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'List of indicator names. Supported: "ema", "rsi", "macd", "adx", "bollinger", "obv".',
            },
            "params": {
                "type": "object",
                "description": (
                    "Optional parameter overrides. "
                    'e.g. {"ema": {"period": 20}, "rsi": {"period": 14}, '
                    '"macd": {"fast": 12, "slow": 26, "signal": 9}, '
                    '"adx": {"period": 14}, "bollinger": {"window": 20, "num_std": 2.0}}.'
                ),
            },
        },
        "required": ["file", "indicators"],
    }

    def execute(self, **kwargs: Any) -> str:
        file_path = kwargs["file"]
        indicators = kwargs["indicators"]
        params = kwargs.get("params", {}) or {}

        # 校验指标名
        unknown = set(indicators) - _SUPPORTED_INDICATORS
        if unknown:
            return json.dumps({
                "status": "error",
                "error": f"Unknown indicator(s): {', '.join(sorted(unknown))}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_INDICATORS))}",
            }, ensure_ascii=False)

        # 读 CSV
        try:
            df = pd.read_csv(file_path)
        except FileNotFoundError:
            return json.dumps({
                "status": "error",
                "error": f"File not found: {file_path}",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": f"Failed to read CSV: {exc}",
            }, ensure_ascii=False)

        # 列名标准化（小写）
        df.columns = [c.lower().strip() for c in df.columns]

        if "close" not in df.columns:
            return json.dumps({
                "status": "error",
                "error": "Missing required column 'close' in CSV",
            }, ensure_ascii=False)

        close = df["close"]
        latest_close = _json_safe(close.iloc[-1])
        latest_result: dict[str, Any] = {}

        for ind in indicators:
            if ind == "ema":
                p = params.get("ema", {})
                period = p.get("period", 12)
                series = compute_ema(close, period=period)
                latest_val = _json_safe(series.iloc[-1])
                latest_result[f"ema_{period}"] = {
                    "value": latest_val,
                    "trend": _trend_direction(series),
                }

            elif ind == "rsi":
                p = params.get("rsi", {})
                period = p.get("period", 14)
                series = compute_rsi(close, period=period)
                latest_val = _json_safe(series.iloc[-1])
                zone = "neutral"
                if not pd.isna(latest_val):
                    if latest_val < 30:
                        zone = "oversold"
                    elif latest_val > 70:
                        zone = "overbought"
                latest_result[f"rsi_{period}"] = {
                    "value": latest_val,
                    "zone": zone,
                }

            elif ind == "macd":
                p = params.get("macd", {})
                fast = p.get("fast", 12)
                slow = p.get("slow", 26)
                signal = p.get("signal", 9)
                macd_df = compute_macd(close, fast=fast, slow=slow, signal=signal)
                hist_series = macd_df["macd_hist"]
                latest_hist = _json_safe(hist_series.iloc[-1])
                cross = "none"
                if len(hist_series) >= 2 and not pd.isna(latest_hist):
                    prev_hist = hist_series.iloc[-2]
                    if not pd.isna(prev_hist):
                        if latest_hist > 0 and prev_hist <= 0:
                            cross = "bullish"
                        elif latest_hist < 0 and prev_hist >= 0:
                            cross = "bearish"
                        elif latest_hist > 0:
                            cross = "bullish"
                        elif latest_hist < 0:
                            cross = "bearish"
                latest_result["macd"] = {
                    "macd": _json_safe(macd_df["macd"].iloc[-1]),
                    "signal": _json_safe(macd_df["macd_signal"].iloc[-1]),
                    "hist": latest_hist,
                    "cross": cross,
                }

            elif ind == "adx":
                required = {"high", "low"}
                missing = required - set(df.columns)
                if missing:
                    return json.dumps({
                        "status": "error",
                        "error": f"Missing required column(s) for ADX: {', '.join(sorted(missing))}",
                    }, ensure_ascii=False)
                p = params.get("adx", {})
                period = p.get("period", 14)
                adx_df = compute_adx(df["high"], df["low"], close, period=period)
                latest_adx = _json_safe(adx_df["adx"].iloc[-1])
                latest_plus = _json_safe(adx_df["plus_di"].iloc[-1])
                latest_minus = _json_safe(adx_df["minus_di"].iloc[-1])
                trend = "weak"
                if not pd.isna(latest_adx) and latest_adx > 25:
                    if not pd.isna(latest_plus) and not pd.isna(latest_minus):
                        if latest_plus > latest_minus:
                            trend = "strong_bull"
                        else:
                            trend = "strong_bear"
                latest_result["adx"] = {
                    "adx": latest_adx,
                    "plus_di": latest_plus,
                    "minus_di": latest_minus,
                    "trend": trend,
                }

            elif ind == "bollinger":
                p = params.get("bollinger", {})
                window = p.get("window", 20)
                num_std = p.get("num_std", 2.0)
                bb_df = compute_bollinger(close, window=window, num_std=num_std)
                bb_mid = _json_safe(bb_df["bb_mid"].iloc[-1])
                bb_upper = _json_safe(bb_df["bb_upper"].iloc[-1])
                bb_lower = _json_safe(bb_df["bb_lower"].iloc[-1])
                position = "middle"
                if not pd.isna(latest_close) and not pd.isna(bb_upper) and not pd.isna(bb_lower):
                    if latest_close > bb_upper:
                        position = "upper"
                    elif latest_close < bb_lower:
                        position = "lower"
                latest_result["bollinger"] = {
                    "bb_mid": bb_mid,
                    "bb_upper": bb_upper,
                    "bb_lower": bb_lower,
                    "position": position,
                }

            elif ind == "obv":
                if "volume" not in df.columns and "vol" not in df.columns:
                    return json.dumps({
                        "status": "error",
                        "error": "Missing required column 'volume' (or 'vol') in CSV for OBV",
                    }, ensure_ascii=False)
                vol_col = "volume" if "volume" in df.columns else "vol"
                obv_series = compute_obv(close, df[vol_col])
                latest_result["obv"] = {
                    "value": _json_safe(obv_series.iloc[-1]),
                    "trend": _trend_direction(obv_series),
                }

        # 最近3行预览
        recent_3 = []
        for _, row in df.tail(3).iterrows():
            row_dict = {}
            for key, value in row.items():
                row_dict[key] = _json_safe(value)
            recent_3.append(row_dict)

        return json.dumps({
            "status": "ok",
            "file": file_path,
            "rows": len(df),
            "latest": latest_result,
            "recent_3_rows": recent_3,
            "hint": "Full indicator series not shown. Read the CSV file directly for detailed analysis.",
        }, ensure_ascii=False, indent=2, allow_nan=False)

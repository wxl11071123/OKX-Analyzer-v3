#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加密货币K线数据获取示例脚本。"""

from typing import Optional

import requests
import pandas as pd

import os
from pathlib import Path

BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com") + "/api/v5"


def _kline_cache_dir() -> Path:
    """返回 K线缓存目录，不存在则创建。"""
    d = Path.home() / ".vibe-trading" / "kline_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_to_cache(df: pd.DataFrame, inst_id: str, bar: str) -> str:
    """将 K线数据保存到缓存文件，返回文件路径。

    Args:
        df: K线 DataFrame。
        inst_id: 交易产品ID，用于文件命名。
        bar: K线周期，用于文件命名。

    Returns:
        保存的 CSV 文件绝对路径。
    """
    safe_sym = inst_id.replace("/", "-")
    filename = f"{safe_sym}_{bar}.csv"
    path = _kline_cache_dir() / filename
    df.to_csv(path, index=False, encoding="utf-8")
    return str(path)

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
INDEX_CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "confirm"]


def get_candles(inst_id: str, bar: str = "1D", limit: int = 100) -> Optional[pd.DataFrame]:
    """获取K线数据并转为 DataFrame。

    Args:
        inst_id: 交易产品ID，如 BTC-USDT。
        bar: K线周期，如 1m/5m/1H/4H/1D/1W。
        limit: 返回条数，最大300。

    Returns:
        DataFrame (ts, open, high, low, close, vol)，失败返回 None。
    """
    try:
        resp = requests.get(f"{BASE_URL}/market/candles", params={
            "instId": inst_id, "bar": bar, "limit": str(limit)
        })
        data = resp.json()
        if data["code"] != "0":
            print(f"API错误: {data['msg']}")
            return None

        df = pd.DataFrame(data["data"], columns=CANDLE_COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
        for col in ["open", "high", "low", "close", "vol"]:
            df[col] = df[col].astype(float)
        df = df.sort_values("ts").reset_index(drop=True)
        _save_to_cache(df, inst_id, bar)
        return df
    except Exception as e:
        print(f"获取K线失败: {e}")
        return None


def get_index_candles(inst_id: str, bar: str = "1D", limit: int = 100) -> Optional[pd.DataFrame]:
    """获取指数K线数据。

    Args:
        inst_id: 指数ID，如 BTC-USD。
        bar: K线周期。
        limit: 返回条数，最大100。

    Returns:
        DataFrame (ts, open, high, low, close)，失败返回 None。
    """
    try:
        resp = requests.get(f"{BASE_URL}/market/index-candles", params={
            "instId": inst_id, "bar": bar, "limit": str(limit)
        })
        data = resp.json()
        if data["code"] != "0":
            print(f"API错误: {data['msg']}")
            return None

        df = pd.DataFrame(data["data"], columns=INDEX_CANDLE_COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df = df.sort_values("ts").reset_index(drop=True)
        _save_to_cache(df, inst_id, bar)
        return df
    except Exception as e:
        print(f"获取指数K线失败: {e}")
        return None


def main():
    """主函数。"""
    print("===== OKX K线数据获取示例 =====\n")

    # BTC 日线
    print("--- BTC-USDT 日线 (最近10天) ---")
    df = get_candles("BTC-USDT", "1D", 10)
    if df is not None:
        print(df[["ts", "open", "high", "low", "close", "vol"]].to_string(index=False))

    # ETH 4小时线
    print("\n--- ETH-USDT 4H线 (最近10根) ---")
    df = get_candles("ETH-USDT", "4H", 10)
    if df is not None:
        print(df[["ts", "open", "high", "low", "close", "vol"]].to_string(index=False))

    # BTC 指数日K
    print("\n--- BTC-USD 指数日线 (最近10天) ---")
    df = get_index_candles("BTC-USD", "1D", 10)
    if df is not None:
        print(df[["ts", "open", "high", "low", "close"]].to_string(index=False))


if __name__ == "__main__":
    main()

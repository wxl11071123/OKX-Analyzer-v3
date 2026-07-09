"""Crypto-only fork stub — Tushare fundamentals removed."""

from typing import Any, Dict

class TushareFundamentalProvider:
    pass

def enrich_price_frames_with_fundamentals(df, *args, **kwargs):
    return df

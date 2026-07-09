"""Crypto-only fork stub — RSSHub event feeds removed."""

from typing import Any, Dict, List

class FeedSpec:
    pass

class RSSHubEventProvider:
    def __init__(self, feeds=None):
        self.feeds = feeds or []
    def is_available(self):
        return False

def enrich_price_frames_with_events(df, *args, **kwargs):
    return df

def feed_specs_from_config(config: Dict[str, Any]) -> List:
    return []

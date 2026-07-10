"""OKX 成交记录拉取客户端。

使用 HMAC-SHA256 签名的 REST API，只读权限即可。
文档: https://www.okx.com/docs-v5/en/#rest-api-trade-get-fills-history
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx


class OKXFillsClient:
    """OKX 成交历史拉取客户端（只读）。"""

    BASE_URL = os.getenv("OKX_RELAY", "https://www.okx.com")

    def _get_base_url(self) -> str:
        return os.getenv("OKX_RELAY", "https://www.okx.com")

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        passphrase: str | None = None,
        flag: str = "0",
    ) -> None:
        self.api_key = api_key or os.getenv("OKX_API_KEY", "")
        self.secret_key = secret_key or os.getenv("OKX_API_SECRET", "")
        self.passphrase = passphrase or os.getenv("OKX_PASSPHRASE", "")
        self.flag = flag or os.getenv("OKX_FLAG", "0")

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """生成 OKX API 签名。"""
        prehash = timestamp + method.upper() + path + body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """构建签名请求头。"""
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if timestamp.endswith("+00:00"):
            timestamp = timestamp.replace("+00:00", "Z")  # OKX 要求 UTC 格式
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        """检查 API 凭证是否已配置。"""
        return bool(self.api_key and self.secret_key and self.passphrase)

    def fetch_fills(
        self,
        inst_type: str = "SPOT",
        inst_id: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        limit: str = "100",
    ) -> list[dict[str, Any]]:
        """拉取成交记录（最近 3 天）。

        Args:
            inst_type: SPOT / SWAP / FUTURES 等
            inst_id: 交易对，如 BTC-USDT（不传则拉全部）
            begin: 起始时间戳（毫秒）
            end: 结束时间戳（毫秒）
            limit: 每页条数，最大 100
        """
        path = "/api/v5/trade/fills"
        params = f"?instType={inst_type}&limit={limit}"
        if inst_id:
            params += f"&instId={inst_id}"
        if begin:
            params += f"&begin={begin}"
        if end:
            params += f"&end={end}"

        headers = self._headers("GET", path + params)
        resp = httpx.get(self._get_base_url() + path + params, headers=headers, timeout=30)
        data = resp.json()

        if data.get("code") != "0":
            raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")

        return self._parse_fills(data.get("data", []))

    def fetch_fills_history(
        self,
        inst_type: str = "SPOT",
        inst_id: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        limit: str = "100",
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        """拉取成交历史（最近 3 个月）。

        参数同 fetch_fills，额外支持 after 游标分页。
        """
        path = "/api/v5/trade/fills-history"
        params = f"?instType={inst_type}&limit={limit}"
        if inst_id:
            params += f"&instId={inst_id}"
        if begin:
            params += f"&begin={begin}"
        if end:
            params += f"&end={end}"
        if after:
            params += f"&after={after}"

        headers = self._headers("GET", path + params)
        resp = httpx.get(self._get_base_url() + path + params, headers=headers, timeout=30)
        data = resp.json()

        if data.get("code") != "0":
            raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")

        return self._parse_fills(data.get("data", []))

    def fetch_all_history(
        self,
        inst_type: str = "SPOT",
        inst_id: str | None = None,
        begin: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        """分页拉取全部成交历史（自动翻页直到无更多数据）。

        注意：OKX 限流 60次/2秒，大批量拉取时会自动间隔。
        """
        all_fills: list[dict[str, Any]] = []
        after: str | None = None
        page = 0

        while True:
            page += 1
            fills = self.fetch_fills_history(
                inst_type=inst_type,
                inst_id=inst_id,
                begin=begin,
                end=end,
                after=after,
            )
            if not fills:
                break

            all_fills.extend(fills)

            # 如果返回不足 100 条，说明已到末尾
            if len(fills) < 100:
                break

            # 用最后一条的 billId 作为下一页游标
            after = fills[-1].get("bill_id", "")
            if not after:
                break

            # OKX 限流保护
            if page % 3 == 0:
                time.sleep(0.5)

        return all_fills

    @staticmethod
    def _parse_fills(raw_data: list[dict]) -> list[dict[str, Any]]:
        """解析 OKX API 返回的成交数据为内部格式。"""
        result = []
        for item in raw_data:
            fill_time_ms = int(item.get("fillTime", "0"))
            result.append({
                "trade_id": item.get("tradeId", ""),
                "symbol": item.get("instId", ""),
                "inst_type": item.get("instType", "SPOT"),
                "side": item.get("side", ""),
                "pos_side": item.get("posSide", ""),
                "price": float(item.get("fillPx", 0)),
                "quantity": float(item.get("fillSz", 0)),
                "fee": float(item.get("fee", 0)),
                "fee_currency": item.get("feeCcy", ""),
                "pnl": float(item.get("fillPnl", 0)),
                "exec_type": item.get("execType", ""),
                "fill_time": fill_time_ms // 1000,  # 转为秒级时间戳
                "ord_id": item.get("ordId", ""),
                "bill_id": item.get("billId", ""),
            })
        return result

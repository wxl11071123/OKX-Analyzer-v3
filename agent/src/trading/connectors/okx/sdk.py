"""Read-only OKX connector via the optional ``python-okx`` SDK.

Wraps ``AccountAPI`` (account/positions), ``TradeAPI`` (orders/fills) and
``MarketAPI`` (quote/candles) for the five read operations. SWAP order functions
use direct HTTP through relay for real-time trading.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import httpx

from src.config.paths import get_runtime_root

CONFIG_FILENAME = "okx.json"

#: Profiles this connector understands and their default account environment.
PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
}

DEFAULT_HOST = "https://www.okx.com"

#: SDK ``flag`` per environment: ``"1"`` demo/paper, ``"0"`` live.
_ENVIRONMENT_FLAGS = {"paper": "1", "live": "0"}


class OKXDependencyError(RuntimeError):
    """Raised when the optional ``python-okx`` package is not installed."""


class OKXConfigError(RuntimeError):
    """Raised when the connector configuration is missing or invalid."""


@dataclass(frozen=True)
class OKXConfig:
    """OKX connector connection settings.

    Args:
        api_key: OKX API key (demo and live use different keys).
        api_secret: OKX API secret key.
        passphrase: OKX API passphrase set at key creation.
        profile: ``paper``, ``live-readonly`` or ``live``.
        host: REST host (default ``https://www.okx.com``).
        expected_uid: Optional account UID to pin in ``check_status``.
        timeout: Network timeout in seconds.
        readonly: Always true for this layer; order methods are not exposed.
    """

    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    profile: str = "paper"
    host: str = DEFAULT_HOST
    expected_uid: str = ""
    timeout: float = 15.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "OKXConfig":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise OKXConfigError("profile must be 'paper', 'live-readonly' or 'live'")
        return cls(
            api_key=str(payload.get("api_key") or "").strip(),
            api_secret=str(payload.get("api_secret") or "").strip(),
            passphrase=str(payload.get("passphrase") or "").strip(),
            profile=profile,
            host=str(payload.get("host") or DEFAULT_HOST).strip(),
            expected_uid=str(payload.get("expected_uid") or "").strip(),
            timeout=float(payload.get("timeout") or 15.0),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        profile: str | None = None,
        host: str | None = None,
        expected_uid: str | None = None,
    ) -> "OKXConfig":
        """Return a copy with CLI/tool overrides applied."""
        payload = asdict(self)
        if api_key is not None:
            payload["api_key"] = api_key
        if api_secret is not None:
            payload["api_secret"] = api_secret
        if passphrase is not None:
            payload["passphrase"] = passphrase
        if profile is not None:
            payload["profile"] = profile
        if host is not None:
            payload["host"] = host
        if expected_uid is not None:
            payload["expected_uid"] = expected_uid
        return OKXConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for this profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def flag(self) -> str:
        """Return the OKX SDK flag (``"1"`` demo/paper, ``"0"`` live)."""
        return _ENVIRONMENT_FLAGS.get(self.environment, "1")

    @property
    def is_demo(self) -> bool:
        """Return whether this profile targets the OKX demo (paper) environment."""
        return self.flag == "1"


_OVERRIDE_KEYS = ("api_key", "api_secret", "passphrase", "profile", "host", "expected_uid")


def build_config(profile_config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> "OKXConfig":
    """Resolve config: saved file ← profile defaults ← CLI overrides."""
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = OKXConfig.from_mapping(base)
    clean = {k: v for k, v in dict(overrides or {}).items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    return cfg.with_overrides(**clean) if clean else cfg


def config_path() -> Path:
    """Return the user-level OKX config path."""
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> OKXConfig:
    """Load OKX settings from ``~/.vibe-trading/okx.json``."""
    path = config_path()
    if not path.exists():
        return OKXConfig()
    try:
        return OKXConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise OKXConfigError(f"invalid OKX config at {path}: {exc}") from exc


def save_config(config: OKXConfig) -> Path:
    """Persist OKX settings with owner-only permissions."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def okx_available() -> bool:
    """Return whether the optional ``python-okx`` SDK can be imported."""
    try:
        _require_okx()
        return True
    except OKXDependencyError:
        return False


def check_status(config: OKXConfig | None = None) -> dict[str, Any]:
    """Check SDK readiness, config completeness, and account identity.

    Returns a JSON-serializable health report. Does not place or mutate any
    broker state. When ``expected_uid`` is set, the account config endpoint is
    queried (best effort) and a UID mismatch is reported as an error.
    """
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "python-okx", "installed": okx_available()},
        "paper_guard": "header_flag+uid_pin",
        "flag": cfg.flag,
    }

    if cfg.flag not in ("0", "1"):
        report["status"] = "error"
        report["error"] = f"invalid OKX flag {cfg.flag!r}; expected '0' (live) or '1' (demo)."
        return report

    missing = _missing_fields(cfg)
    if missing:
        report["status"] = "error"
        report["error"] = f"OKX connector not configured: missing {', '.join(missing)}."
        return report

    if not report["sdk"]["installed"]:
        report["status"] = "error"
        report["error"] = "Optional dependency missing: install with `pip install python-okx`."
        return report

    try:
        snapshot = get_account_snapshot(cfg)
    except Exception as exc:  # noqa: BLE001 - health endpoint reports cleanly
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    uid = None
    if cfg.expected_uid:
        try:
            account = _account_client(cfg)
            resp = _safe_call(account, "get_account_config")
            rows = _extract_data(resp)
            uid = _first(rows[0], ("uid",)) if rows else None
            if uid is not None and str(uid) != cfg.expected_uid:
                report["status"] = "error"
                report["error"] = f"UID mismatch: expected {cfg.expected_uid}, broker returned {uid}."
                return report
        except Exception as exc:  # noqa: BLE001 - uid pinning is best effort
            report["uid_check"] = {"ok": False, "error": str(exc)}

    report["account"] = {
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "uid": str(uid) if uid is not None else None,
        "total_equity": snapshot.get("account", {}).get("total_equity"),
    }
    return report


def get_account_snapshot(config: OKXConfig | None = None) -> dict[str, Any]:
    """Fetch account balance for the configured account."""
    cfg = config or load_config()
    account = _account_client(cfg)
    resp = _safe_call(account, "get_account_balance")
    rows = _extract_data(resp)
    summary = rows[0] if rows else {}
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "account": {
            "total_equity": _first(summary, ("totalEq",)),
            "details": [_balance_detail_to_dict(item) for item in _as_iter(_obj_get(summary, "details"))],
        },
    }


def get_positions(config: OKXConfig | None = None) -> dict[str, Any]:
    """Fetch current positions for the configured account."""
    cfg = config or load_config()
    account = _account_client(cfg)
    resp = _safe_call(account, "get_positions")
    rows = [_position_to_dict(item) for item in _extract_data(resp)]
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "positions": rows,
    }


def get_open_orders(config: OKXConfig | None = None, *, include_executions: bool = False) -> dict[str, Any]:
    """Fetch open orders and, optionally, recent fills."""
    cfg = config or load_config()
    trade = _trade_client(cfg)
    resp = _safe_call(trade, "get_order_list")
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "open_orders": [_order_to_dict(item) for item in _extract_data(resp)],
    }
    if include_executions:
        fills = _safe_call(trade, "get_fills")
        result["executions"] = [_fill_to_dict(item) for item in _extract_data(fills)]
    return result


def get_quote(symbol: str, *, config: OKXConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch a top-of-book ticker snapshot for ``symbol``."""
    cfg = config or load_config()
    market = _market_client(cfg)
    clean = symbol.strip().upper()
    resp = _safe_call(market, "get_ticker", instId=clean)
    rows = _extract_data(resp)
    payload = _quote_to_dict(rows[0]) if rows else {}
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": clean,
        "quote": payload,
    }


#: Canonical period token → OKX ``bar`` parameter.
_BAR_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W", "1M": "1M",
}


def get_historical_bars(
    symbol: str,
    *,
    config: OKXConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical OHLCV candlesticks for ``symbol`` (``period`` canonical)."""
    cfg = config or load_config()
    market = _market_client(cfg)
    clean = symbol.strip().upper()
    bar = _BAR_MAP.get(period.strip(), "1D")
    resp = _safe_call(market, "get_candlesticks", instId=clean, bar=bar, limit=str(int(limit)))
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "symbol": clean,
        "period": period,
        "bar": bar,
        "bars": [_candle_to_dict(item) for item in _extract_data(resp)],
    }


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------


def place_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | str | None = None,
    notional: float | str | None = None,
    order_type: str = "market",
    limit_price: float | str | None = None,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Place a spot order on the configured OKX account.

    The demo-vs-live target is selected entirely by the configured profile flag
    (``"1"`` demo, ``"0"`` live); this connector merely executes against whatever
    environment the flag selects.

    Args:
        config: Resolved connector config; loaded from disk when ``None``.
        symbol: Instrument id, e.g. ``"BTC-USDT"`` (uppercased before sending).
        side: ``"buy"`` or ``"sell"``.
        quantity: Base-currency size. Exactly one of ``quantity``/``notional``.
        notional: Quote-currency amount (market orders only). Exactly one of
            ``quantity``/``notional``.
        order_type: ``"market"`` or ``"limit"``. Limit orders require
            ``limit_price`` and must be sized with ``quantity``.
        limit_price: Limit price (required and only used for limit orders).
        time_in_force: Accepted for interface symmetry; OKX spot ``cash`` orders
            do not take a standalone TIF field, so this is recorded but not sent.

    Returns:
        On success ``{"status": "ok", "order_id", "symbol", "side", "profile",
        ...}``. On any validation or broker error a fail-closed
        ``{"status": "error", "error": ...}`` payload (no order is sent unless
        every precondition passes).
    """
    cfg = config or load_config()

    clean_side = str(side or "").strip().lower()
    if clean_side not in ("buy", "sell"):
        return _order_error(cfg, "side must be 'buy' or 'sell'", symbol=symbol, side=side)

    clean_type = str(order_type or "").strip().lower()
    if clean_type not in ("market", "limit"):
        return _order_error(cfg, "order_type must be 'market' or 'limit'", symbol=symbol, side=clean_side)

    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return _order_error(cfg, "symbol is required", symbol=symbol, side=clean_side)

    has_qty = quantity is not None
    has_notional = notional is not None
    if has_qty == has_notional:
        return _order_error(
            cfg, "exactly one of quantity or notional is required", symbol=clean_symbol, side=clean_side
        )

    # Limit orders are priced and always sized in base currency.
    if clean_type == "limit":
        if limit_price is None:
            return _order_error(cfg, "limit order requires limit_price", symbol=clean_symbol, side=clean_side)
        if not has_qty:
            return _order_error(
                cfg, "limit order must be sized with quantity (base size)", symbol=clean_symbol, side=clean_side
            )

    missing = _missing_fields(cfg)
    if missing:
        return _order_error(
            cfg, f"OKX connector not configured: missing {', '.join(missing)}.", symbol=clean_symbol, side=clean_side
        )

    # Build the OKX request. Spot trading uses tdMode="cash".
    params: dict[str, Any] = {
        "instId": clean_symbol,
        "tdMode": "cash",
        "side": clean_side,
        "ordType": clean_type,
    }
    if has_qty:
        params["sz"] = str(quantity)
    else:
        # Market order sized by quote-currency notional: OKX expects the quote
        # amount in ``sz`` plus ``tgtCcy="quote_ccy"`` so it is not mistaken for
        # a base-currency size. (notional is only reachable for market orders
        # here: limit orders reject above unless quantity is supplied.)
        params["sz"] = str(notional)
        params["tgtCcy"] = "quote_ccy"
    if clean_type == "limit":
        params["px"] = str(limit_price)

    try:
        trade = _trade_client(cfg)
        # Call the SDK directly (NOT via _safe_call): a write must fail closed on
        # any signature drift, never silently re-invoke with stripped arguments.
        resp = trade.place_order(**params)
    except OKXDependencyError as exc:
        return _order_error(cfg, str(exc), symbol=clean_symbol, side=clean_side)
    except Exception as exc:  # noqa: BLE001 - broker/network failures fail closed
        return _order_error(cfg, str(exc), symbol=clean_symbol, side=clean_side)

    return _order_result(
        cfg,
        resp,
        symbol=clean_symbol,
        side=clean_side,
        order_type=clean_type,
        time_in_force=str(time_in_force or "").strip().lower(),
    )


def cancel_order(
    config: OKXConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel a resting order on the configured OKX account.

    Args:
        config: Resolved connector config; loaded from disk when ``None``.
        order_id: OKX order id (``ordId``) to cancel.
        symbol: Instrument id of the order. OKX requires ``instId`` to cancel,
            so this is mandatory; ``None`` fails closed.

    Returns:
        On success ``{"status": "ok", "order_id", "symbol", "profile", ...}``.
        On any validation or broker error a fail-closed
        ``{"status": "error", "error": ...}`` payload.
    """
    cfg = config or load_config()

    clean_id = str(order_id or "").strip()
    if not clean_id:
        return _order_error(cfg, "OKX cancel requires order_id", symbol=symbol)

    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return _order_error(cfg, "OKX cancel requires symbol", order_id=clean_id)

    missing = _missing_fields(cfg)
    if missing:
        return _order_error(
            cfg, f"OKX connector not configured: missing {', '.join(missing)}.", symbol=clean_symbol, order_id=clean_id
        )

    try:
        trade = _trade_client(cfg)
        # Direct SDK call (not _safe_call): writes must fail closed, not retry stripped.
        resp = trade.cancel_order(instId=clean_symbol, ordId=clean_id)
    except OKXDependencyError as exc:
        return _order_error(cfg, str(exc), symbol=clean_symbol, order_id=clean_id)
    except Exception as exc:  # noqa: BLE001 - broker/network failures fail closed
        return _order_error(cfg, str(exc), symbol=clean_symbol, order_id=clean_id)

    return _order_result(cfg, resp, symbol=clean_symbol, action="cancel", requested_order_id=clean_id)


# ---------------------------------------------------------------------------
# SWAP 下单（永续合约，通过 relay 直连 OKX）
# ---------------------------------------------------------------------------

RELAY = os.getenv("OKX_RELAY", "https://www.okx.com")
OKX_API = f"{RELAY}/api/v5"


def _sway_auth_headers(method: str, path: str, body: str = "", get_params: dict[str, str] | None = None) -> dict[str, str]:
    """构建 OKX 签名头。POST 请求需传入 JSON body。

    GET 请求的查询参数必须包含在签名路径中（OKX 文档要求）。
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    secret = os.getenv("OKX_API_SECRET", "")
    # GET 请求的 query params 必须包含在签名路径中
    sign_path = path
    if get_params and method.upper() == "GET":
        qs = "&".join(f"{k}={v}" for k, v in sorted(get_params.items()))
        sign_path = f"{path}?{qs}"
    prehash = ts + method.upper() + sign_path + body
    sign = base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    flag = os.getenv("OKX_FLAG", "1")
    headers = {
        "OK-ACCESS-KEY": os.getenv("OKX_API_KEY", ""),
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.getenv("OKX_PASSPHRASE", ""),
        "Content-Type": "application/json",
    }
    if flag == "1":
        headers["x-simulated-trading"] = "1"
    return headers


def set_swap_leverage(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    lever: str,
    mgn_mode: str = "cross",
    pos_side: str = "",
) -> dict[str, Any]:
    """设置永续合约杠杆和保证金模式。

    Args:
        symbol: 合约 ID，如 "BTC-USDT-SWAP"
        lever: 杠杆倍数，如 "5"
        mgn_mode: 保证金模式 "isolated" 或 "cross"
        pos_side: 持仓方向 "long" 或 "short"（双向持仓时必填）
    """
    cfg = config or load_config()
    clean = str(symbol).strip().upper()
    path = "/api/v5/account/set-leverage"
    body: dict[str, Any] = {
        "instId": clean,
        "lever": str(lever),
        "mgnMode": mgn_mode,
    }
    if pos_side:
        body["posSide"] = pos_side

    try:
        resp = httpx.post(
            f"{OKX_API}/account/set-leverage",
            headers=_sway_auth_headers("POST", path, json.dumps(body, separators=(",", ":"))),
            json=body,
            timeout=cfg.timeout,
        )
        data = resp.json()
        if data.get("code") != "0":
            return {"status": "error", "error": data.get("msg", "set leverage failed"), "detail": data}
        return {
            "status": "ok",
            "symbol": clean,
            "lever": str(lever),
            "mgn_mode": mgn_mode,
            "profile": cfg.profile,
            "is_demo": cfg.is_demo,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "symbol": clean}


def place_swap_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    side: str,
    pos_side: str = "",
    sz: str,
    lever: str = "5",
    td_mode: str = "isolated",
    order_type: str = "market",
    px: str = "",
) -> dict[str, Any]:
    """永续合约下单。

    与现货 place_order() 的区别：
    - tdMode: "isolated" 而非 "cash"
    - posSide 可选（net_mode 不需要）
    - sz 是合约张数
    - 需先调 set_swap_leverage 设置杠杆

    Args:
        symbol: "BTC-USDT-SWAP"
        side: "buy" 开多/平空, "sell" 开空/平多
        pos_side: "long" 或 "short"（net_mode 下不传）
        sz: 合约张数
        lever: 杠杆（仅记录，实际需先 set_swap_leverage）
        td_mode: "isolated" 或 "cross"
        order_type: "market" 或 "limit"
        px: 限价单价格
    """
    cfg = config or load_config()
    clean = str(symbol).strip().upper()
    clean_side = str(side).strip().lower()
    clean_pos = str(pos_side).strip().lower() if pos_side else ""

    if clean_side not in ("buy", "sell"):
        return {"status": "error", "error": "side must be buy or sell"}

    path = "/api/v5/trade/order"
    body: dict[str, Any] = {
        "instId": clean,
        "tdMode": td_mode,
        "side": clean_side,
        "ordType": order_type,
        "sz": str(sz),
    }
    if clean_pos:
        body["posSide"] = clean_pos
    if order_type == "limit" and px:
        body["px"] = str(px)

    try:
        resp = httpx.post(
            f"{OKX_API}/trade/order",
            headers=_sway_auth_headers("POST", path, json.dumps(body, separators=(",", ":"))),
            json=body,
            timeout=cfg.timeout,
        )
        data = resp.json()
        if data.get("code") != "0":
            return {"status": "error", "error": data.get("msg", "order failed"), "detail": data}
        order_info = (data.get("data") or [{}])[0]
        return {
            "status": "ok",
            "order_id": order_info.get("ordId"),
            "symbol": clean,
            "side": clean_side,
            "pos_side": clean_pos,
            "sz": str(sz),
            "profile": cfg.profile,
            "is_demo": cfg.is_demo,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "symbol": clean}


def place_swap_stop_order(
    config: OKXConfig | None = None,
    *,
    symbol: str,
    side: str,
    pos_side: str = "",
    sz: str,
    stop_price: str,
    td_mode: str = "isolated",
) -> dict[str, Any]:
    """永续合约止损单（OKX 条件单）。

    使用 /trade/order-algo 接口创建 stop-market 条件单。
    slTriggerPx 触发后以市价平仓。

    Args:
        symbol: "BTC-USDT-SWAP"
        side: 反向平仓方向（做多时 sell，做空时 buy）
        pos_side: "long" 或 "short"（net_mode 不传）
        sz: 合约张数
        stop_price: 触发价
    """
    cfg = config or load_config()
    clean = str(symbol).strip().upper()
    clean_side = str(side).strip().lower()
    clean_pos = str(pos_side).strip().lower() if pos_side else ""

    path = "/api/v5/trade/order-algo"
    body: dict[str, Any] = {
        "instId": clean,
        "tdMode": td_mode,
        "side": clean_side,
        "ordType": "conditional",
        "sz": str(sz),
        "slTriggerPx": str(stop_price),
        "slOrdPx": "-1",
    }
    if clean_pos:
        body["posSide"] = clean_pos

    try:
        resp = httpx.post(
            f"{OKX_API}/trade/order-algo",
            headers=_sway_auth_headers("POST", path, json.dumps(body, separators=(",", ":"))),
            json=body,
            timeout=cfg.timeout,
        )
        data = resp.json()
        if data.get("code") != "0":
            return {"status": "error", "error": data.get("msg", "stop order failed"), "detail": data}
        order_info = (data.get("data") or [{}])[0]
        return {
            "status": "ok",
            "algo_id": order_info.get("algoId"),
            "order_id": order_info.get("ordId"),
            "symbol": clean,
            "side": clean_side,
            "pos_side": clean_pos,
            "stop_price": str(stop_price),
            "profile": cfg.profile,
            "is_demo": cfg.is_demo,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "symbol": clean}


def calc_swap_sz(notional_usdt: float, symbol: str) -> tuple[int, float]:
    """计算永续合约下单张数。

    通过 OKX API 查询合约面值，用标记价格换算名义价值。

    Args:
        notional_usdt: 名义价值（保证金 × 杠杆）
        symbol: "BTC-USDT-SWAP"

    Returns:
        (sz, ct_val) — 合约张数（整数）和合约面值
    """
    price = _get_mark_price(symbol)
    if price <= 0:
        return 0, 0.0

    # 从 OKX API 查询实际 ctVal 和 minSz
    try:
        resp = httpx.get(
            f"{OKX_API}/public/instruments",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == "0" and data.get("data"):
            ct_val = float(data["data"][0].get("ctVal", "10"))
            min_sz = float(data["data"][0].get("minSz", "1"))
        else:
            ct_val = 10.0
            min_sz = 1.0
    except Exception:
        ct_val = 10.0
        min_sz = 1.0

    # ctVal × price = 每张合约的 USDT 名义价值
    contract_notional = ct_val * price
    if contract_notional <= 0:
        return 0, ct_val, min_sz

    sz_float = notional_usdt / contract_notional
    # 不足最小下单量，但能买最小下单量 → 下最小量
    if sz_float < min_sz and notional_usdt >= min_sz * contract_notional:
        sz_float = min_sz
    # 取整到整手数倍数
    sz = round(sz_float / min_sz) * min_sz if min_sz > 0 else sz_float
    # 小于1张用小数显示
    if sz < 1:
        sz = round(sz, 6)

    return max(sz, 0), ct_val, min_sz


def _get_mark_price(symbol: str) -> float:
    """获取标记价格。"""
    try:
        resp = httpx.get(f"{OKX_API}/market/ticker", params={"instId": symbol}, timeout=5)
        data = resp.json()
        items = data.get("data", [])
        if items:
            return float(items[0].get("last") or 0)
    except Exception:
        pass
    return 0.0


def _order_error(cfg: OKXConfig, message: str, **extra: Any) -> dict[str, Any]:
    """Build a fail-closed error payload carrying profile/guard context."""
    payload: dict[str, Any] = {
        "status": "error",
        "error": message,
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _order_result(cfg: OKXConfig, resp: Any, *, symbol: str, **extra: Any) -> dict[str, Any]:
    """Interpret an OKX order/cancel response, failing closed on any non-zero code.

    Success requires both the outer ``code == "0"`` and the first data row's
    ``sCode == "0"``; otherwise the row's ``sMsg`` (or a generic message) is
    surfaced as an error.
    """
    rows = _extract_data(resp)
    if not rows:
        message = _resp_message(resp) or "OKX returned no order data"
        return _order_error(cfg, message, symbol=symbol, **extra)

    row = rows[0]
    s_code = str(_first(row, ("sCode",)) or "")
    if s_code != "0":
        message = str(_first(row, ("sMsg",)) or _resp_message(resp) or f"OKX rejected order (sCode={s_code or '?'})")
        return _order_error(cfg, message, symbol=symbol, **extra)

    order_id = str(_first(row, ("ordId",)) or "")
    result: dict[str, Any] = {
        "status": "ok",
        "order_id": order_id,
        "symbol": symbol,
        "profile": cfg.profile,
        "is_demo": cfg.is_demo,
        "paper_guard": "header_flag+uid_pin",
        "client_order_id": _first(row, ("clOrdId",)),
    }
    for key, value in extra.items():
        result[key] = value
    return result


def _resp_message(resp: Any) -> str:
    """Pull a top-level OKX error message (``msg``) when present."""
    if isinstance(resp, Mapping):
        return str(resp.get("msg") or "")
    return ""


# ---------------------------------------------------------------------------
# SDK plumbing
# ---------------------------------------------------------------------------


def _require_okx() -> ModuleType:
    try:
        import okx  # type: ignore
    except ModuleNotFoundError as exc:
        raise OKXDependencyError("python-okx is not installed; run `pip install python-okx`.") from exc
    return okx


def _account_client(cfg: OKXConfig):
    _require_okx()
    from okx.Account import AccountAPI  # type: ignore

    return AccountAPI(cfg.api_key, cfg.api_secret, cfg.passphrase, False, cfg.flag, domain=cfg.host)


def _trade_client(cfg: OKXConfig):
    _require_okx()
    from okx.Trade import TradeAPI  # type: ignore

    return TradeAPI(cfg.api_key, cfg.api_secret, cfg.passphrase, False, cfg.flag, domain=cfg.host)


def _market_client(cfg: OKXConfig):
    _require_okx()
    from okx.MarketData import MarketAPI  # type: ignore

    return MarketAPI(cfg.api_key, cfg.api_secret, cfg.passphrase, False, cfg.flag, domain=cfg.host)


def _missing_fields(cfg: OKXConfig) -> list[str]:
    missing = []
    if not cfg.api_key:
        missing.append("api_key")
    if not cfg.api_secret:
        missing.append("api_secret")
    if not cfg.passphrase:
        missing.append("passphrase")
    return missing


def _public_config(cfg: OKXConfig) -> dict[str, Any]:
    """Config snapshot with secrets redacted."""
    data = asdict(cfg)
    if data.get("api_secret"):
        data["api_secret"] = "***redacted***"
    if data.get("passphrase"):
        data["passphrase"] = "***redacted***"
    if data.get("api_key"):
        data["api_key"] = data["api_key"][:4] + "***"
    data["flag"] = cfg.flag
    data["is_demo"] = cfg.is_demo
    return data


# ---------------------------------------------------------------------------
# Defensive field extraction (python-okx returns ``{"code":"0","data":[...]}``)
# ---------------------------------------------------------------------------


def _as_iter(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _obj_get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = _obj_get(obj, name, None)
        if value is not None:
            return value
    return default


def _extract_data(resp: Any) -> list[Any]:
    """Return the ``data`` list from an OKX response, or ``[]`` defensively.

    python-okx returns a dict shaped ``{"code": "0", "data": [...]}`` on success.
    Anything else (a non-zero code, a non-dict, a missing/non-list ``data``) is
    treated as empty rather than raising, matching the reference connectors'
    defensive extraction posture.
    """
    if isinstance(resp, Mapping):
        if str(resp.get("code")) != "0":
            return []
        data = resp.get("data")
        return list(data) if isinstance(data, (list, tuple)) else []
    return _as_iter(resp)


def _balance_detail_to_dict(item: Any) -> dict[str, Any]:
    return {
        "currency": _first(item, ("ccy",)),
        "equity": _first(item, ("eq",)),
        "available": _first(item, ("availBal",)),
        "cash_balance": _first(item, ("cashBal",)),
        "frozen": _first(item, ("frozenBal",)),
    }


def _position_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "side": str(_first(item, ("posSide",)) or ""),
        "quantity": _first(item, ("pos",)),
        "average_cost": _first(item, ("avgPx",)),
        "unrealized_pnl": _first(item, ("upl",)),
        "margin_mode": _first(item, ("mgnMode",)),
        "leverage": _first(item, ("lever",)),
    }


def _order_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "order_id": str(_first(item, ("ordId",)) or ""),
        "client_order_id": _first(item, ("clOrdId",)),
        "price": _first(item, ("px",)),
        "quantity": _first(item, ("sz",)),
        "order_type": _first(item, ("ordType",)),
        "side": str(_first(item, ("side",)) or ""),
        "status": str(_first(item, ("state",)) or ""),
        "filled_qty": _first(item, ("fillSz",)),
        "avg_fill_price": _first(item, ("avgPx",)),
        "acc_filled_qty": _first(item, ("accFillSz",)),
    }


def _fill_to_dict(item: Any) -> dict[str, Any]:
    return {
        "symbol": _first(item, ("instId",)),
        "order_id": str(_first(item, ("ordId",)) or ""),
        "trade_id": str(_first(item, ("tradeId",)) or ""),
        "fill_price": _first(item, ("fillPx",)),
        "fill_qty": _first(item, ("fillSz",)),
        "side": str(_first(item, ("side",)) or ""),
        "fee": _first(item, ("fee",)),
        "time": str(_first(item, ("ts",)) or ""),
    }


def _quote_to_dict(item: Any) -> dict[str, Any]:
    return {
        "last": _first(item, ("last",)),
        "ask": _first(item, ("askPx",)),
        "ask_size": _first(item, ("askSz",)),
        "bid": _first(item, ("bidPx",)),
        "bid_size": _first(item, ("bidSz",)),
        "open_24h": _first(item, ("open24h",)),
        "high_24h": _first(item, ("high24h",)),
        "low_24h": _first(item, ("low24h",)),
        "volume_24h": _first(item, ("vol24h",)),
        "time": str(_first(item, ("ts",)) or ""),
    }


def _candle_to_dict(item: Any) -> dict[str, Any]:
    """Map an OKX candle array ``[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]``.

    OKX candlesticks come back as positional arrays rather than dicts, so this
    indexes defensively and tolerates short rows.
    """
    row = list(item) if isinstance(item, (list, tuple)) else []

    def at(idx: int) -> Any:
        return row[idx] if idx < len(row) else None

    # ``confirm`` is always the LAST element across OKX's 7- and 9-field candle
    # shapes; read it positionally from the tail rather than a fixed index.
    return {
        "time": str(at(0) or ""),
        "open": at(1),
        "high": at(2),
        "low": at(3),
        "close": at(4),
        "volume": at(5),
        "volume_ccy": at(6),
        "confirm": row[-1] if row else None,
    }


def _safe_call(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``obj.name(*args, **kwargs)`` if it exists, retrying without kwargs.

    python-okx signatures vary across versions (some read methods accept extra
    keyword filters, some do not). We try the richer call first and fall back to
    the no-arg form so a signature drift degrades to a usable call instead of an
    error.
    """
    fn = getattr(obj, name, None)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except TypeError:
        try:
            return fn(*args)
        except TypeError:
            return fn()

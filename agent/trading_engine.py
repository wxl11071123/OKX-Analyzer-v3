#!/usr/bin/env python3
"""TSMOM 自动化交易引擎。

用法:
    # Paper trading (demo)
    OKX_FLAG=1 python trading_engine.py

    # Live trading
    OKX_FLAG=0 python trading_engine.py

环境变量:
    OKX_FLAG: "1"=demo, "0"=live (默认 "1")
    OKX_RELAY: relay 地址或直连 https://www.okx.com
    OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE: 凭证
"""

from __future__ import annotations

import logging
import os
import signal
import sys

# 加载 .env（systemd 不会自动加载）
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                if _k not in os.environ:
                    os.environ[_k] = _v

from src.trading.executor import ExecutorConfig, TradingExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("trading_engine")

DEFAULT_SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


def main():
    env = os.environ.get("OKX_FLAG", "1")
    is_live = env == "0"
    logger.info(
        "TSMOM 交易引擎启动, env=%s (%s)", env,
        "LIVE" if is_live else "DEMO",
    )

    executor = TradingExecutor(
        ExecutorConfig(
            symbols=DEFAULT_SYMBOLS,
            initial_capital=50.0 if is_live else 150.0,
            is_live=is_live,
        ),
    )

    def _shutdown(_sig, _frame):
        logger.info("收到退出信号, 停止引擎...")
        executor.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    executor.run()


if __name__ == "__main__":
    main()
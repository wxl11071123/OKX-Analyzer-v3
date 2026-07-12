"""Kill-switch (halt) sentinel for live trading.

纯文件系统机制，不依赖 LLM/Agent 状态：

- ``trip_halt()`` 写 ``~/.vibe-trading/live/HALT`` 文件
- ``halt_flag_set()`` 检查文件是否存在
- ``clear_halt()`` 删除文件
- ``read_halt()`` 读取元数据

全局 HALT 阻断所有 broker；per-broker HALT 只阻断指定 broker。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.live.paths import live_dir

_HALT_FILENAME = "HALT"
_HALT_ACTIONS: Dict[Optional[str], Callable[[str], Any]] = {}


def _broker_key(broker: Optional[str]) -> Optional[str]:
    """Normalise a broker key. Returns None for the global scope.

    An unresolvable key (path traversal) is treated as halted — the gate must
    fail closed.
    """
    if broker is None:
        return None
    key = broker.strip().lower()
    if not key or ".." in key or "/" in key or "\\" in key:
        return None
    return key


def halt_path(broker: Optional[str] = None) -> Path:
    """Return the HALT sentinel file path for the given scope.

    Args:
        broker: Broker key, or ``None`` for the global switch.
    """
    key = _broker_key(broker)
    if key is None:
        return live_dir() / _HALT_FILENAME
    return live_dir() / key / _HALT_FILENAME


def halt_flag_set(broker: Optional[str] = None) -> bool:
    """Return whether the kill switch is tripped.

    The global HALT sentinel blocks every broker. A per-broker sentinel blocks
    only that broker. An unresolvable broker key (path traversal, etc.) is
    treated as halted (fail closed).

    Args:
        broker: Broker key, or ``None`` to check the global switch.
    """
    key = _broker_key(broker)
    if broker is not None and key is None:
        return True
    if key is None:
        return halt_path(None).exists()

    if halt_path(None).exists():
        return True
    return halt_path(key).exists()


def trip_halt(by: str, reason: str, broker: Optional[str] = None) -> Path:
    """Write the HALT sentinel file.

    Args:
        by: Who or what triggered the halt (e.g. "cli", "frontend", "feishu").
        reason: Human-readable reason.
        broker: Broker key, or ``None`` for the global switch.

    Returns:
        The sentinel file path that was written.
    """
    path = halt_path(broker)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "by": by,
        "reason": reason,
        "broker": broker,
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "tripped_at_epoch_ms": int(time.time() * 1000),
    }
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_halt(broker: Optional[str] = None) -> bool:
    """Remove the HALT sentinel file.

    Args:
        broker: Broker key, or ``None`` for the global switch.

    Returns:
        ``True`` if a sentinel was found and removed, ``False`` if none existed.
    """
    path = halt_path(broker)
    if not path.exists():
        return False
    path.unlink()
    return True


def read_halt(broker: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the metadata from the HALT sentinel file.

    Args:
        broker: Broker key, or ``None`` for the global switch.

    Returns:
        A dict with ``by``, ``reason``, ``broker``, ``tripped_at``, and
        ``tripped_at_epoch_ms`` keys, or ``None`` if no sentinel exists.
        Returns an empty dict if the file exists but is unparseable (the
        sentinel is authoritative by existence, not by content).
    """
    path = halt_path(broker)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def register_halt_action(fn: Callable[[str], Any], broker: Optional[str] = None) -> None:
    """Register a callback to run when the halt switch is tripped.

    Args:
        fn: Callable receiving the broker key as argument.
        broker: Scope the action to a specific broker, or ``None`` for global.
    """
    _HALT_ACTIONS[broker] = fn


def unregister_halt_action(broker: Optional[str] = None) -> bool:
    """Remove a previously registered halt action callback.

    Returns:
        ``True`` if an action was found and removed, ``False`` otherwise.
    """
    if broker in _HALT_ACTIONS:
        del _HALT_ACTIONS[broker]
        return True
    return False


def on_halt_action(broker: str) -> Any:
    """Run the registered halt action for the given broker, if any.

    Per-broker actions take precedence over global actions.

    Args:
        broker: The broker key whose halt action should be triggered.

    Returns:
        The return value of the action callback, or ``None`` if no action is
        registered.
    """
    fn = _HALT_ACTIONS.get(broker) or _HALT_ACTIONS.get(None)
    if fn is None:
        return None
    return fn(broker)
"""Live-trading runtime paths.

Centralises the live-trading runtime root so kill-switch, mandate, audit, and
runner code all share the same configurable filesystem location.
"""

from __future__ import annotations

from pathlib import Path

from src.config.paths import get_runtime_root as _config_runtime_root


def get_runtime_root() -> Path:
    """Return the live-trading runtime root directory.

    Returns:
        ``~/.vibe-trading`` by default, overridable in tests via monkeypatch.
    """
    return _config_runtime_root()


def live_dir() -> Path:
    """Return `<runtime_root>/live` (created on first access)."""
    d = get_runtime_root() / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def broker_dir(broker: str) -> Path:
    """Return `<runtime_root>/live/<broker>` (created on first access)."""
    d = live_dir() / broker.strip().lower()
    d.mkdir(parents=True, exist_ok=True)
    return d
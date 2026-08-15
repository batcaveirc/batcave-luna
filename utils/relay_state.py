"""
Thread-safe relay toggle management.

Stores on/off state for each relay path. All toggles default to ON.
Dashboard writes toggle state; relay modules read it before forwarding.

Usage:
    from utils.relay_state import relay_state

    # Check before relaying
    if relay_state.is_enabled("relay_to_irc"):
        bridge.send_to_irc(msg)

    # Toggle from dashboard
    relay_state.set_enabled("relay_to_irc", False)
"""

import threading
import time
from typing import Any


# ── Toggle names ─────────────────────────────────────────────────────────────

RELAY_TO_IRC = "relay_to_irc"
RELAY_TO_DISCORD = "relay_to_discord"
RELAY_TO_STARALIGN = "relay_to_staralign"

_ALL_TOGGLES = (RELAY_TO_IRC, RELAY_TO_DISCORD, RELAY_TO_STARALIGN)


# ── Statistics tracking ──────────────────────────────────────────────────────

class _Stats:
    """Thread-safe counters for relay activity."""

    __slots__ = ("_lock", "_messages_today", "_rate_limit_hits", "_day_stamp")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages_today: int = 0
        self._rate_limit_hits: int = 0
        self._day_stamp: str = _today()

    def record_message(self) -> None:
        with self._lock:
            self._roll_day()
            self._messages_today += 1

    def record_rate_limit_hit(self) -> None:
        with self._lock:
            self._roll_day()
            self._rate_limit_hits += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll_day()
            return {
                "messages_today": self._messages_today,
                "rate_limit_hits": self._rate_limit_hits,
                "day": self._day_stamp,
            }

    def _roll_day(self) -> None:
        today = _today()
        if self._day_stamp != today:
            self._messages_today = 0
            self._rate_limit_hits = 0
            self._day_stamp = today


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ── Recent messages log ──────────────────────────────────────────────────────

class _RecentMessages:
    """Thread-safe ring buffer of recent relay messages (last N)."""

    __slots__ = ("_lock", "_buffer", "_capacity")

    def __init__(self, capacity: int = 20) -> None:
        self._lock = threading.Lock()
        self._buffer: list[dict[str, str]] = []
        self._capacity = capacity

    def append(self, direction: str, username: str, text: str) -> None:
        entry = {
            "time": time.strftime("%H:%M:%S", time.gmtime()),
            "direction": direction,
            "username": username,
            "text": text[:200],
        }
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) > self._capacity:
                self._buffer = self._buffer[-self._capacity:]

    def list_recent(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._buffer)


# ── Relay state singleton ────────────────────────────────────────────────────

class RelayState:
    """Central toggle + stats store, shared across all modules."""

    __slots__ = ("_lock", "_toggles", "stats", "recent", "_start_time")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._toggles: dict[str, bool] = {name: True for name in _ALL_TOGGLES}
        self.stats = _Stats()
        self.recent = _RecentMessages(capacity=20)
        self._start_time: float = time.time()

    def is_enabled(self, toggle_name: str) -> bool:
        with self._lock:
            return self._toggles.get(toggle_name, True)

    def set_enabled(self, toggle_name: str, enabled: bool) -> None:
        with self._lock:
            self._toggles[toggle_name] = enabled

    def all_toggles(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._toggles)

    def uptime_seconds(self) -> float:
        return time.time() - self._start_time


# Module-level singleton — import this everywhere
relay_state = RelayState()

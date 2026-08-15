"""
Luna keep-alive API server — lightweight endpoints for health, relay status,
and relay control.  No dashboard HTML, no auth (BatCave Bot Manager handles that).

Endpoints:
  GET  /health           → {"status": "ok"}
  GET  /api/relay-status → relay toggle states + stats
  POST /api/toggle       → {"key": "<toggle_name>", "enabled": true/false}
  POST /api/test-relay   → {"username": "...", "text": "..."}
  POST /api/irc/nick     → {"nick": "<new_nick>"} — change Luna's IRC nick
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

from utils.relay_state import (
    RELAY_TO_DISCORD,
    RELAY_TO_IRC,
    RELAY_TO_STARALIGN,
    relay_state,
)

_VALID_TOGGLES = frozenset({RELAY_TO_IRC, RELAY_TO_DISCORD, RELAY_TO_STARALIGN})

# Injected references — luna.py calls set_bot_ref() after startup
_bot_ref: Any = None
_bridge_ref: Any = None


def set_bot_ref(bot: Any, bridge: Any) -> None:
    """Inject bot and bridge references so API can dispatch test relays."""
    global _bot_ref, _bridge_ref
    _bot_ref = bot
    _bridge_ref = bridge


def keep_alive(port: int = 8081) -> None:
    """Start the API server on a daemon thread (localhost only)."""
    server = HTTPServer(("127.0.0.1", port), _APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[keep_alive] Luna API on http://127.0.0.1:{port}")


class _APIHandler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._json({"status": "ok"})
        elif path == "/api/relay-status":
            self._json(_build_relay_status())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/toggle":
            self._handle_toggle()
        elif path == "/api/test-relay":
            self._handle_test_relay()
        elif path == "/api/irc/nick":
            self._handle_irc_nick()
        else:
            self._json({"error": "not found"}, 404)

    # ── Route handlers ───────────────────────────────────────────────────

    def _handle_toggle(self) -> None:
        data = self._read_json()
        if data is None:
            self._json({"ok": False, "error": "invalid JSON"})
            return
        key = data.get("key", "")
        enabled = bool(data.get("enabled", True))
        if key not in _VALID_TOGGLES:
            self._json({"ok": False, "error": f"unknown toggle: {key}"})
            return
        relay_state.set_enabled(key, enabled)
        self._json({"ok": True, "key": key, "enabled": enabled})

    def _handle_test_relay(self) -> None:
        data = self._read_json()
        if data is None:
            self._json({"ok": False, "error": "invalid JSON"})
            return
        username = data.get("username", "DashboardTest")
        text = data.get("text", "")
        if not text:
            self._json({"ok": False, "error": "text is required"})
            return
        try:
            from utils.staralign_relay import relay_to_staralign
            loop = getattr(_bridge_ref, "loop", None) if _bridge_ref else None
            if loop and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    relay_to_staralign(username=username, text=text), loop,
                )
                result = fut.result(timeout=15)
            else:
                result = asyncio.run(
                    relay_to_staralign(username=username, text=text)
                )
            relay_state.recent.append("test_send", username, text)
            self._json({"ok": result})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})

    def _handle_irc_nick(self) -> None:
        data = self._read_json()
        if data is None:
            self._json({"ok": False, "error": "invalid JSON"})
            return
        nick = (data.get("nick") or "").strip()
        if not nick:
            self._json({"ok": False, "error": "nick required"})
            return
        if _bridge_ref is None:
            self._json({"ok": False, "error": "bridge not ready"})
            return
        ok = _bridge_ref.change_nick(nick)
        self._json({"ok": bool(ok)})

    # ── Helpers ──────────────────────────────────────────────────────────

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def log_message(self, *_: Any) -> None:
        pass  # suppress access logs


def _build_relay_status() -> dict:
    """Assemble the full relay-status payload."""
    from utils.staralign_relay import (
        STARALIGN_RELAY_URL,
        _is_configured,
    )
    stats = relay_state.stats.snapshot()
    return {
        "toggles": relay_state.all_toggles(),
        "stats": stats,
        "staralign": {
            "configured": _is_configured(),
            "url": STARALIGN_RELAY_URL,
        },
    }

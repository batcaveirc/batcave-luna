"""
StarAlign Relay — forwards IRC/Discord messages to StarAlign's community bridge room.

Messages arrive as anonymous relay users in StarAlign's "The Bridge" chatroom.
Users appear as "Guest XXXX" (deterministic hash) — no platform info is exposed.

Configuration (in .env):
  STARALIGN_RELAY_URL=https://<your-backend>/api/community/relay/ingest
  STARALIGN_RELAY_SECRET=<shared secret matching RELAY_BOT_SECRET in functions/.env>
"""

import asyncio
import os
import time
from typing import Optional

from utils.relay_state import RELAY_TO_STARALIGN, relay_state

# Use aiohttp if available; fall back to urllib for zero-dep simplicity
try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


# ── Configuration ────────────────────────────────────────────────────────────

# No default endpoint. This repo is public, and a hardcoded cloud-function URL
# publishes which backend project the owner runs — infrastructure nobody needs
# to know about. Unset, the relay stays dormant, which is the right behaviour
# for anyone who is not the owner running this.
STARALIGN_RELAY_URL: str = os.getenv("STARALIGN_RELAY_URL", "")
STARALIGN_RELAY_SECRET: str = os.getenv("STARALIGN_RELAY_SECRET", "")
STARALIGN_BOT_ID: str = "relaybot"

# Rate limiter: max 20 messages per 60 seconds
_RATE_WINDOW = 60
_RATE_LIMIT = 20
_send_times: list[float] = []


def _is_configured() -> bool:
    """Check if StarAlign relay is properly configured."""
    return bool(STARALIGN_RELAY_URL and STARALIGN_RELAY_SECRET)


def _rate_ok() -> bool:
    """Simple sliding-window rate limiter."""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    # Remove old entries
    while _send_times and _send_times[0] < cutoff:
        _send_times.pop(0)
    if len(_send_times) >= _RATE_LIMIT:
        return False
    _send_times.append(now)
    return True


# ── Async relay ──────────────────────────────────────────────────────────────

async def relay_to_staralign(
    username: str,
    text: str,
    bot_id: Optional[str] = None,
    kind: str = "user",
) -> bool:
    """
    Send a message to StarAlign's bridge room.

    Args:
        username: Display name (e.g. IRC nick or Discord display name).
                  Platform prefix like [IRC] is stripped server-side.
        text: Message content (max 500 chars, trimmed server-side).
        bot_id: Optional bot identifier for audit (default: "relaybot").
        kind: "user" (relayed human, anonymized as Guest on StarAlign) or
              "bot" (automated bot — shown as the single identity "bot").

    Returns:
        True if the message was accepted, False otherwise.
    """
    if not _is_configured():
        return False

    if not relay_state.is_enabled(RELAY_TO_STARALIGN):
        return False

    if not username or not text or not text.strip():
        return False

    if not _rate_ok():
        print("[staralign_relay] Rate limit hit — skipping message")
        relay_state.stats.record_rate_limit_hit()
        return False

    payload = {
        "username": username[:42],
        "text": text[:500],
        "botId": bot_id or STARALIGN_BOT_ID,
        "kind": "bot" if kind == "bot" else "user",
    }

    headers = {
        "Content-Type": "application/json",
        "x-relay-secret": STARALIGN_RELAY_SECRET,
    }

    try:
        if _HAS_AIOHTTP:
            ok = await _send_aiohttp(payload, headers)
        else:
            ok = await _send_urllib(payload, headers)
        if ok:
            relay_state.stats.record_message()
            relay_state.recent.append("to_staralign", username, text[:200])
        return ok
    except Exception as e:
        print(f"[staralign_relay] Error: {e}")
        return False


async def _send_aiohttp(
    payload: dict,
    headers: dict,
) -> bool:
    """Send via aiohttp (preferred — non-blocking)."""
    import json

    async with aiohttp.ClientSession() as session:
        async with session.post(
            STARALIGN_RELAY_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            print(
                f"[staralign_relay] API returned {resp.status}: "
                f"{body[:200]}"
            )
            return False


async def _send_urllib(
    payload: dict,
    headers: dict,
) -> bool:
    """Fallback: send via urllib in a thread executor."""
    import json
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        STARALIGN_RELAY_URL,
        data=data,
        headers=headers,
        method="POST",
    )

    loop = asyncio.get_event_loop()
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(req, timeout=10),
        )
        return resp.status == 200
    except Exception as e:
        print(f"[staralign_relay] urllib error: {e}")
        return False

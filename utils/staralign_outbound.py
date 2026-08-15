"""
staralign_outbound.py — reverse relay (StarAlign → Discord).

Lets a StarAlign user talk TO the Vampire bot by tagging it
("@bot ...", "bot: ...", "hey bot ..."). This module polls the
backend's `/community/relay/outbound` endpoint for such tagged
messages; luna.py forwards each into the Discord bridge channel so
the Vampire bot (which replies to channel messages) responds. Its
reply then relays back to StarAlign as the "bot" identity via the
normal Discord→StarAlign path.

Stateless dedupe: the caller tracks the last seen timestamp; we only
request messages newer than that.
"""

from __future__ import annotations

import os
from typing import List, NamedTuple

try:
    import aiohttp

    _HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - fallback path
    _HAS_AIOHTTP = False

from utils.staralign_relay import (
    STARALIGN_RELAY_SECRET,
    STARALIGN_RELAY_URL,
)

# Derive the outbound URL from the configured ingest URL so a single
# env var (STARALIGN_RELAY_URL) keeps both endpoints in sync.
_OUTBOUND_URL: str = os.getenv(
    "STARALIGN_OUTBOUND_URL",
    STARALIGN_RELAY_URL.replace("/relay/ingest", "/relay/outbound"),
)


class OutboundMessage(NamedTuple):
    id: str
    sender_name: str
    text: str
    ts: int


def is_configured() -> bool:
    return bool(_OUTBOUND_URL and STARALIGN_RELAY_SECRET)


async def fetch_tagged_messages(since_ts: int) -> List[OutboundMessage]:
    """
    Fetch StarAlign messages that tag the bot, newer than ``since_ts``.

    Returns an empty list on any error or if not configured — callers
    should treat this as "nothing new" and keep their cursor.
    """
    if not is_configured() or not _HAS_AIOHTTP:
        return []

    url = f"{_OUTBOUND_URL}?since={int(since_ts)}"
    headers = {"x-relay-secret": STARALIGN_RELAY_SECRET}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception as e:  # noqa: BLE001 - never crash the bot loop
        print(f"[staralign_outbound] fetch error: {e}")
        return []

    out: List[OutboundMessage] = []
    for m in data.get("messages", []) or []:
        try:
            out.append(
                OutboundMessage(
                    id=str(m.get("id", "")),
                    sender_name=str(m.get("senderName", "Soul"))[:42],
                    text=str(m.get("text", ""))[:480],
                    ts=int(m.get("ts", 0)),
                )
            )
        except (TypeError, ValueError):
            continue
    return out

"""Hearing an attack being organised, before it reaches the room.

Luna's half of the early-warning system. Same rules as Dracula's watch.js, and
they exist for the same reasons:

1. **Observe abroad, act at home.** Luna sits in other people's rooms as a
   guest. She never speaks or sets a mode there — moderating someone else's
   channel is presumptuous, and it would get her banned from the very rooms
   worth watching.

2. **Naming our channel is not suspicious.** Regulars invite friends and the
   recruiter advertises there deliberately. What marks an attack is the mention
   arriving WITH abuse, or hammered repeatedly the way spam is.

She runs it whether or not Dracula is present. Two bots hearing the same
advertisement is harmless — the alert is idempotent per nick — while a gap in
coverage is exactly when a raid gets assembled unseen.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional

REPEAT_THRESHOLD = 3
MEMORY_SEC = 45 * 60


class Watch:
    def __init__(self, homes: List[str], enabled: bool = True) -> None:
        self.enabled = enabled
        self.homes = [h.lower() for h in homes if h]
        self._seen: Dict[str, List[dict]] = {}

    def mentions(self, message: str) -> int:
        """How many times this line names one of our rooms."""
        if not message:
            return 0
        low = message.lower()
        total = 0
        for home in self.homes:
            bare = home.lstrip("#")
            if not bare:
                continue
            total += len(re.findall(re.escape(f"#{bare}"), low))
        return total

    def hear(self, chan: str, nick: str, message: str, *,
             trusted: bool = False, abusive: bool = False,
             bad_nick: bool = False) -> Optional[dict]:
        """What we just heard in a FOREIGN room, and whether it matters."""
        if not self.enabled:
            return None
        count = self.mentions(message)
        if not count or trusted:
            return None

        spammed = count >= REPEAT_THRESHOLD
        nasty = bool(abusive or bad_nick)
        if not spammed and not nasty:
            # Remembered, but not worth waking anyone over.
            self.remember(nick, chan, "mentioned the room")
            return {"level": "watch", "why": "mentioned the room", "count": count}

        if nasty and spammed:
            why = "spamming the room name alongside abuse"
        elif nasty:
            why = "naming the room while being abusive"
        else:
            why = f"named the room {count}x in one line"
        self.remember(nick, chan, why)
        return {"level": "alert", "why": why, "count": count}

    def remember(self, nick: str, chan: str, why: str) -> None:
        k = nick.lower()
        now = time.time()
        hist = [s for s in self._seen.get(k, []) if now - s["at"] < MEMORY_SEC]
        hist.append({"at": now, "chan": chan, "why": why})
        self._seen[k] = hist

    def seen_in(self, nick: str) -> List[str]:
        now = time.time()
        hist = [s for s in self._seen.get(nick.lower(), []) if now - s["at"] < MEMORY_SEC]
        return list(dict.fromkeys(s["chan"] for s in hist))

    def is_flagged(self, nick: str) -> bool:
        return bool(self.seen_in(nick))

    def forget(self, nick: str) -> None:
        self._seen.pop(nick.lower(), None)

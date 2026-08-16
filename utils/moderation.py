"""Luna's auto-moderation — deliberately the GAPS, not a second word filter.

Dracula is the primary moderator in both rooms and already handles profanity
(English + Hinglish, leet-normalised), an AI severity layer, flood, caps,
repeats, raids, offensive nicks and ban masks. Luna repeating any of that would
mean two bots kicking the same person for the same line, which is worse than
either doing it alone.

So this covers only what a word list and a flood counter structurally cannot:

1. Homoglyph and zero-width evasion — Cyrillic "а" is not Latin "a", and a
   zero-width space between two letters defeats every substring check. This is
   the gap that matters most: it is how a determined person walks straight
   through Dracula.
2. Highlight spam — naming twenty people at once to make everyone's client
   beep. No individual word is offensive.
3. Control-code flooding — colour and blink codes that make a client unreadable.
   Classic IRC abuse that a text filter never sees.
4. Advertising — invites to other servers and networks.
5. Walls of text and join/part cycling.

Everything here is off unless $mod on. Nothing here escalates to a ban: the
worst outcome is a kick, and only after a warning for anyone with standing.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from fnmatch import fnmatch
from collections import deque
from typing import Dict, Optional, Tuple

import config

# ── Tunables ────────────────────────────────────────────────────────────────
MAX_HIGHLIGHTS = int(os.getenv("LUNA_MAX_HIGHLIGHTS", "6"))
MAX_LINE_LEN = int(os.getenv("LUNA_MAX_LINE", "400"))
MAX_CONTROL_CODES = int(os.getenv("LUNA_MAX_CONTROL", "8"))
CYCLE_LIMIT = int(os.getenv("LUNA_CYCLE_LIMIT", "4"))       # joins per window
CYCLE_WINDOW = int(os.getenv("LUNA_CYCLE_WINDOW", "60"))    # seconds
WARN_LIMIT = int(os.getenv("LUNA_WARN_LIMIT", "2"))

# Invites to elsewhere. Deliberately narrow: a link is not spam, an invite is.
_ADVERT = re.compile(
    r"(discord\.(gg|com/invite)/\S+|irc\.[a-z0-9-]+\.[a-z]{2,}|t\.me/\S+"
    r"|join\s+#\S+\s+on\s+\S+)", re.I)

# IRC formatting: colour, bold, italic, underline, reverse, reset.
_CONTROL = re.compile(r"[\x02\x0F\x11\x16\x1D\x1E\x1F]|\x03\d{0,2}(,\d{1,2})?")

# Zero-width and other invisible joiners used to split words apart.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿­]")


def strip_invisible(text: str) -> str:
    """Remove zero-width characters. `f<zwsp>u<zwsp>c<zwsp>k` is one word to a
    reader and five to a filter, which is exactly why people use it."""
    return _INVISIBLE.sub("", text or "")


def deconfuse(text: str) -> str:
    """Fold look-alike characters onto ASCII.

    NFKD handles fullwidth and enclosed forms (ＦＵＣＫ, 🅱). Cyrillic and Greek
    look-alikes survive that — "аdmin" with a Cyrillic а is a different string
    to every filter but identical to every reader — so they are mapped by hand.
    """
    text = unicodedata.normalize("NFKD", strip_invisible(text))
    out = []
    for ch in text:
        out.append(_HOMOGLYPHS.get(ch, ch))
    return "".join(out)


_HOMOGLYPHS = {
    # Cyrillic
    "а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "к": "k", "м": "m",
    "о": "o", "р": "p", "ѕ": "s", "т": "t", "у": "y", "х": "x", "і": "i",
    "ј": "j", "ԁ": "d", "ɡ": "g", "ν": "v", "ѡ": "w",
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "У": "Y", "Х": "X", "І": "I",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "Α": "A", "Β": "B", "Ε": "E", "Ο": "O",
    # misc look-alikes
    "ı": "i", "ǀ": "l", "ɑ": "a", "ᴏ": "o", "ѐ": "e",
}


class Moderator:
    """Stateful, per-channel. One instance per bridge."""

    def __init__(self, bridge) -> None:
        self.bridge = bridge
        self.enabled = _on(os.getenv("LUNA_MOD", "off"))
        self._warns: Dict[str, int] = {}
        self._joins: Dict[str, deque] = {}
        self._whitelist = {
            n.strip().lower()
            for n in os.getenv("LUNA_WHITELIST_IRC", "").split(",") if n.strip()
        }
        # Trust that follows the HOST. A regular who arrives under a different
        # nick each day cannot be covered by a nick list, however long it gets.
        self._trusted_masks = [
            m.strip() for m in os.getenv("TRUSTED_MASKS", "").split(",") if m.strip()
        ]
        # Never police the other bots or the services.
        self._never = {
            "chanserv", "nickserv", "chanbot", "dracula", "vampire", "luna1",
            config.IRC_NICK.lower(),
        }

    # ── helpers ────────────────────────────────────────────────────────────
    def _exempt(self, nick: str, channel: str) -> bool:
        n = nick.lower()
        if n in self._never or n in self._whitelist:
            return True
        if self._trusted_masks:
            host = ""
            try:
                host = self.bridge.host_of(nick)
            except Exception:
                pass
            if host and any(fnmatch(f"{nick}!{host}".lower(), m.lower())
                            for m in self._trusted_masks):
                return True
        # Channel operators are the humans in charge; never act on them.
        try:
            return self.bridge.has_prefix(channel, nick)
        except Exception:
            return False

    def _act(self, channel: str, nick: str, reason: str) -> None:
        """Warn, then kick on the second offence. Never bans — a false positive
        that can be undone by rejoining is a very different mistake to one that
        locks someone out."""
        key = f"{channel.lower()}|{nick.lower()}"
        n = self._warns.get(key, 0) + 1
        self._warns[key] = n
        if n >= WARN_LIMIT:
            self._warns.pop(key, None)
            self.bridge.kick_irc(nick, reason, channel)
            self.bridge._queue(channel, f"\x0306{nick} removed — {reason}.\x03")
        else:
            self.bridge._queue(
                channel, f"\x0306{nick}: {reason} ({n}/{WARN_LIMIT}).\x03")

    # ── checks ─────────────────────────────────────────────────────────────
    def check_message(self, channel: str, nick: str, text: str) -> bool:
        """Returns True if Luna acted (caller should stop processing)."""
        if not self.enabled or self._exempt(nick, channel):
            return False

        # 1. Evasion. Only flag when folding the text CHANGES it and the folded
        #    form hits a severe word — otherwise ordinary non-English writing
        #    would be punished for existing.
        folded = deconfuse(text)
        if folded != text and _severe_hit(folded):
            self._act(channel, nick, "disguised abuse")
            return True

        # 2. Highlight spam — how many people were named at once.
        members = set()
        try:
            members = {m.lower() for m in self.bridge.get_channel_nicks(channel)}
        except Exception:
            pass
        if members:
            named = {w.strip(",:;@").lower() for w in text.split()} & members
            if len(named) > MAX_HIGHLIGHTS:
                self._act(channel, nick, f"pinged {len(named)} people at once")
                return True

        # 3. Control-code flooding.
        if len(_CONTROL.findall(text)) > MAX_CONTROL_CODES:
            self._act(channel, nick, "colour-code flooding")
            return True

        # 4. Advertising another server.
        if _ADVERT.search(text):
            self._act(channel, nick, "advertising")
            return True

        # 5. Wall of text in one line.
        if len(text) > MAX_LINE_LEN:
            self._act(channel, nick, "wall of text")
            return True

        return False

    def check_join(self, channel: str, nick: str) -> bool:
        """Join/part cycling: rejoining repeatedly to spam the room's join
        messages. The raid guard counts DISTINCT users, so one person doing it
        alone never trips it."""
        if not self.enabled or self._exempt(nick, channel):
            return False
        key = f"{channel.lower()}|{nick.lower()}"
        now = time.time()
        q = self._joins.setdefault(key, deque())
        q.append(now)
        while q and now - q[0] > CYCLE_WINDOW:
            q.popleft()
        if len(q) > CYCLE_LIMIT:
            q.clear()
            self.bridge.kick_irc(nick, "join/part flooding", channel)
            return True
        return False


def _on(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Severe words live in a secret, never in this public repo. Without it, the
# evasion check simply never fires — which is the safe direction to fail.
_SEVERE = {
    w.strip().lower()
    for w in os.getenv("LUNA_SEVERE_WORDS", "").split(",") if w.strip()
}


def _severe_hit(text: str) -> Optional[str]:
    if not _SEVERE:
        return None
    flat = re.sub(r"[^a-z]", "", text.lower())
    for w in _SEVERE:
        if len(w) >= 4 and w in flat:
            return w
    return None

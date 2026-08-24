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
import threading
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
# Matches Dracula's DEVOICE_MINUTES so the room behaves the same way
# whichever bot happens to be awake.
DEVOICE_MINUTES = int(os.getenv("DEVOICE_MINUTES", "2"))

# Invites to elsewhere. Deliberately narrow: a link is not spam, an invite is.
_ADVERT = re.compile(
    r"(discord\.(gg|com/invite)/\S+|irc\.[a-z0-9-]+\.[a-z]{2,}|t\.me/\S+"
    r"|join\s+#\S+\s+on\s+\S+)", re.I)

# IRC formatting: colour, bold, italic, underline, reverse, reset.
_CONTROL = re.compile(r"[\x02\x0F\x11\x16\x1D\x1E\x1F]|\x03\d{0,2}(,\d{1,2})?")

# Ordinary profanity, used only when standing in for an absent peer. Kept in a
# secret rather than in this public repo, and empty by default — with no list,
# failover simply falls back to the gap checks, which is the safe direction.
_BADWORDS = {
    w.strip().lower() for w in os.getenv("BADWORDS", "").split(",") if w.strip()
}

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
        # The primary moderator. Luna covers only the gaps while it is here,
        # and the whole job while it is not.
        self._peer = os.getenv("PEER_BOT", "Dracula")

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
        """Take the VOICE first, the seat only if it keeps happening.

        Both rooms run +m with everyone auto-voiced, so a de-voice is the
        punishment the room is actually built around: the offender goes quiet
        while everyone else carries on, and it costs them two minutes rather
        than their place. Luna used to jump straight from a spoken warning to a
        kick, which meant that whenever she was standing in for an absent
        Dracula the room silently became harsher than when he is there — the
        opposite of what covering for someone should mean.

        Never bans. A false positive undone by rejoining is a very different
        mistake to one that locks somebody out.
        """
        key = f"{channel.lower()}|{nick.lower()}"
        n = self._warns.get(key, 0) + 1
        self._warns[key] = n

        if n < WARN_LIMIT:
            self.bridge.send_raw(f"MODE {channel} -v {nick}")
            self.bridge._queue(
                channel,
                f"\x0306{nick}: {reason} — voice back in {DEVOICE_MINUTES}m "
                f"({n}/{WARN_LIMIT}).\x03")
            threading.Timer(
                DEVOICE_MINUTES * 60,
                self._restore_voice, args=(channel, nick),
            ).start()
            return

        self._warns.pop(key, None)
        self.bridge.kick_irc(nick, reason, channel)
        self.bridge._queue(channel, f"\x0306{nick} removed — {reason}.\x03")

    def _restore_voice(self, channel: str, nick: str) -> None:
        """Give the voice back, but only to somebody still in the room."""
        try:
            if self.bridge.is_nick_in_channel(nick, channel):
                self.bridge.send_raw(f"MODE {channel} +v {nick}")
        except Exception as e:
            print(f"[moderation] could not restore voice for {nick}: {e}")

    # ── failover ───────────────────────────────────────────────────────────
    def _peer_present(self, channel: str) -> bool:
        """Is the primary moderator actually in this room right now?

        Two bots enforcing the same rule is worse than either alone, so Luna
        normally stays on the gaps. But a bot that is offline moderates
        nothing, and this one restarts every six hours and occasionally lands
        on a blocked address — so when the peer is absent she has to be able to
        do the whole job rather than politely deferring to nobody.
        """
        try:
            here = {n.lower() for n in self.bridge.get_channel_nicks(channel)}
        except Exception:
            return True          # unsure: assume covered, stay on the gaps
        return self._peer.lower() in here

    def _word_hit(self, text: str) -> Optional[str]:
        """The plain word filter, used ONLY while standing in for the peer."""
        if not _BADWORDS:
            return None
        flat = re.sub(r"[^a-z ]", " ", deconfuse(text).lower())
        for w in flat.split():
            if w in _BADWORDS:
                return w
        return None

    # ── checks ─────────────────────────────────────────────────────────────
    def check_message(self, channel: str, nick: str, text: str) -> bool:
        """Returns True if Luna acted (caller should stop processing)."""
        if not self.enabled or self._exempt(nick, channel):
            return False

        # Standing in: the peer is gone, so the ordinary word filter is hers
        # too until it comes back.
        if not self._peer_present(channel):
            hit = self._word_hit(text)
            if hit:
                self._act(channel, nick, "language")
                return True

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

    def screen_relay(self, text: str) -> Optional[str]:
        """Should this Discord message be withheld from IRC?

        The bridge is a hole in the moderation, and it was wide open: Luna is
        opped in both rooms, Dracula never moderates channel operators, and
        every Discord line was relayed verbatim under her nick. Anyone on the
        far side could put anything into the channel and nothing would touch
        it.

        Screening happens HERE because it is the only place it can. Dracula
        cannot act on a person who is not in the room, and warning them is
        meaningless — the only proportionate response to something that should
        not be said in the room is to not carry it there.

        Ordinary profanity still passes: an IRC user only gets a warning for
        that, so blocking it from Discord would be harsher than the rule it is
        mirroring. Severe content does not pass at all.
        """
        if not text:
            return None
        folded = deconfuse(text)
        hit = _severe_hit(folded) or _severe_hit(text)
        if hit:
            return "severe language"
        if len(_CONTROL.findall(text)) > MAX_CONTROL_CODES:
            return "control-code flooding"
        if _ADVERT.search(text):
            return "advertising"
        return None

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

"""
Shared command dispatcher for Luna.

Works for BOTH Discord and IRC. Owner/whitelist gated.

Commands: help, ai, ping, roll, flip, choose, calc, weather, nicks, say, diag

Deliberately short. Canned-list commands (8ball, fact, dadjoke, quote) were
removed: a fixed list repeats within minutes and stops being funny. batstatus
went with them — Dracula covers the room now — and remind, because it lived in
memory on a host that restarts the process every six hours.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

import config


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _csv_env(key: str) -> set:
    raw = os.getenv(key, "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


OWNERS_IRC = _csv_env("LUNA_OWNERS_IRC")
WHITELIST_IRC = _csv_env("LUNA_WHITELIST_IRC")
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",")
    if x.strip().isdigit()
}


def is_irc_authorized(nick: str) -> bool:
    if not (OWNERS_IRC or WHITELIST_IRC):
        return True  # open if unset
    n = nick.lower()
    return n in OWNERS_IRC or n in WHITELIST_IRC


def is_discord_authorized(user_id: int) -> bool:
    if not OWNER_IDS:
        return True
    return user_id in OWNER_IDS


# ── Data ─────────────────────────────────────────────────────────────────────






# ── SharedCommands singleton ─────────────────────────────────────────────────

class SharedCommands:
    _instance: Optional["SharedCommands"] = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, bot=None, bridge=None) -> "SharedCommands":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(bot, bridge)
            else:
                if bot is not None:
                    cls._instance.bot = bot
                if bridge is not None:
                    cls._instance.bridge = bridge
            return cls._instance

    def __init__(self, bot, bridge):
        self.bot = bot
        self.bridge = bridge

    # ── Dispatch entry points ────────────────────────────────────────────

    def dispatch_irc(self, nick: str, text: str) -> Optional[str]:
        """Parse a prefixed command from an IRC PRIVMSG. Reply text or None.

        The prefix comes from config: hardcoding it here meant every command
        silently stopped working the moment the prefix changed, with no error
        anywhere — the bot simply ignored people.
        """
        pfx = config.PREFIX
        if not text.startswith(pfx):
            return None
        parts = text[len(pfx):].split(None, 1)
        if not parts:
            return None
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if not is_irc_authorized(nick):
            return None  # silent deny on IRC
        return self._run("irc", nick, None, cmd, args)

    def dispatch_discord(
        self,
        user_id: int,
        username: str,
        cmd: str,
        args: str,
    ) -> Optional[str]:
        if not is_discord_authorized(user_id):
            return "*Luna narrows her eyes.* Not for you, darling."
        return self._run("discord", username, user_id, cmd, args)

    def _run(self, platform: str, name: str, user_id, cmd: str, args: str) -> Optional[str]:
        method = getattr(self, f"cmd_{cmd}", None)
        if method is None:
            return None
        try:
            return method(platform, name, args)
        except Exception as e:
            return f"[error] {e}"

    # ── Commands ─────────────────────────────────────────────────────────

    def cmd_ping(self, platform, name, args):
        return f"pong! ({platform})"

    def cmd_help(self, platform, name, args):
        """Everything Luna answers to, with the live prefix baked in.

        Kept short and split by topic: this is read in a chat window, often on
        a phone, where a long block scrolls the conversation away.
        """
        p = config.PREFIX
        sub = (args or "").strip().lower()
        if sub in ("fun", "games"):
            return (
                f"[\x02Fun\x02] {p}roll [NdN] · {p}flip · {p}choose a, b, c · "
                f"{p}calc 5 x 89 · {p}weather [city]"
            )
        if sub in ("irc", "bridge"):
            return (
                f"[\x02Bridge\x02] {p}ping · {p}nicks who is here · "
                f"{p}say <msg> cross-post"
            )
        # The first line states what Luna is. HybridIRC's relay policy asks
        # that relay bots be clearly identified and that users know their
        # messages leave the channel — and it is simply fair warning.
        return (
            f"\x02Luna\x02 — I bridge this room to a linked room elsewhere; "
            f"what you type here is relayed, and replies come back tagged. "
            f"(prefix \x02{p}\x02) — "
            f"Talk to me: just say my name, or {p}ai <question> · "
            f"Fun: {p}roll {p}flip {p}choose {p}calc {p}weather · "
            f"Bridge: {p}ping {p}nicks {p}say · "
            f"More: {p}help fun | {p}help bridge"
        )


    def cmd_roll(self, platform, name, args):
        expr = (args or "1d6").strip()
        m = re.match(r"^(\d+)d(\d+)$", expr)
        if not m:
            return f"Usage: {config.PREFIX}roll 2d6"
        count, sides = int(m.group(1)), int(m.group(2))
        if count <= 0 or sides <= 0 or count > 20 or sides > 1000:
            return "Limits: 1-20 dice, 1-1000 sides."
        rolls = [random.randint(1, sides) for _ in range(count)]
        return f"dice {rolls} = {sum(rolls)}"

    def cmd_calc(self, platform, name, args):
        expr = (args or "").strip()
        if not expr:
            return f"Usage: {config.PREFIX}calc 2+2"
        # "5 x 89" is how people actually write multiplication in chat.
        expr = re.sub(r"(?<=[\d\s)])[xX](?=[\d\s(])", "*", expr)
        if not re.match(r"^[0-9+\-*/().\s]+$", expr):
            return "Only numbers and + - * / ( ) allowed."
        try:
            # safe: regex above restricts chars
            return f"= {eval(expr, {'__builtins__': {}}, {})}"
        except Exception:
            return "Bad expression."




    def cmd_choose(self, platform, name, args):
        options = [x.strip() for x in (args or "").split(",") if x.strip()]
        if len(options) < 2:
            return f"Usage: {config.PREFIX}choose a, b, c"
        return f"I choose: {random.choice(options)}"

    def cmd_flip(self, platform, name, args):
        return random.choice(["Heads", "Tails"])

    def cmd_weather(self, platform, name, args):
        city = (args or "").strip() or "Delhi"
        try:
            url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=3"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode("utf-8").strip()
        except Exception as e:
            return f"Weather error: {e}"

    def cmd_nicks(self, platform, name, args):
        if not self.bridge:
            return "IRC bridge not attached."
        nicks = sorted(self.bridge.get_channel_nicks())
        if not nicks:
            return "IRC channel empty."
        return f"IRC ({len(nicks)}): " + ", ".join(nicks)

    def cmd_say(self, platform, name, args):
        """Cross-post: from IRC -> Discord, from Discord -> IRC."""
        text = (args or "").strip()
        if not text:
            return f"Usage: {config.PREFIX}say <message>"
        text = text[:300]
        if platform == "irc":
            if self.bridge and self.bridge.loop and self.bridge.loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._post_discord(f"**[{name}@IRC]** {text}"),
                    self.bridge.loop,
                )
            return "relayed to Discord."
        else:
            if self.bridge:
                self.bridge.send_to_irc(f"[{name}] {text}")
            return None

    async def _post_discord(self, text: str) -> None:
        if not self.bot:
            return
        target = getattr(config, "BRIDGE_CHANNEL", "").lower()
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == target:
                    try:
                        await ch.send(text[:1900])
                    except Exception:
                        pass
                    return











    def cmd_diag(self, platform, name, args):
        """Report what Luna can actually see on the far side of the bridge.

        The picture commands were timing out with no way to tell why: from IRC,
        "the other bot is not in the channel", "it ignored me" and "it is slow"
        all look identical. This answers the first one directly.
        """
        bot = self.bot
        if bot is None:
            return "no Discord connection object — bridge is running standalone."

        lines = []
        user = getattr(bot, "user", None)
        lines.append(f"logged in as {user}" if user else "NOT logged in to Discord")

        try:
            mapping = dict(self.bridge._d2i) if self.bridge else {}
        except Exception:
            mapping = {}
        lines.append("bridges: " + (", ".join(f"#{d}<->{i}" for d, i in mapping.items())
                                    or "none"))

        target = (getattr(config, "BRIDGE_CHANNEL", "") or "").lower()
        found = None
        for guild in getattr(bot, "guilds", []):
            for ch in getattr(guild, "text_channels", []):
                if ch.name.lower() == target:
                    found = ch
                    break
        if found is None:
            lines.append(f"channel '{target}' NOT FOUND in any guild Luna is in")
            return " | ".join(lines)

        perms = found.permissions_for(found.guild.me) if found.guild.me else None
        lines.append(f"channel #{found.name}: "
                     f"send={getattr(perms, 'send_messages', '?')} "
                     f"read={getattr(perms, 'read_messages', '?')}")

        # The decisive fact: is the bot we are sending commands to even here?
        bots = [m.name for m in getattr(found, "members", []) if getattr(m, "bot", False)]
        lines.append(f"bots in that channel ({len(bots)}): "
                     + (", ".join(bots[:12]) or "NONE — nothing can answer a command"))
        return " | ".join(lines)

    # ── Reminder loop ────────────────────────────────────────────────────



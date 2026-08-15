"""
Shared command dispatcher for Luna.

Works for BOTH Discord and IRC. Owner/whitelist gated.

Commands: ping, help, 8ball, roll, calc, fact, dadjoke, quote,
          choose, flip, weather, nicks, say, remind, batstatus
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

_8BALL = [
    "Yes.", "No.", "Absolutely.", "Doubtful.", "Ask again later.",
    "The stars say yes.", "The moon says no.", "Without a doubt.",
    "Very unlikely.", "Signs point to yes.", "My sources say no.",
    "Concentrate and ask again.", "Outlook not so good.", "It is certain.",
]

_FACTS = [
    "Octopuses have three hearts and blue blood.",
    "Bananas are berries but strawberries are not.",
    "A day on Venus is longer than its year.",
    "Honey never spoils.",
    "Sharks existed before trees.",
    "Wombats produce cube-shaped poop.",
    "Your stomach gets a new lining every 3-4 days.",
    "The Eiffel Tower grows up to 6 inches taller in summer.",
    "Bananas are slightly radioactive.",
    "A group of flamingos is called a flamboyance.",
]

_DAD_JOKES = [
    "Why don't scientists trust atoms? They make up everything.",
    "I told my wife she drew her eyebrows too high. She looked surprised.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I used to hate facial hair, but then it grew on me.",
    "Did you hear about the mathematician who's afraid of negative numbers? He stops at nothing to avoid them.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
    "I would tell you a construction joke, but I'm still working on it.",
]

_QUOTES = [
    "The only way out is through. - Robert Frost",
    "Not all those who wander are lost. - Tolkien",
    "We are all in the gutter, but some of us are looking at the stars. - Wilde",
    "The night is darkest just before the dawn.",
    "Even the darkest night will end and the sun will rise. - Hugo",
]


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
        self._reminders: list = []
        self._reminders_lock = threading.Lock()
        self._reminder_thread_started = False
        self._start_reminder_thread()

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
                f"[\x02Fun\x02] {p}8ball <q> · {p}roll [NdN] · {p}flip · "
                f"{p}choose a, b, c · {p}fact · {p}dadjoke · {p}quote · "
                f"{p}weather [city] · {p}calc 2+2"
            )
        if sub in ("irc", "bridge"):
            return (
                f"[\x02Bridge\x02] {p}ping · {p}nicks who is here · "
                f"{p}say <msg> cross-post · {p}remind 10m <task> · {p}batstatus"
            )
        if sub in ("all", "full", "list"):
            # The picture commands live in the bridge's allowlist; import it
            # lazily so the two modules don't import each other at load time.
            try:
                from utils.irc_bridge import _allowed_notsobot_cmds
                every = ", ".join(sorted(_allowed_notsobot_cmds()))
            except Exception:
                every = "(unavailable)"
            return f"[\x02All pictures/search\x02] {every}"
        if sub in ("img", "image", "pics"):
            return (
                f"[\x02Pictures\x02] {p}img <words> · {p}magik <url> · "
                f"{p}edit <url> · {p}deepfry <url> · {p}caption <url> <text> — "
                f"{p}help all for every one"
            )
        # The first line states what Luna is. HybridIRC's relay policy asks
        # that relay bots be clearly identified and that users know their
        # messages leave the channel — and it is simply fair warning.
        return (
            f"\x02Luna\x02 — I bridge this room to a linked room elsewhere; "
            f"what you type here is relayed, and replies come back tagged. "
            f"(prefix \x02{p}\x02) — "
            f"Fun: {p}8ball {p}roll {p}flip {p}choose {p}fact {p}dadjoke {p}quote {p}weather · "
            f"Bridge: {p}ping {p}nicks {p}say {p}remind {p}batstatus · "
            f"Pictures: {p}img {p}magik {p}edit · "
            f"More: {p}help fun | {p}help bridge | {p}help img | {p}help all"
        )

    def cmd_8ball(self, platform, name, args):
        if not args.strip():
            return "Ask a question, darling."
        return random.choice(_8BALL)

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
        if not re.match(r"^[0-9+\-*/().\s]+$", expr):
            return "Only numbers and + - * / ( ) allowed."
        try:
            # safe: regex above restricts chars
            return f"= {eval(expr, {'__builtins__': {}}, {})}"
        except Exception:
            return "Bad expression."

    def cmd_fact(self, platform, name, args):
        return random.choice(_FACTS)

    def cmd_dadjoke(self, platform, name, args):
        return random.choice(_DAD_JOKES)

    def cmd_quote(self, platform, name, args):
        return random.choice(_QUOTES)

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

    def cmd_remind(self, platform, name, args):
        m = re.match(r"^(\d+)([mh])\s+(.+)$", (args or "").strip())
        if not m:
            return f"Usage: {config.PREFIX}remind 10m take pizza out"
        n, unit, text = int(m.group(1)), m.group(2), m.group(3)
        secs = n * (60 if unit == "m" else 3600)
        if secs > 86400:
            return "Max 24h."
        due = time.time() + secs
        with self._reminders_lock:
            self._reminders.append((due, name, platform, text))
        return f"reminder set for {n}{unit}: {text}"

    # ── Vikram-GF persona & memory commands ──────────────────────────────








    def cmd_batstatus(self, platform, name, args):
        url = getattr(config, "BATBOT_REPLIT_URL", "")
        bat_nick = getattr(config, "BATBOT_IRC_NICK", "Vampire")
        http_ok = False
        if url:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    http_ok = r.status == 200
            except Exception:
                http_ok = False
        irc_ok = False
        if self.bridge:
            try:
                irc_ok = self.bridge.is_nick_in_channel(bat_nick)
            except Exception:
                irc_ok = False
        status = "ALIVE" if (http_ok and irc_ok) else "DOWN"
        return f"BatBot: {status}  (http={http_ok}, irc={irc_ok})"

    # ── Reminder loop ────────────────────────────────────────────────────

    def _start_reminder_thread(self) -> None:
        if self._reminder_thread_started:
            return
        self._reminder_thread_started = True
        t = threading.Thread(target=self._reminder_loop, daemon=True, name="luna-reminders")
        t.start()

    def _reminder_loop(self) -> None:
        while True:
            time.sleep(5)
            now = time.time()
            fired: list = []
            with self._reminders_lock:
                kept = []
                for r in self._reminders:
                    if r[0] <= now:
                        fired.append(r)
                    else:
                        kept.append(r)
                self._reminders = kept
            for _due, who, plat, text in fired:
                try:
                    if plat == "discord":
                        if self.bridge and self.bridge.loop and self.bridge.loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                self._post_discord(f"reminder for {who}: {text}"),
                                self.bridge.loop,
                            )
                    else:
                        if self.bridge:
                            self.bridge.send_to_irc(f"reminder for {who}: {text}")
                except Exception as e:
                    print(f"[reminder] error firing: {e}")

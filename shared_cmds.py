"""
Shared command dispatcher for Luna.

Works for BOTH Discord and IRC. Owner/whitelist gated.

Commands: help, ai, ping, roll, flip, choose, calc, weather, nicks, say, mod

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
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",")
    if x.strip().isdigit()
}


def is_irc_owner(nick: str, bridge=None, channel: str = "") -> bool:
    """Configured owners, or a channel operator.

    An empty LUNA_OWNERS_IRC must not mean "everyone" for a command that
    decides whether people get kicked — but it must not mean "nobody" either,
    which is what happened live: $mod answered with silence because the secret
    was never set. Falling back to channel ops is self-configuring and matches
    who already holds that authority in the room.
    """
    if nick.lower() in OWNERS_IRC:
        return True
    if bridge is not None and channel:
        try:
            return bridge.has_prefix(channel, nick)
        except Exception:
            return False
    return False


def is_irc_authorized(nick: str) -> bool:
    """Who may use the ORDINARY commands: everyone.

    This used to mean "owners and whitelist only, unless neither is
    configured". Setting LUNA_OWNERS_IRC therefore switched $help, $ping,
    $roll and the rest OFF for the entire room, silently — the dispatcher
    returned None and the bot simply never answered. A list of who is in charge
    must not double as a list of who is allowed to speak to the bot.

    Restricted commands do their own checking; $mod asks is_irc_owner.
    """
    return True


def is_discord_authorized(user_id: int) -> bool:
    if not OWNER_IDS:
        return True
    return user_id in OWNER_IDS


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
        self._channel = ""

    # ── Dispatch entry points ────────────────────────────────────────────

    def dispatch_irc(self, nick: str, text: str, channel: str = "") -> Optional[str]:
        """Parse a prefixed command from an IRC PRIVMSG. Reply text or None.

        The prefix comes from config: hardcoding it here meant every command
        silently stopped working the moment the prefix changed, with no error
        anywhere — the bot simply ignored people.
        """
        pfx = config.PREFIX
        if not text.startswith(pfx):
            return None
        # ANOTHER BOT'S COMMAND. The standbys use "$$" and Luna uses "$", so
        # "$$trivia" parses here as prefix + "$trivia" — unknown. That was
        # harmless while unknown commands were silent; the moment they started
        # answering, Luna began interrupting every single standby command with
        # "I do not know $$trivia". Caught in a live test within the hour.
        if text[len(pfx):].startswith(pfx):
            return None
        parts = text[len(pfx):].split(None, 1)
        if not parts:
            return None
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if not is_irc_authorized(nick):
            return None  # silent deny on IRC
        self._channel = channel
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

    # Commands that exist only as Discord cogs. Typed on IRC they used to do
    # NOTHING — no reply, no error, no log — so 48 of the bot's 58 commands
    # were silently dead there and the only way to find out was to ask a human
    # why the bot ignored you.
    # Every Discord command IRC cannot reach. This is the FALLBACK used when
    # there is no live bot to ask (tests, and the window before the cogs
    # finish loading) — elsewhere_on_discord() prefers the real thing.
    # test_help_audit.py fails if this drifts out of date again, which is how
    # $find came to be answered with "I do not know $find".
    DISCORD_ONLY = {
        "about", "activity", "ai", "ask", "batcheck", "batstatus", "bridges",
        "bs", "clearwarns", "commands", "compat", "confess", "dare", "deop",
        "devoice", "find", "flirt", "gossip", "h", "ircban", "ircbridges",
        "ircinfo", "ircjoin", "irckick", "ircleave", "ircnick", "ircnicks",
        "ircpart", "ircping", "ircraw", "ircreconnect", "irctopic",
        "ircunban", "ircwho", "luna", "match", "memo", "mood", "mute", "op",
        "record", "regulars", "relay", "search", "secret", "seduce", "seen",
        "sendregulars", "ship", "slowmode", "spill", "stats", "tea", "tell",
        "to", "tod", "truth", "truthordare", "unmute", "unwarn", "vibe",
        "vibecheck", "voice", "warn", "warnings", "warns", "who",
        "s",
    }

    def irc_command_names(self):
        """What IRC can actually reach — read off the methods, not a list.

        The "On IRC I answer: ..." line used to be typed out by hand, so it was
        a promise nobody re-checked. Reading the cmd_ methods means it cannot
        advertise something that is not there, or omit something that is.
        """
        return sorted(m[4:] for m in dir(self) if m.startswith("cmd_"))

    def elsewhere_on_discord(self, cmd: str) -> bool:
        """Does this command exist, just not here?

        DISCORD_ONLY was a hand-written set, and it drifted the moment new
        commands landed: $find, $tell, $stats and $mood were all answered on
        IRC with "I do not know $find" — which is not true. It exists; it is
        somewhere else, and those are completely different things to be told
        when you are trying to work out whether the bot is broken.

        So ask the live bot what it registered. A list maintained by hand
        describes what somebody remembered; the bot describes what is.
        DISCORD_ONLY stays as the answer when there is no bot to ask, which is
        the case in tests and before the cogs finish loading.
        """
        bot = getattr(self, "bot", None)
        if bot is None:
            return cmd in self.DISCORD_ONLY
        try:
            names = set()
            for c in bot.commands:
                names.add(c.name)
                names.update(c.aliases or ())
            if not names:                      # cogs not loaded yet
                return cmd in self.DISCORD_ONLY
            return cmd in names
        except Exception:
            return cmd in self.DISCORD_ONLY

    def _run(self, platform: str, name: str, user_id, cmd: str, args: str) -> Optional[str]:
        """`_channel` is set by the caller so a command can check op status in
        the room it was typed in."""
        method = getattr(self, f"cmd_{cmd}", None)
        if method is None:
            # Say something. An unrecognised command that produces silence is
            # indistinguishable from a broken bot, and that is exactly how this
            # was reported: "most commands did not work".
            if platform == "irc":
                p = config.PREFIX
                here = " ".join(f"{p}{n}" for n in self.irc_command_names())
                if self.elsewhere_on_discord(cmd):
                    return (f"{p}{cmd} works from Discord, not from here. "
                            f"On IRC I answer: {here}")
                return f"I do not know {p}{cmd}. Try {p}help."
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
        if sub in ("mod", "moderation"):
            # Operators only. The owner asked that ordinary users neither use the
            # mod commands nor "get to know them", so a non-op asking for the mod
            # help is told it is not theirs rather than shown the list. The
            # commands are already gated for USE; this hides them from view too.
            if not is_irc_owner(name, self.bridge, self._channel):
                return "That half is for operators."
            return (
                f"[\x02Moderation\x02] From Discord, acting on the room you are "
                f"pointed at ({p}to): "
                f"{p}op {p}deop {p}voice {p}devoice <nick> · "
                f"{p}mute {p}unmute <nick> · "
                f"{p}irckick <nick> [reason] · {p}ircban {p}ircunban <nick> · "
                f"{p}ircwho who is in the room. "
                f"{p}nsfw on|off — set a room to adult mode (writes the 18+ notice "
                f"into its topic). "
                f"Automatic: {p}mod on|off — I cover what Dracula cannot see: "
                f"disguised text, mass pings, colour flooding, adverts, walls "
                f"of text, join flooding. Warn first, kick second, never a ban."
            )
        if sub in ("nsfw", "adult", "afterdark"):
            return (
                f"[\x02Adult\x02] Only in rooms an operator set to adult mode "
                f"(the topic will say {chr(0x1F51E)}, and being in the room is your "
                f"agreement). {p}afterdark · {p}tempt {p}spicy {p}fantasy "
                f"{p}midnight {p}desire <nick>. Don't want to be involved? "
                f"{p}boundaries and I leave you out — {p}boundaries off to rejoin."
            )
        if sub in ("memory", "mem", "history"):
            # These read the relayed history, so they are the one thing Luna can
            # do that IRC itself cannot: the room has no scrollback and she has
            # weeks of it.
            return (
                f"[\x02Memory\x02] I remember what this room has said. "
                f"{p}find <text> — search it · "
                f"{p}quote [nick] — something somebody actually said · "
                f"{p}onthisday [days] — what was being said a week ago · "
                f"{p}tell <nick> <message> — I pass it on when they next speak · "
                f"{p}stats — who talks here and when · "
                f"{p}seen <nick> · {p}mood"
            )
        if sub in ("irc", "bridge"):
            # $to matters most here. Two IRC rooms feed one Discord channel, so
            # without it everything typed in Discord goes to whichever room was
            # configured first and the other is unreachable — which is exactly
            # what happened to #batcave.
            return (
                f"[\x02Bridge\x02] {p}to <#room> — choose which IRC room this "
                f"Discord channel talks to; {p}to alone shows where messages go. "
                f"Or start one message with a room name to send just that line "
                f"there. Also: {p}ping · {p}nicks who is here · {p}say <msg> "
                f"cross-post · {p}post <text> — place it in Discord VERBATIM, which "
                f"{p}say cannot (it decorates), so it is the only way to test "
                f"whether another bot there answers a bot at all (ops only) · "
                f"{p}ircbridges {p}ircjoin {p}ircleave {p}irctopic "
                f"{p}ircnicks {p}ircreconnect"
            )
        # The first line states what Luna is. HybridIRC's relay policy asks
        # that relay bots be clearly identified and that users know their
        # messages leave the channel — and it is simply fair warning.
        return (
            f"\x02Luna\x02 — I bridge this room to a linked room elsewhere; "
            f"what you type here is relayed, and replies come back tagged. "
            f"(prefix \x02{p}\x02) — "
            f"Talk to me: just say my name, or {p}ai <question> · "
            f"Memory: {p}find {p}quote {p}onthisday {p}tell {p}stats · "
            f"Fun: {p}roll {p}flip {p}choose {p}calc {p}weather · "
            f"Bridge: {p}ping {p}nicks {p}say · "
            # No mod command names here: the top line is what everyone sees, and
            # ordinary users should not learn the moderation surface from it.
            # $help mod carries them, and only shows them to operators.
            f"More: {p}help memory | {p}help fun | {p}help bridge | {p}help nsfw"
            + (f" | {p}help mod" if is_irc_owner(name, self.bridge, self._channel) else "")
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

    def cmd_mod(self, platform, name, args):
        """$mod on|off — Luna's auto-moderation.

        Owner-gated: this decides whether people get kicked, so it must not be
        something any passer-by can flip. Off by default, because a brand-new
        moderator loose in a live room is how a regular gets thrown out
        mid-joke.
        """
        if platform == "irc" and not is_irc_owner(name, self.bridge, self._channel):
            return "that one is for channel operators."
        mod = getattr(self.bridge, "moderator", None)
        if mod is None:
            return "moderation is not loaded."
        arg = (args or "").strip().lower()
        if arg in ("on", "enable"):
            mod.enabled = True
            return ("auto-moderation ON — I cover what Dracula misses: "
                    "disguised text, mass pings, colour flooding, adverts, "
                    "walls of text, join flooding.")
        if arg in ("off", "disable"):
            mod.enabled = False
            return "auto-moderation OFF."
        return f"auto-moderation is {'ON' if mod.enabled else 'OFF'}."

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

    def cmd_post(self, platform, name, args):
        """Put text into the bridged Discord channel EXACTLY as typed.

        $say decorates what it sends — "**[vikram@IRC]** !weather london" — and a
        prefix bot needs its prefix at the very start of the line, so $say can
        never trigger one. This does not decorate.

        It is NOT a way around Discord's rule that bots cannot invoke each
        other's commands: this still arrives from a bot account with author.bot
        set, so any bot that filters bot authors ignores it just the same. What
        it settles is whether the bots in this server filter AT ALL — plenty
        never added the guard. One message answers a question nobody has
        actually asked yet.

        Owners and channel operators only. A command that makes the bot say
        arbitrary text is a way to wear its voice, and this room has been
        attacked by people wearing other people's names.
        """
        if platform == "irc" and not is_irc_owner(name, self.bridge, self._channel):
            return None                      # silent, like the other gated ones
        text = (args or "").strip()
        if not text:
            return f"Usage: {config.PREFIX}post <exact text to place in Discord>"
        if text.startswith(config.PREFIX):
            return "That is my own prefix — it would only talk to me."
        text = text[:300]
        # Named in the LOG, not in the message: the text stays verbatim so it can
        # trigger a prefix bot, but the action is never anonymous.
        print(f"[shared_cmds] $post by {name} ({platform}): {text[:120]}")
        if platform == "irc":
            if self.bridge and self.bridge.loop and self.bridge.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._post_discord(text), self.bridge.loop)
                return "placed in Discord verbatim."
            return "Discord is not reachable right now."
        if self.bridge:
            self.bridge.send_to_irc(text)
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

"""
Who turns up and talks — counted out of the history Luna already keeps.

Dracula decides who is a trusted regular in #batcave, and its automatic
promotion has never once fired. Not because it is broken: because its evidence
is amnesiac. It wants 40 messages and counts them in memory, and GitHub Actions
hands the job over roughly every six hours, so the count starts again from zero
before anybody ordinary could reach it. The owner described the people he wants
trusted as the ones who "talk normally everyday" — which is precisely the
pattern that can never accumulate. Fifteen lines a day for a fortnight scores
fifteen, forever; one noisy afternoon scores forty and qualifies.

Luna is the half of this pair that remembers. Every line of the room is relayed
into Discord as ``**[room]** `nick`: message`` and Discord keeps it, so weeks of
"who spoke, on which day" is already sitting there being used for nothing. This
reads it back, counts the DAYS each nick was heard on, and hands the tally to
Dracula.

Three things this deliberately does not do:

**It does not decide anything.** The report is evidence — "this nick was heard on
nine separate days" — and Dracula applies every gate afterwards: a registered
services account, the account's age, its own deny list. Luna watches a relay,
where a nick is just text somebody typed; it cannot tell aishwarya from whoever
took her name this morning. Only Dracula can ask the network who somebody is.

**It does not travel unsigned.** "Here is the list of people you should trust" is
the most useful message an attacker on this network could forge, so the report
carries an HMAC over a body that includes its own timestamp, using the same
shared secret the bots already use to recognise each other. No secret configured
means no report sent at all — never a report anybody could have written.

**It does not read DMs.** Only the bridged channels, which are relays of a public
room. Luna has never seen DM traffic and this does not change that.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config

# The shape irc_bridge.py writes into Discord. Anchored at the start so a
# message that merely quotes the format cannot inject a speaker.
_RELAY = re.compile(r"^\*\*\[(?P<room>[^\]]{1,64})\]\*\*\s+`(?P<nick>[^`]{1,32})`:")
# What Dracula's attendance.js will accept as a nick. Anything else is dropped
# rather than being sent and silently ignored at the other end.
_NICK_OK = re.compile(r"^[A-Za-z0-9_\[\]{}\\^`|.-]{1,32}$")

# How far back to look, and how often to report.
_WINDOW_DAYS = int(os.getenv("ATTEND_WINDOW_DAYS", "30"))
_EVERY_MIN = int(os.getenv("ATTEND_EVERY_MIN", "180"))
_MAX_SCAN = int(os.getenv("ATTEND_MAX_MESSAGES", "20000"))
# Who to hand it to, and the nick Dracula answers on.
_TO = os.getenv("ATTEND_REPORT_TO", "Dracula")
# Names that are relayed but are not people: our own bots, and services.
_NOT_PEOPLE = {
    n.strip().lower()
    for n in (os.getenv("ATTEND_IGNORE", "") or "").split(",")
    if n.strip()
} | {"chanserv", "nickserv", "hostserv", "operserv", "botserv", "memoserv", "chanbot"}


def _sign(secret: str, body: str) -> str:
    """The construction Dracula's attendance.js verifies, and nothing else."""
    return hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def encode_report(secret: str, days: dict[str, int], at_ms: int | None = None) -> str:
    """``REGULARS <ms> nick:days,... <sig>`` — the timestamp is inside the body."""
    at = int(time.time() * 1000) if at_ms is None else int(at_ms)
    pairs = [
        f"{nick}:{min(400, max(0, int(n)))}"
        for nick, n in sorted(days.items())
        if _NICK_OK.match(nick)
    ]
    body = f"{at} {','.join(pairs)}"
    return f"REGULARS {body} {_sign(secret, body)}"


class AttendanceCog(commands.Cog, name="Attendance"):
    """Count the days each IRC nick was heard on, and tell Dracula."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last: dict[str, int] = {}
        self._last_run = 0.0
        self._last_error = ""

    async def cog_load(self) -> None:
        self.report_loop.change_interval(minutes=max(15, _EVERY_MIN))
        self.report_loop.start()

    async def cog_unload(self) -> None:
        self.report_loop.cancel()

    # ── the count ────────────────────────────────────────────────────────────

    def _bridge(self):
        return getattr(self.bot, "_irc_bridge", None)

    def _bridged_channels(self) -> list[discord.TextChannel]:
        """Only channels that are relays of an IRC room.

        Read from the bridge's own map rather than guessed from names, so a
        channel that stops being bridged stops being counted on the same day.
        """
        bridge = self._bridge()
        out: list[discord.TextChannel] = []
        names: set[str] = set()
        # _i2d maps irc channel -> Discord channel NAME, and is the same map the
        # relay itself reads through get_discord_for_irc(). The first version of
        # this guessed at four plausible attribute names, none of which existed,
        # so it fell through to BRIDGE_CHANNEL and would have counted one of the
        # two bridged rooms while looking like it counted both.
        i2d = getattr(bridge, "_i2d", None) if bridge is not None else None
        if isinstance(i2d, dict):
            lock = getattr(bridge, "_map_lock", None)
            if lock is not None:
                with lock:
                    names = {str(v).lstrip("#").lower() for v in i2d.values() if v}
            else:
                names = {str(v).lstrip("#").lower() for v in i2d.values() if v}
        if not names and getattr(config, "BRIDGE_CHANNEL", ""):
            names = {str(config.BRIDGE_CHANNEL).lstrip("#").lower()}
        if not names:
            self._last_error = "no bridged channels found to count"
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() in names:
                    out.append(ch)
        return out

    async def count_days(self) -> dict[str, int]:
        """nick -> how many SEPARATE days it was heard on, inside the window.

        Days, not messages, and that is the whole point: it is the measure that
        matches "talks normally everyday", and the one somebody cannot reach by
        flooding for an hour.
        """
        since = datetime.now(timezone.utc) - timedelta(days=max(1, _WINDOW_DAYS))
        seen: dict[str, set[str]] = defaultdict(set)
        scanned = 0
        for ch in self._bridged_channels():
            try:
                async for m in ch.history(limit=_MAX_SCAN, after=since, oldest_first=False):
                    scanned += 1
                    # Only Luna's own relay lines. A human typing the format by
                    # hand in Discord must not be able to invent a speaker.
                    if m.author.id != self.bot.user.id and not m.webhook_id:
                        continue
                    hit = _RELAY.match(m.content or "")
                    if not hit:
                        continue
                    nick = hit.group("nick").strip()
                    if not _NICK_OK.match(nick) or nick.lower() in _NOT_PEOPLE:
                        continue
                    seen[nick.lower()].add(m.created_at.strftime("%Y-%m-%d"))
            except discord.Forbidden:
                # Missing Read Message History. Worth saying: the feed would
                # otherwise look configured and quietly report nothing.
                self._last_error = f"no permission to read history in #{ch.name}"
                print(f"[attendance] {self._last_error}")
            except Exception as e:                      # noqa: BLE001
                self._last_error = f"{type(e).__name__} reading #{ch.name}: {e}"
                print(f"[attendance] {self._last_error}")
        print(f"[attendance] {scanned} relayed lines, {len(seen)} nicks, window {_WINDOW_DAYS}d")
        return {nick: len(days) for nick, days in seen.items()}

    # ── the handover ─────────────────────────────────────────────────────────

    def _send(self, days: dict[str, int]) -> str:
        secret = os.getenv("PEER_SECRET", "")
        if not secret:
            return "PEER_SECRET is not set, so nothing was sent (an unsigned list is worse than none)"
        bridge = self._bridge()
        if bridge is None:
            return "the IRC bridge is not running"
        line = encode_report(secret, days)
        # A NOTICE to Dracula alone. Never to a channel: the list of who is
        # about to be trusted is not something to publish in the room it is
        # about. IRC drops a line past ~512 bytes SILENTLY, so this is split.
        head = f"NOTICE {_TO} :"
        if len(head) + len(line) > 440:
            # Report the busiest first, since those are the only ones near any
            # threshold; a truncated tail costs nothing.
            ranked = sorted(days.items(), key=lambda kv: -kv[1])
            keep: dict[str, int] = {}
            for nick, n in ranked:
                trial = dict(keep, **{nick: n})
                if len(head) + len(encode_report(secret, trial)) > 440:
                    break
                keep = trial
            line = encode_report(secret, keep)
            days = keep
        bridge.send_raw(f"{head}{line}")
        return f"sent {len(days)} nick(s) to {_TO}"

    @tasks.loop(minutes=180)
    async def report_loop(self) -> None:
        try:
            days = await self.count_days()
            self._last = days
            self._last_run = time.time()
            if days:
                print(f"[attendance] {self._send(days)}")
        except Exception as e:                          # noqa: BLE001
            self._last_error = f"{type(e).__name__}: {e}"
            print(f"[attendance] report failed: {self._last_error}")

    @report_loop.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()

    # ── what a moderator can ask ─────────────────────────────────────────────

    @commands.command(name="regulars")
    async def regulars(self, ctx: commands.Context, top: int = 15) -> None:
        """Who talks here most days, out of the relayed history."""
        days = self._last or await self.count_days()
        if not days:
            await ctx.send(
                "No relayed history found to count. "
                + (f"Last problem: {self._last_error}" if self._last_error else
                   "Check that this channel is bridged and I can read its history.")
            )
            return
        ranked = sorted(days.items(), key=lambda kv: (-kv[1], kv[0]))[: max(1, min(40, top))]
        width = max(len(n) for n, _ in ranked)
        body = "\n".join(f"{n.ljust(width)}  {d:>3} days" for n, d in ranked)
        await ctx.send(
            f"**Heard on the most separate days** (last {_WINDOW_DAYS} days)\n"
            f"```\n{body}\n```\n"
            "Dracula treats this as evidence only — it still requires a registered "
            "account and a clean record before anyone is trusted."
        )

    @commands.command(name="sendregulars")
    async def sendregulars(self, ctx: commands.Context) -> None:
        """Hand the tally to Dracula now, rather than waiting for the timer."""
        days = await self.count_days()
        self._last = days
        await ctx.send(self._send(days) if days else "Nothing counted, so nothing sent.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AttendanceCog(bot))

"""
The moderator memory neither bot has.

Dracula forgets. Everything it knows about people's behaviour lives in maps
created when the process starts, and GitHub Actions hands the job over roughly
every six hours: `warns`, the strike counts the escalation ladder runs on, and
`watch`'s sightings of somebody misbehaving in another room all reset. In
practice that means a moderator cannot ask "has this person been warned before?"
and get a true answer, and somebody who trips the filter once every seven hours
never reaches a second strike at all.

Luna is the half of the pair that can remember, because Discord keeps what it is
told. So this is the Vampire-era moderation that Dracula never got, put where it
can actually work rather than duplicated where it cannot:

  $warn <nick> <reason>     record a warning that survives every restart
  $warnings <nick>          the whole history, with who gave it and when
  $clearwarns <nick>        wipe somebody's record (mod)
  $seen <nick>              when the room last heard from them
  $slowmode <n>             one line per n seconds, enforced by DEVOICING

The store is an append-only ledger in one Discord channel: one line per record,
read back on demand. Not a file — the runner's disk is destroyed with the job,
which is exactly how the previous version of this lost its warns.json every six
hours without anybody noticing.

$seen needs no store at all. Every line of the room is already relayed into
Discord, so the answer is in the history — the same source the attendance count
reads, parsed with the same pattern.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
from cogs.admin_cog import mod_only

# One ledger line. Deliberately ugly and unambiguous: it has to survive being
# read back out of a channel that also contains ordinary conversation, so it is
# anchored, fenced, and carries a version tag for when the shape changes.
_LEDGER_RE = re.compile(
    r"^`LEDGER1`\s+(?P<kind>warn|clear|tell|told)\s+\|(?P<nick>[^|]{1,32})\|"
    r"(?P<by>[^|]{1,64})\|(?P<at>\d{1,15})\|(?P<reason>.*)$"
)
# The relay format from utils/irc_bridge.py, for $seen.
_RELAY_RE = re.compile(r"^\*\*\[(?P<room>[^\]]{1,64})\]\*\*\s+`(?P<nick>[^`]{1,32})`:")

_LEDGER_CHANNEL = os.getenv("RECORD_CHANNEL", "") or getattr(config, "ALERT_CHANNEL", "bot-logs")
_SCAN = int(os.getenv("RECORD_SCAN", "4000"))
_SEEN_DAYS = int(os.getenv("SEEN_WINDOW_DAYS", "30"))
# Never rate-limited: services, the network's own bot, and our own side of the
# pair. Dracula moderating a room while Luna devoices Dracula is a fight between
# two bots that a human then has to break up.
_NEVER_TOUCH = {
    "chanserv", "nickserv", "hostserv", "operserv", "botserv", "memoserv",
    "chanbot", "dracula", "luna1", "luna",
}


def _ago(seconds: float) -> str:
    """Human time, because "1757800000" tells a moderator nothing."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    return f"{s // 86400}d ago"


class RecordsCog(commands.Cog, name="Records"):
    """Warnings and sightings that outlive a restart."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._slow: dict[str, int] = {}                    # irc room -> seconds
        self._spoke: dict[tuple[str, str], float] = {}     # (room, nick) -> last line
        self._told: dict[tuple[str, str], float] = {}      # (room, nick) -> last devoice
        self._never = {
            n.strip().lower()
            for n in (os.getenv("LUNA_WHITELIST_IRC", "") or "").split(",")
            if n.strip()
        }

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _bridge(self):
        return getattr(self.bot, "_irc_bridge", None)

    def _ledger_channel(self) -> discord.TextChannel | None:
        want = _LEDGER_CHANNEL.lstrip("#").lower()
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == want:
                    return ch
        return None

    def _relay_channels(self) -> list[discord.TextChannel]:
        """The channels that are relays of an IRC room, from the bridge's own map."""
        bridge = self._bridge()
        names: set[str] = set()
        i2d = getattr(bridge, "_i2d", None) if bridge is not None else None
        if isinstance(i2d, dict):
            lock = getattr(bridge, "_map_lock", None)
            if lock is not None:
                with lock:
                    names = {str(v).lstrip("#").lower() for v in i2d.values() if v}
            else:
                names = {str(v).lstrip("#").lower() for v in i2d.values() if v}
        out = []
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() in names:
                    out.append(ch)
        return out

    async def _read_ledger(self, nick: str | None = None) -> list[dict]:
        """Every record, oldest first. A `clear` truncates what came before it."""
        ch = self._ledger_channel()
        if ch is None:
            return []
        want = (nick or "").lower()
        rows: list[dict] = []
        try:
            async for m in ch.history(limit=_SCAN, oldest_first=False):
                # Only Luna's own lines. A moderator cannot hand somebody a
                # warning record by typing the format into the channel.
                if m.author.id != self.bot.user.id:
                    continue
                hit = _LEDGER_RE.match((m.content or "").strip())
                if not hit:
                    continue
                row = hit.groupdict()
                if want and row["nick"].lower() != want:
                    continue
                rows.append(row)
        except discord.Forbidden:
            return []
        rows.reverse()
        # A clear wipes everything before it, so the history a moderator reads
        # matches what they were told when they cleared it.
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["kind"] == "clear":
                rows = rows[i + 1:]
                break
        return [r for r in rows if r["kind"] == "warn"]

    async def _write(self, kind: str, nick: str, by: str, reason: str) -> bool:
        ch = self._ledger_channel()
        if ch is None:
            return False
        # Pipes are the field separator, so they cannot appear in a field.
        safe = lambda t, n: str(t).replace("|", "/").replace("\n", " ")[:n]  # noqa: E731
        try:
            await ch.send(
                f"`LEDGER1` {kind} |{safe(nick, 32)}|{safe(by, 64)}|{int(time.time())}|"
                f"{safe(reason, 300)}"
            )
            return True
        except discord.Forbidden:
            return False

    # ── warnings ─────────────────────────────────────────────────────────────

    @commands.command(name="warn")
    @mod_only()
    async def warn(self, ctx: commands.Context, nick: str, *, reason: str = "no reason given") -> None:
        """Record a warning against an IRC nick. Survives every restart."""
        if not re.fullmatch(r"[A-Za-z0-9_\[\]{}\\^`|.-]{1,32}", nick):
            await ctx.send("That is not a valid IRC nick.")
            return
        if not await self._write("warn", nick, str(ctx.author), reason):
            await ctx.send(
                f"I could not write to #{_LEDGER_CHANNEL} — a warning I cannot store is "
                "a warning that disappears at the next handover, so I did not pretend to save it."
            )
            return
        history = await self._read_ledger(nick)
        await ctx.send(f"⚠️ Recorded. **{nick}** now has **{len(history)}** warning(s) on record.")
        # Tell them on IRC, quietly. Never in the channel: a public telling-off
        # is the thing the room objected to.
        bridge = self._bridge()
        if bridge is not None:
            bridge.send_raw(
                f"NOTICE {nick} :[MOD] {reason} — this is on record "
                f"(warning {len(history)}). Ask a moderator if you think it is wrong."
            )

    @commands.command(name="warnings", aliases=["warns", "record"])
    @mod_only()
    async def warnings(self, ctx: commands.Context, nick: str) -> None:
        """Somebody's whole warning history."""
        rows = await self._read_ledger(nick)
        if not rows:
            await ctx.send(f"**{nick}** has nothing on record.")
            return
        now = time.time()
        lines = [
            f"{_ago(now - int(r['at'])):>9}  by {r['by'].split('#')[0]:<16} {r['reason'][:80]}"
            for r in rows[-12:]
        ]
        more = f"\n…and {len(rows) - 12} older" if len(rows) > 12 else ""
        await ctx.send(f"**{nick}** — {len(rows)} warning(s)\n```\n" + "\n".join(lines) + f"\n```{more}")

    @commands.command(name="clearwarns", aliases=["unwarn"])
    @mod_only()
    async def clearwarns(self, ctx: commands.Context, nick: str) -> None:
        """Wipe somebody's record."""
        rows = await self._read_ledger(nick)
        if not rows:
            await ctx.send(f"**{nick}** has nothing on record.")
            return
        if not await self._write("clear", nick, str(ctx.author), f"cleared {len(rows)}"):
            await ctx.send(f"I could not write to #{_LEDGER_CHANNEL}, so nothing was cleared.")
            return
        await ctx.send(f"✅ Cleared **{len(rows)}** warning(s) against **{nick}**.")

    # ── seen ─────────────────────────────────────────────────────────────────

    @commands.command(name="seen")
    async def seen(self, ctx: commands.Context, nick: str) -> None:
        """When the room last heard from somebody.

        Read straight out of the relayed history, so it needs no store of its own
        and covers as far back as the channel does.
        """
        want = nick.lower()
        since = datetime.now(timezone.utc) - timedelta(days=max(1, _SEEN_DAYS))
        best = None
        room = ""
        for ch in self._relay_channels():
            try:
                async for m in ch.history(limit=_SCAN, after=since, oldest_first=False):
                    if m.author.id != self.bot.user.id and not m.webhook_id:
                        continue
                    hit = _RELAY_RE.match(m.content or "")
                    if not hit or hit.group("nick").lower() != want:
                        continue
                    if best is None or m.created_at > best:
                        best = m.created_at
                        room = hit.group("room")
                    break          # history is newest-first, so the first hit is the latest
            except discord.Forbidden:
                continue
        if best is None:
            await ctx.send(
                f"I have not heard **{nick}** in the last {_SEEN_DAYS} days of relayed history."
            )
            return
        ago = _ago((datetime.now(timezone.utc) - best).total_seconds())
        await ctx.send(f"**{nick}** was last heard in **{room}** — {ago}.")

    # ── slow mode ────────────────────────────────────────────────────────────

    @commands.command(name="slowmode")
    @mod_only()
    async def slowmode(self, ctx: commands.Context, seconds: int = 0) -> None:
        """One line per n seconds per person in the bridged IRC room. 0 lifts it.

        Enforced by DEVOICING whoever goes faster, never by kicking them.
        """
        bridge = self._bridge()
        if bridge is None:
            await ctx.send("The IRC bridge is not running.")
            return
        room = bridge.get_irc_for_discord(ctx.channel.name) or getattr(config, "IRC_CHANNEL", "")
        if not room:
            await ctx.send("I could not work out which IRC room this channel maps to.")
            return
        if seconds <= 0:
            self._slow.pop(room.lower(), None)
            await ctx.send(f"Slow mode off in `{room}`.")
            return
        n = max(1, min(120, seconds))
        self._slow[room.lower()] = n
        await ctx.send(
            f"Slow mode on in `{room}` — one line per **{n}s** per person. Anyone faster "
            f"loses voice and a moderator is told; nobody is kicked. "
            f"`{config.PREFIX}slowmode 0` to lift it."
        )

    @commands.Cog.listener("on_message")
    async def _watch_rate(self, message: discord.Message) -> None:
        """Every IRC line arrives here, because Luna relays it into Discord.

        Reading the relay instead of the socket means this needs no change to the
        bridge and no second connection: the line is already in a channel by the
        time it matters.

        Deliberately a DEVOICE. InspIRCd's own flood mode (+f) kicks, which is
        precisely what the room objected to — and the first version of this
        command sent "+f [1t#n]", which is UnrealIRCd syntax and would have been
        rejected by this server with no visible error at all.
        """
        if not self._slow or message.author.id != self.bot.user.id:
            return
        hit = _RELAY_RE.match(message.content or "")
        if not hit:
            return
        bridge = self._bridge()
        if bridge is None:
            return
        room = bridge.get_irc_for_discord(message.channel.name) or ""
        gap = self._slow.get(room.lower())
        if not gap:
            return
        nick = hit.group("nick")
        low = nick.lower()
        if low in _NEVER_TOUCH or low in self._never:
            return
        now = time.time()
        last = self._spoke.get((room.lower(), low), 0.0)
        self._spoke[(room.lower(), low)] = now
        if not last or now - last >= gap:
            return
        # Once. Repeating a devoice they already have is a mode war with nobody.
        key = (room.lower(), low)
        if now - self._told.get(key, 0.0) < 300:
            return
        self._told[key] = now
        bridge.send_raw(f"MODE {room} -v {nick}")
        bridge.send_raw(
            f"NOTICE {nick} :[MOD] {room} is in slow mode — about one line every "
            f"{gap}s. Your voice is off for now; a moderator can give it straight back."
        )
        alert = self._ledger_channel()
        if alert is not None:
            try:
                await alert.send(
                    f"🐌 **{nick}** went faster than slow mode allows in `{room}` "
                    f"— devoiced, not kicked. `/mode {room} +v {nick}` to undo."
                )
            except discord.Forbidden:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecordsCog(bot))

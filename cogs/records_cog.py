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
  $slowmode <n>             hold the room's flood limit to n seconds

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
    r"^`LEDGER1`\s+(?P<kind>warn|clear)\s+\|(?P<nick>[^|]{1,32})\|"
    r"(?P<by>[^|]{1,64})\|(?P<at>\d{1,15})\|(?P<reason>.*)$"
)
# The relay format from utils/irc_bridge.py, for $seen.
_RELAY_RE = re.compile(r"^\*\*\[(?P<room>[^\]]{1,64})\]\*\*\s+`(?P<nick>[^`]{1,32})`:")

_LEDGER_CHANNEL = os.getenv("RECORD_CHANNEL", "") or getattr(config, "ALERT_CHANNEL", "bot-logs")
_SCAN = int(os.getenv("RECORD_SCAN", "4000"))
_SEEN_DAYS = int(os.getenv("SEEN_WINDOW_DAYS", "30"))


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

    # ── slowmode ─────────────────────────────────────────────────────────────

    @commands.command(name="slowmode")
    @mod_only()
    async def slowmode(self, ctx: commands.Context, seconds: int = 0) -> None:
        """Limit how fast the IRC room can be typed in. 0 turns it off."""
        bridge = self._bridge()
        if bridge is None:
            await ctx.send("The IRC bridge is not running.")
            return
        room = bridge.get_irc_for_discord(ctx.channel.name) if hasattr(
            bridge, "get_irc_for_discord") else None
        room = room or getattr(config, "IRC_CHANNEL", "")
        if not room:
            await ctx.send("I could not work out which IRC room this channel maps to.")
            return
        if seconds <= 0:
            bridge.send_raw(f"MODE {room} -f")
            await ctx.send(f"Slow mode off in `{room}`.")
            return
        n = max(1, min(60, seconds))
        # InspIRCd's flood mode: more than one line per n seconds and the server
        # blocks the excess. Chosen over a bot-side throttle because the server
        # enforces it on everybody, including while Luna is between restarts.
        bridge.send_raw(f"MODE {room} +f [1t#{n}]")
        await ctx.send(
            f"Slow mode on in `{room}` — about one line per **{n}s** per person. "
            f"`{config.PREFIX}slowmode 0` to lift it."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecordsCog(bot))

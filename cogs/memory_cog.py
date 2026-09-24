"""
The three things Luna can do that nothing else in this room can.

IRC has no scrollback and no memory. A person who joins at nine has no idea what
happened at eight, a message for somebody who is offline simply cannot be left,
and nobody can answer "when is this room actually busy?". Dracula cannot help —
it forgets everything when the host hands over, roughly every six hours.

Luna relays every line into Discord, and Discord keeps it. So:

  $find <text>        search what the room has actually said
  $tell <nick> <msg>  leave a message, delivered when they next speak
  $stats              who talks here, and when the room is awake

None of these are new IRC ideas — MemoServ has done $tell for decades. What is
new is that they work HERE, from the room's own history, without anybody needing
to register with a service or remember a syntax.

$tell is the one with a trust boundary. A message left for somebody is delivered
to them privately later, in Luna's voice, so it must not become a way to put
words in her mouth or to harass somebody who cannot see who sent it. It carries
the sender's name, it is rate-limited per sender, and it is refused for anybody
on the never-touch list.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
from cogs.records_cog import _LEDGER_RE, _RELAY_RE, _NEVER_TOUCH, _ago

_SCAN = int(os.getenv("RECORD_SCAN", "4000"))
_WINDOW_DAYS = int(os.getenv("FIND_WINDOW_DAYS", "30"))
_MAX_PENDING = int(os.getenv("TELL_MAX_PENDING", "3"))
_NICK_OK = re.compile(r"^[A-Za-z0-9_\[\]{}\\^`|.-]{1,32}$")


class MemoryCog(commands.Cog, name="Memory"):
    """Search, messages and activity — all out of the relayed history."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delivered: set[str] = set()      # ledger lines already acted on

    # ── plumbing, shared with records_cog ────────────────────────────────────

    def _bridge(self):
        return getattr(self.bot, "_irc_bridge", None)

    def _relay_channels(self) -> list[discord.TextChannel]:
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
        return [ch for g in self.bot.guilds for ch in g.text_channels
                if ch.name.lower() in names]

    def _ledger_channel(self) -> discord.TextChannel | None:
        want = (os.getenv("RECORD_CHANNEL", "")
                or getattr(config, "ALERT_CHANNEL", "bot-logs")).lstrip("#").lower()
        for g in self.bot.guilds:
            for ch in g.text_channels:
                if ch.name.lower() == want:
                    return ch
        return None

    # ── $find ────────────────────────────────────────────────────────────────

    @commands.command(name="find", aliases=["search"])
    async def find(self, ctx: commands.Context, *, text: str) -> None:
        """Search what the room has said. IRC has no scrollback; this is it."""
        needle = text.strip().lower()
        if len(needle) < 3:
            await ctx.send("Give me at least three characters to look for.")
            return
        since = datetime.now(timezone.utc) - timedelta(days=max(1, _WINDOW_DAYS))
        hits: list[tuple[datetime, str, str, str]] = []
        for ch in self._relay_channels():
            try:
                async for m in ch.history(limit=_SCAN, after=since, oldest_first=False):
                    if m.author.id != self.bot.user.id and not m.webhook_id:
                        continue
                    hit = _RELAY_RE.match(m.content or "")
                    if not hit:
                        continue
                    said = (m.content or "").split(":", 1)[-1].strip()
                    if needle not in said.lower():
                        continue
                    hits.append((m.created_at, hit.group("room"), hit.group("nick"), said))
                    if len(hits) >= 60:
                        break
            except discord.Forbidden:
                continue
        if not hits:
            await ctx.send(f"Nothing matching **{text[:60]}** in the last {_WINDOW_DAYS} days.")
            return
        hits.sort(key=lambda h: h[0], reverse=True)
        now = datetime.now(timezone.utc)
        lines = [f"{_ago((now - at).total_seconds()):>9}  [{room}] {nick}: {said[:90]}"
                 for at, room, nick, said in hits[:10]]
        more = f"\n…and {len(hits) - 10} older" if len(hits) > 10 else ""
        await ctx.send(f"**{len(hits)}** match(es) for **{text[:60]}**\n```\n"
                       + "\n".join(lines) + f"\n```{more}")

    # ── $tell ────────────────────────────────────────────────────────────────

    @commands.command(name="tell", aliases=["memo"])
    async def tell(self, ctx: commands.Context, nick: str, *, message: str) -> None:
        """Leave a message, delivered privately when they next speak."""
        if not _NICK_OK.match(nick):
            await ctx.send("That is not a valid IRC nick.")
            return
        if nick.lower() in _NEVER_TOUCH:
            await ctx.send("That one is a bot or a service — it will not read its messages.")
            return
        ch = self._ledger_channel()
        if ch is None:
            await ctx.send(
                f"I have nowhere to keep it — #{os.getenv('RECORD_CHANNEL', '') or 'bot-logs'} "
                "is missing, and a message I cannot store is one that quietly disappears."
            )
            return
        pending = [r for r in await self._pending() if r["nick"].lower() == nick.lower()]
        mine = [r for r in pending if r["by"].split("#")[0] == str(ctx.author).split("#")[0]]
        if len(mine) >= _MAX_PENDING:
            await ctx.send(f"You already have {len(mine)} waiting for **{nick}**. "
                           "Let them read those first.")
            return
        safe = lambda t, n: str(t).replace("|", "/").replace("\n", " ")[:n]  # noqa: E731
        await ch.send(f"`LEDGER1` tell |{safe(nick, 32)}|{safe(ctx.author, 64)}|"
                      f"{int(time.time())}|{safe(message, 300)}")
        await ctx.send(f"✉️ I'll give that to **{nick}** when they next speak.")

    async def _pending(self) -> list[dict]:
        """Messages left but not yet delivered, oldest first."""
        ch = self._ledger_channel()
        if ch is None:
            return []
        left: list[dict] = []
        done: set[str] = set()
        try:
            async for m in ch.history(limit=_SCAN, oldest_first=False):
                if m.author.id != self.bot.user.id:
                    continue
                hit = _LEDGER_RE.match((m.content or "").strip())
                if not hit:
                    continue
                row = hit.groupdict()
                key = f"{row['nick'].lower()}|{row['at']}"
                if row["kind"] == "told":
                    done.add(key)
                elif row["kind"] == "tell":
                    row["key"] = key
                    left.append(row)
        except discord.Forbidden:
            return []
        left.reverse()
        return [r for r in left if r["key"] not in done and r["key"] not in self._delivered]

    @commands.Cog.listener("on_message")
    async def _deliver(self, message: discord.Message) -> None:
        """Hand over anything waiting, the moment they speak.

        Read off Luna's own relay rather than the socket: the line is already in a
        channel by the time it matters, and this needs no change to the bridge.
        """
        if message.author.id != self.bot.user.id:
            return
        hit = _RELAY_RE.match(message.content or "")
        if not hit:
            return
        who = hit.group("nick")
        waiting = [r for r in await self._pending() if r["nick"].lower() == who.lower()]
        if not waiting:
            return
        bridge = self._bridge()
        if bridge is None:
            return
        ch = self._ledger_channel()
        now = time.time()
        for r in waiting[:_MAX_PENDING]:
            bridge.send_raw(
                f"NOTICE {who} :[{_ago(now - int(r['at']))}] {r['by'].split('#')[0]} "
                f"left you this: {r['reason'][:300]}"
            )
            self._delivered.add(r["key"])
            if ch is not None:
                try:
                    await ch.send(f"`LEDGER1` told |{r['nick']}|{r['by']}|{r['at']}|delivered")
                except discord.Forbidden:
                    pass

    # ── $stats ───────────────────────────────────────────────────────────────

    @commands.command(name="stats", aliases=["activity"])
    async def stats(self, ctx: commands.Context) -> None:
        """Who talks here, and when the room is actually awake."""
        since = datetime.now(timezone.utc) - timedelta(days=7)
        talkers: Counter = Counter()
        hours: Counter = Counter()
        rooms: Counter = Counter()
        total = 0
        for ch in self._relay_channels():
            try:
                async for m in ch.history(limit=_SCAN, after=since, oldest_first=False):
                    if m.author.id != self.bot.user.id and not m.webhook_id:
                        continue
                    hit = _RELAY_RE.match(m.content or "")
                    if not hit:
                        continue
                    nick = hit.group("nick").lower()
                    if nick in _NEVER_TOUCH:
                        continue
                    talkers[hit.group("nick")] += 1
                    hours[m.created_at.hour] += 1
                    rooms[hit.group("room")] += 1
                    total += 1
            except discord.Forbidden:
                continue
        if not total:
            await ctx.send("No relayed history to count yet.")
            return
        top = talkers.most_common(8)
        width = max(len(n) for n, _ in top)
        busiest = ", ".join(f"{h:02d}:00 UTC" for h, _ in hours.most_common(3))
        body = "\n".join(f"{n.ljust(width)}  {c:>4}" for n, c in top)
        await ctx.send(
            f"**Last 7 days** — {total} lines across "
            + ", ".join(f"{r} ({c})" for r, c in rooms.most_common(3))
            + f"\nBusiest hours: **{busiest}**\n```\n{body}\n```"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemoryCog(bot))

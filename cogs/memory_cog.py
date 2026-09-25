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
import random
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

    # ── The work, separated from how it was asked for ────────────────────────
    #
    # These used to live inside the command bodies, tangled with ctx.send, which
    # meant IRC could not reach a single one of them — and IRC is where the room
    # actually is. The owner put it plainly: the new commands were not in $help
    # and did not work, because they were Discord-side and nothing said so.
    #
    # So the work returns DATA and the callers dress it. Discord gets code
    # blocks and ten rows; IRC gets one compact line, because IRC floods.

    async def _scan(self, since, until=None, cap: int = _SCAN):
        """Every relayed line in a window, newest first: (at, room, nick, said).

        One scanner for every history feature. It was copied into each command
        before, which is how two of them ended up disagreeing about what counts
        as a relayed line.
        """
        out: list[tuple] = []
        for ch in self._relay_channels():
            try:
                async for m in ch.history(limit=cap, after=since, before=until,
                                          oldest_first=False):
                    if m.author.id != self.bot.user.id and not m.webhook_id:
                        continue
                    hit = _RELAY_RE.match(m.content or "")
                    if not hit:
                        continue
                    said = (m.content or "").split(":", 1)[-1].strip()
                    if not said:
                        continue
                    out.append((m.created_at, hit.group("room"),
                                hit.group("nick"), said))
            except discord.Forbidden:
                continue
        out.sort(key=lambda r: r[0], reverse=True)
        return out

    async def search(self, needle: str, cap: int = 60) -> list:
        needle = needle.strip().lower()
        since = datetime.now(timezone.utc) - timedelta(days=max(1, _WINDOW_DAYS))
        return [r for r in await self._scan(since) if needle in r[3].lower()][:cap]

    async def activity(self, days: int = 7):
        since = datetime.now(timezone.utc) - timedelta(days=days)
        talkers: Counter = Counter()
        hours: Counter = Counter()
        rooms: Counter = Counter()
        for at, room, nick, _said in await self._scan(since):
            if nick.lower() in _NEVER_TOUCH:
                continue
            talkers[nick] += 1
            hours[at.hour] += 1
            rooms[room] += 1
        return sum(talkers.values()), talkers, hours, rooms

    async def sample(self, nick: str = "", days: int = 30, min_len: int = 25):
        """One line somebody actually said. Commands and bots are skipped —
        quoting the bot back at the room is not a quote."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        pool = [r for r in await self._scan(since)
                if len(r[3]) >= min_len
                and r[2].lower() not in _NEVER_TOUCH
                and not r[3].startswith(config.PREFIX)
                and (not nick or r[2].lower() == nick.lower())]
        return random.choice(pool) if pool else None

    async def rewind(self, days_back: int = 7, span_hours: int = 2, cap: int = 5):
        """What the room was saying this time N days ago, oldest first."""
        until = datetime.now(timezone.utc) - timedelta(days=days_back)
        rows = await self._scan(until - timedelta(hours=span_hours), until=until)
        return list(reversed([r for r in rows if r[2].lower() not in _NEVER_TOUCH][:cap]))

    # ── IRC-shaped answers ───────────────────────────────────────────────────
    # One line where possible. A three-line reply on IRC pushes the
    # conversation off a phone screen, and the pacer sends about two a second.

    async def irc_tell(self, author: str, nick: str, message: str) -> str:
        """Leave a message from an IRC nick. The ledger keys on the name given,
        so this is deliberately the same store the Discord side writes to —
        one inbox, not two that disagree."""
        if not _NICK_OK.match(nick):
            return "That is not a valid IRC nick."
        if nick.lower() in _NEVER_TOUCH:
            return "That one is a bot — it will not read its messages."
        if nick.lower() == author.lower():
            return "You are right here."
        if not message.strip():
            return f"And what should I tell {nick}?"
        ch = self._ledger_channel()
        if ch is None:
            # A message I cannot store is one that quietly disappears, which is
            # worse than refusing it.
            return "I have nowhere to keep it right now, so I will not pretend to."
        mine = [r for r in await self._pending()
                if r["nick"].lower() == nick.lower()
                and r["by"].split("#")[0].lower() == author.lower()]
        if len(mine) >= _MAX_PENDING:
            return f"You already have {len(mine)} waiting for {nick}."
        safe = lambda t, n: str(t).replace("|", "/").replace("\n", " ")[:n]  # noqa: E731
        await ch.send(f"`LEDGER1` tell |{safe(nick, 32)}|{safe(author, 64)}|"
                      f"{int(time.time())}|{safe(message, 300)}")
        return f"I'll give that to {nick} when they next speak."

    async def irc_seen(self, nick: str) -> str:
        """Last heard from. Lives here rather than in records_cog because it
        reads the same relayed history as everything else, and two scanners
        over one source is how they drift apart."""
        if not nick.strip():
            return f"{config.PREFIX}seen <nick>"
        want = nick.strip().lower()
        since = datetime.now(timezone.utc) - timedelta(days=max(1, _WINDOW_DAYS))
        for at, room, who, said in await self._scan(since):
            if who.lower() == want:
                ago = _ago((datetime.now(timezone.utc) - at).total_seconds()).strip()
                return f'{who} was last heard {ago} in {room}: "{said[:120]}"'
        return f"I have not heard {nick[:32]} in the last {_WINDOW_DAYS} days."

    async def irc_mood(self) -> str:
        from utils import moods
        name, line = moods.current()
        return f"Tonight I am {name} — {line.split(':', 1)[-1].strip()}"

    async def irc_find(self, needle: str) -> str:
        if len(needle.strip()) < 3:
            return "Give me at least three characters to look for."
        hits = await self.search(needle)
        if not hits:
            return f'Nothing matching "{needle[:40]}" in the last {_WINDOW_DAYS} days.'
        now = datetime.now(timezone.utc)
        body = " · ".join(
            f"[{_ago((now - at).total_seconds()).strip()}] {nick}: {said[:70]}"
            for at, _room, nick, said in hits[:3])
        more = f" (+{len(hits) - 3} older)" if len(hits) > 3 else ""
        return f'{len(hits)} for "{needle[:40]}" — {body}{more}'

    async def irc_stats(self) -> str:
        total, talkers, hours, rooms = await self.activity()
        if not total:
            return "No relayed history to count yet."
        top = ", ".join(f"{n} ({c})" for n, c in talkers.most_common(5))
        busy = ", ".join(f"{h:02d}:00" for h, _ in hours.most_common(2))
        where = ", ".join(r for r, _ in rooms.most_common(2))
        return f"Last 7 days: {total} lines in {where}. Busiest {busy} UTC. Talking most: {top}"

    async def irc_quote(self, nick: str = "") -> str:
        row = await self.sample(nick)
        if not row:
            return (f"I have nothing quotable from {nick} yet." if nick
                    else "Nothing worth quoting in my memory yet.")
        at, _room, who, said = row
        ago = _ago((datetime.now(timezone.utc) - at).total_seconds()).strip()
        return f'{who}, {ago}: "{said[:220]}"'

    async def irc_rewind(self, days_back: int = 7) -> str:
        rows = await self.rewind(days_back)
        if not rows:
            return f"I have nothing from {days_back} day(s) ago."
        body = " · ".join(f"{nick}: {said[:60]}" for _at, _room, nick, said in rows[:4])
        return f"{days_back} day(s) ago — {body}"

    @commands.command(name="find", aliases=["search"])
    async def find(self, ctx: commands.Context, *, text: str) -> None:
        """Search what the room has said. IRC has no scrollback; this is it."""
        if len(text.strip()) < 3:
            await ctx.send("Give me at least three characters to look for.")
            return
        hits = await self.search(text)
        if not hits:
            await ctx.send(f"Nothing matching **{text[:60]}** in the last {_WINDOW_DAYS} days.")
            return
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
        total, talkers, hours, rooms = await self.activity()
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


    # ── $quote / $onthisday ──────────────────────────────────────────────────
    #
    # The owner asked for "more fun stuff as it remember conversations from
    # discord history". These are that: the memory already exists, and the only
    # reason it was dull was that nothing pointed at it except a search box.

    @commands.command(name="quote")
    async def quote(self, ctx: commands.Context, nick: str = "") -> None:
        """Something somebody actually said."""
        await ctx.send(await self.irc_quote(nick))

    @commands.command(name="onthisday", aliases=["rewind", "backthen"])
    async def onthisday(self, ctx: commands.Context, days: int = 7) -> None:
        """What the room was saying this time a few days ago."""
        days = max(1, min(60, days))
        rows = await self.rewind(days)
        if not rows:
            await ctx.send(f"I have nothing from {days} day(s) ago.")
            return
        lines = [f"[{room}] {nick}: {said[:90]}" for _at, room, nick, said in rows]
        await ctx.send(f"**{days} day(s) ago**\n```\n" + "\n".join(lines) + "\n```")

    # ── $mood ────────────────────────────────────────────────────────────────

    @commands.command(name="mood")
    async def mood(self, ctx: commands.Context) -> None:
        """What sort of evening Luna is having."""
        from utils import moods
        name, line = moods.current()
        nxt = moods.name(datetime.now(timezone.utc) + timedelta(hours=4))
        await ctx.send(f"Tonight I am **{name}** — *{line.split(':', 1)[-1].strip()}*\n"
                       f"Later: **{nxt}**.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemoryCog(bot))

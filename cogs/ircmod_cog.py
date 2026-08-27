"""
IRC moderation from Discord.

`$irckick` and `$ircban` were advertised in Luna's help for months and never
existed — the bridge had kick_irc()/ban_irc() the whole time and no command was
ever wired to them. This module is that wiring, plus the mode controls the room
actually needs day to day.

Two design decisions worth keeping:

**The target room is the room you are standing in.** Every command resolves the
IRC channel from the Discord channel it was typed in, so `$op bob` in the
#batcave Discord channel acts on #batcave, and the same words in the emoji
channel act on the emoji room. The old bridge helpers defaulted to Luna's
primary room no matter where you typed — which meant a moderator in the
#batcave channel would have silently kicked someone out of the *other* room.

**Say what actually happened.** IRC gives no reply to a MODE or KICK that was
refused for lack of operator status; the command simply vanishes. So these check
that Luna is connected, in the room, and opped, and report which of those failed
instead of printing a cheerful confirmation of nothing.
"""

from __future__ import annotations

from typing import Optional, Tuple

import discord
from discord.ext import commands

import config
from cogs.admin_cog import mod_only

# Mode letters, so the intent reads at the call site rather than as a bare glyph.
_OP, _VOICE, _BAN, _QUIET = "o", "v", "b", "q"


class IrcModCog(commands.Cog, name="IRC Moderation"):
    """Kick, ban, op and voice IRC users from Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Plumbing ─────────────────────────────────────────────────────────────

    @property
    def _bridge(self):
        return getattr(self.bot, "_irc_bridge", None)

    def _resolve(self, ctx: commands.Context) -> Tuple[Optional[str], Optional[str]]:
        """(irc_channel, error). The room bridged to where the command was typed.

        Falling back to Luna's primary room would be worse than failing: a
        moderator in the #batcave channel would act on the emoji room and be
        told it succeeded.
        """
        bridge = self._bridge
        if bridge is None:
            return None, "IRC bridge is not running."
        if not bridge.is_connected():
            return None, f"Not connected to IRC. Try `{config.PREFIX}ircreconnect`."
        name = getattr(ctx.channel, "name", "") or ""
        # Honour an explicit $to target: moderating from Discord must act on
        # the room you are actually talking to, not the one the config happened
        # to register first.
        irc_ch = (bridge.get_reply_target(name) if hasattr(bridge, "get_reply_target")
                  else None) or bridge.get_irc_for_discord(name)
        if not irc_ch:
            bridges = ", ".join(f"#{d} → {i}" for i, d in bridge.list_bridges()) or "none"
            return None, (f"**#{name}** is not bridged to an IRC room, so there is "
                          f"nothing to act on. Bridged: {bridges}")
        return irc_ch, None

    def _can_act(self, irc_ch: str) -> Optional[str]:
        """Why this would not work, or None if it will."""
        bridge = self._bridge
        if not bridge.is_nick_in_channel(bridge.nick, irc_ch):
            return f"I am not in `{irc_ch}` right now."
        if not bridge.has_prefix(irc_ch, bridge.nick):
            return (f"I am in `{irc_ch}` but not opped, so the server will ignore "
                    f"me. Give Luna1 op there, or `/msg ChanServ OP {irc_ch} Luna1`.")
        return None

    async def _mode(self, ctx, sign: str, letter: str, nick: str, verb: str):
        """Every mode command is the same four steps; only the letter changes."""
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        blocked = self._can_act(irc_ch)
        if blocked:
            await ctx.send(f"❌ {blocked}")
            return
        if not self._bridge.is_nick_in_channel(nick, irc_ch):
            await ctx.send(f"⚠️ **{nick}** is not in `{irc_ch}`.")
            return
        self._bridge.send_raw(f"MODE {irc_ch} {sign}{letter} {nick}")
        await ctx.send(f"✅ {verb} **{nick}** in `{irc_ch}`.")

    # ── Removal ──────────────────────────────────────────────────────────────

    @commands.command(name="to")
    async def to_room(self, ctx, room: str = ""):
        """Choose which bridged IRC room this Discord channel talks to.

        Two IRC rooms feed this one Discord channel, and a message typed here
        can only go to one of them. The default is whichever room was
        configured first — the emoji room — which is why #batcave could not be
        reached from Discord at all. This picks.
        """
        bridge = self._bridge
        if bridge is None or not bridge.is_connected():
            await ctx.send("IRC bridge is not connected.")
            return
        name = getattr(ctx.channel, "name", "") or ""
        rooms = bridge.bridged_rooms()
        current = bridge.get_reply_target(name)

        if not room:
            listing = "\n".join(
                f"{'**→ ' + r + '**' if current and r.lower() == current.lower() else '  ' + r}"
                for r in rooms) or "  (nothing bridged)"
            await ctx.send(
                f"Messages here go to **{current or 'nowhere'}**.\n{listing}\n"
                f"`{config.PREFIX}to <#room>` to switch, or start a single message "
                f"with a room name to send just that line there.")
            return

        want = room if room.startswith("#") else f"#{room}"
        if bridge.set_reply_target(name, want):
            await ctx.send(f"Messages typed here now go to **{want}**.")
        else:
            await ctx.send(
                f"**{want}** is not bridged. Available: {', '.join(rooms) or 'none'}")

    @commands.command(name="irckick")
    @mod_only()
    async def irc_kick(self, ctx: commands.Context, nick: str, *, reason: str = ""):
        """Kick someone from the IRC room this channel is bridged to."""
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        blocked = self._can_act(irc_ch)
        if blocked:
            await ctx.send(f"❌ {blocked}")
            return
        if not self._bridge.is_nick_in_channel(nick, irc_ch):
            await ctx.send(f"⚠️ **{nick}** is not in `{irc_ch}` — nothing to kick.")
            return
        why = reason.strip() or f"Kicked by {ctx.author.display_name}"
        self._bridge.kick_irc(nick, why, channel=irc_ch)
        await ctx.send(f"👢 Kicked **{nick}** from `{irc_ch}` — {why}")

    @commands.command(name="ircban")
    @mod_only()
    async def irc_ban(self, ctx: commands.Context, nick: str, *, reason: str = ""):
        """Ban and remove someone from the bridged IRC room.

        Bans the HOST when we know it. A `nick!*@*` ban is undone by typing
        `/nick` once, which makes it decoration rather than a ban.
        """
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        blocked = self._can_act(irc_ch)
        if blocked:
            await ctx.send(f"❌ {blocked}")
            return
        host = self._bridge.host_of(nick)
        mask = f"*!*@{host.split('@')[-1]}" if "@" in host else f"{nick}!*@*"
        why = reason.strip() or f"Banned by {ctx.author.display_name}"
        self._bridge.send_raw(f"MODE {irc_ch} +{_BAN} {mask}")
        self._bridge.kick_irc(nick, why, channel=irc_ch)
        note = "" if "@" in host else ("\n⚠️ I don't know their host, so this bans the "
                                       "nick only — they can evade it with `/nick`.")
        await ctx.send(f"🔨 Banned `{mask}` from `{irc_ch}` — {why}{note}")

    @commands.command(name="ircunban")
    @mod_only()
    async def irc_unban(self, ctx: commands.Context, mask: str):
        """Lift a ban. Takes the mask exactly as $ircbans lists it."""
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        blocked = self._can_act(irc_ch)
        if blocked:
            await ctx.send(f"❌ {blocked}")
            return
        self._bridge.send_raw(f"MODE {irc_ch} -{_BAN} {mask}")
        await ctx.send(f"🕊️ Lifted `{mask}` in `{irc_ch}`.")

    # ── Status ───────────────────────────────────────────────────────────────

    @commands.command(name="op")
    @mod_only()
    async def op(self, ctx: commands.Context, nick: str):
        """Give operator status in the bridged room."""
        await self._mode(ctx, "+", _OP, nick, "Opped")

    @commands.command(name="deop")
    @mod_only()
    async def deop(self, ctx: commands.Context, nick: str):
        """Take operator status away."""
        await self._mode(ctx, "-", _OP, nick, "De-opped")

    @commands.command(name="voice")
    @mod_only()
    async def voice(self, ctx: commands.Context, nick: str):
        """Give voice — lets them speak while the room is moderated."""
        await self._mode(ctx, "+", _VOICE, nick, "Voiced")

    @commands.command(name="devoice")
    @mod_only()
    async def devoice(self, ctx: commands.Context, nick: str):
        """Take voice away. In a moderated room this silences them."""
        await self._mode(ctx, "-", _VOICE, nick, "De-voiced")

    @commands.command(name="mute")
    @mod_only()
    async def mute(self, ctx: commands.Context, nick: str):
        """Quiet someone (+q). They stay in the room and cannot talk.

        Softer than a kick and it survives a rejoin, which a de-voice does not.
        """
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        blocked = self._can_act(irc_ch)
        if blocked:
            await ctx.send(f"❌ {blocked}")
            return
        host = self._bridge.host_of(nick)
        mask = f"*!*@{host.split('@')[-1]}" if "@" in host else f"{nick}!*@*"
        self._bridge.send_raw(f"MODE {irc_ch} +{_QUIET} {mask}")
        await ctx.send(f"🔇 Quieted `{mask}` in `{irc_ch}`. `{config.PREFIX}unmute {mask}` to undo.")

    @commands.command(name="unmute")
    @mod_only()
    async def unmute(self, ctx: commands.Context, mask: str):
        """Undo a quiet. Takes the mask $mute printed."""
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        blocked = self._can_act(irc_ch)
        if blocked:
            await ctx.send(f"❌ {blocked}")
            return
        self._bridge.send_raw(f"MODE {irc_ch} -{_QUIET} {mask}")
        await ctx.send(f"🔊 Un-quieted `{mask}` in `{irc_ch}`.")

    @commands.command(name="ircwho")
    async def irc_who(self, ctx: commands.Context):
        """Who is in the bridged room, and can Luna actually act there.

        Answers the question every failed moderation command raises: is Luna
        even in the right place with the right powers.
        """
        irc_ch, err = self._resolve(ctx)
        if err:
            await ctx.send(f"❌ {err}")
            return
        bridge = self._bridge
        nicks = sorted(bridge.get_channel_nicks(irc_ch))
        opped = bridge.has_prefix(irc_ch, bridge.nick)
        here = bridge.is_nick_in_channel(bridge.nick, irc_ch)
        status = ("✅ opped — commands will work" if opped and here else
                  "⚠️ present but not opped — kicks and modes will be ignored"
                  if here else "❌ not in the room")
        body = ", ".join(nicks[:60]) or "(nobody)"
        if len(nicks) > 60:
            body += f" … +{len(nicks) - 60} more"
        await ctx.send(f"**`{irc_ch}`** — {len(nicks)} present. Luna: {status}\n{body}"[:1900])


async def setup(bot: commands.Bot):
    await bot.add_cog(IrcModCog(bot))

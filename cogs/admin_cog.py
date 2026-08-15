"""
Admin Cog — BatBot monitoring + Discord/IRC moderation.

BatBot health check runs every 5 minutes. Posts playful jokes when BatBot
goes offline and celebrates when he comes back.

Mod commands require the MOD_ROLE or kick_members/ban_members Discord permission.
"""

import asyncio
import datetime
import json
import os
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

import config

# ── Warnings storage ──────────────────────────────────────────────────────────

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_WARN_FILE = os.path.join(_DATA_DIR, "warnings.json")


def _load_warnings() -> dict:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(_WARN_FILE):
        return {}
    with open(_WARN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_warnings(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_WARN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _add_warning(guild_id: int, user_id: int, reason: str, by: str) -> int:
    """Add a warning. Returns new warning count."""
    data     = _load_warnings()
    gid      = str(guild_id)
    uid      = str(user_id)
    entry    = {"reason": reason, "by": by, "at": datetime.datetime.utcnow().isoformat()}
    updated  = {
        **data,
        gid: {
            **data.get(gid, {}),
            uid: [*data.get(gid, {}).get(uid, []), entry],
        },
    }
    _save_warnings(updated)
    return len(updated[gid][uid])


def _get_warnings(guild_id: int, user_id: int) -> list:
    data = _load_warnings()
    return data.get(str(guild_id), {}).get(str(user_id), [])


def _clear_warnings(guild_id: int, user_id: int) -> None:
    data    = _load_warnings()
    gid     = str(guild_id)
    uid     = str(user_id)
    updated = {
        **data,
        gid: {k: v for k, v in data.get(gid, {}).items() if k != uid},
    }
    _save_warnings(updated)


# ── Permission helper ─────────────────────────────────────────────────────────

def _is_mod(ctx: commands.Context) -> bool:
    """True if the caller has mod permissions or the configured MOD_ROLE."""
    member = ctx.author
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    if member.guild_permissions.kick_members or member.guild_permissions.ban_members:
        return True
    role_name = config.MOD_ROLE.lower()
    return any(r.name.lower() == role_name for r in member.roles)


def mod_only():
    async def predicate(ctx: commands.Context):
        if not _is_mod(ctx):
            await ctx.send("🚫 You don't have permission to use that command.")
            return False
        return True
    return commands.check(predicate)


# ── Cog ───────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog, name="Admin"):
    """BatBot monitoring and moderation commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        pass

    # ── IRC bridge helper ─────────────────────────────────────────────────────

    @property
    def _bridge(self):
        return getattr(self.bot, "_irc_bridge", None)

    # ── BatBot health checks ──────────────────────────────────────────────────

    async def _http_alive(self) -> bool:
        """Ping BatBot's keep-alive URL. Returns True if reachable."""
        url = config.BATBOT_REPLIT_URL
        if not url:
            return True   # No URL configured — can't check, assume OK
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    return resp.status == 200
        except Exception:
            return False

    def _irc_alive(self) -> bool:
        """Check if BatBot's IRC nick is present in #BatCave."""
        nick = config.BATBOT_IRC_NICK
        if not nick or not self._bridge:
            return True   # Can't check — assume OK
        return self._bridge.is_nick_in_channel(nick)

    # ── BatBot status command ─────────────────────────────────────────────────

    @commands.command(name="batstatus", aliases=["bs", "batcheck"])
    async def batstatus(self, ctx: commands.Context):
        """Check if BatBot is alive on IRC and Replit."""
        async with ctx.typing():
            http_ok = await self._http_alive()
            irc_ok  = self._irc_alive()

        irc_status  = "✅ In channel" if irc_ok  else "❌ Not found"
        http_status = "✅ Reachable"  if http_ok else ("❌ Unreachable" if config.BATBOT_REPLIT_URL else "⚠️ URL not set")
        is_up       = http_ok or irc_ok

        em = discord.Embed(
            title = "🦇 BatBot Status",
            color = 0x00FF88 if is_up else 0xFF4444,
        )
        em.add_field(name="IRC Presence",      value=irc_status,  inline=True)
        em.add_field(name="Replit Keep-Alive", value=http_status, inline=True)
        em.add_field(name="Overall",           value="🟢 Online" if is_up else "🔴 Offline", inline=True)
        await ctx.send(embed=em)

    # ── Discord mod commands ──────────────────────────────────────────────────

    @commands.command(name="kick")
    @mod_only()
    async def kick_member(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given"):
        """Kick a member from the Discord server."""
        if member.top_role >= ctx.author.top_role and not ctx.author.guild_permissions.administrator:
            await ctx.send("🚫 You can't kick someone with an equal or higher role.")
            return
        try:
            await member.kick(reason=f"{reason} (by {ctx.author})")
            em = discord.Embed(
                title       = "👢 Kicked",
                description = f"**{member}** has been kicked from the cave.",
                color       = 0xFF6600,
            )
            em.add_field(name="Reason", value=reason)
            em.add_field(name="By",     value=ctx.author.mention)
            await ctx.send(embed=em)
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to kick that user.")

    @commands.command(name="ban")
    @mod_only()
    async def ban_member(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given"):
        """Permanently ban a member from the Discord server."""
        if member.top_role >= ctx.author.top_role and not ctx.author.guild_permissions.administrator:
            await ctx.send("🚫 You can't ban someone with an equal or higher role.")
            return
        try:
            await member.ban(reason=f"{reason} (by {ctx.author})", delete_message_days=1)
            em = discord.Embed(
                title       = "🔨 Banned",
                description = f"**{member}** has been banished from the cave. 🌑",
                color       = 0xFF0000,
            )
            em.add_field(name="Reason", value=reason)
            em.add_field(name="By",     value=ctx.author.mention)
            await ctx.send(embed=em)
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to ban that user.")

    @commands.command(name="unban")
    @mod_only()
    async def unban_member(self, ctx: commands.Context, user_id: int, *, reason: str = "Forgiven"):
        """Unban a user by their Discord ID. Usage: !!unban 123456789"""
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user, reason=f"{reason} (by {ctx.author})")
            await ctx.send(f"✅ **{user}** has been unbanned. The moon forgives... this time.")
        except discord.NotFound:
            await ctx.send("❌ That user ID wasn't found in the ban list.")
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to unban users.")

    @commands.command(name="mute", aliases=["timeout"])
    @mod_only()
    async def mute_member(self, ctx: commands.Context, member: discord.Member, minutes: int = 10, *, reason: str = "No reason given"):
        """Timeout a member. Usage: !!mute @user 30 spamming"""
        if minutes < 1 or minutes > 40320:   # Discord max: 28 days
            await ctx.send("❌ Duration must be between 1 and 40320 minutes (28 days).")
            return
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        try:
            await member.timeout(until, reason=f"{reason} (by {ctx.author})")
            em = discord.Embed(
                title       = "🔇 Muted",
                description = f"**{member.mention}** has been silenced for {minutes} minute(s). 🌑",
                color       = 0xAA00FF,
            )
            em.add_field(name="Reason",    value=reason)
            em.add_field(name="Duration",  value=f"{minutes} min")
            em.add_field(name="By",        value=ctx.author.mention)
            await ctx.send(embed=em)
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to timeout that user.")

    @commands.command(name="unmute", aliases=["untimeout"])
    @mod_only()
    async def unmute_member(self, ctx: commands.Context, member: discord.Member):
        """Remove a timeout from a member."""
        try:
            await member.timeout(None)
            await ctx.send(f"🔈 **{member.mention}** has been unmuted. The moon is merciful.")
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to remove that timeout.")

    @commands.command(name="warn")
    @mod_only()
    async def warn_member(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        """Warn a member. Sends them a DM and logs it."""
        count = _add_warning(ctx.guild.id, member.id, reason, str(ctx.author))

        # Try to DM the user
        try:
            dm_em = discord.Embed(
                title       = "⚠️ Warning — BatCave",
                description = f"You have been warned in **{ctx.guild.name}**.",
                color       = 0xFFAA00,
            )
            dm_em.add_field(name="Reason",        value=reason)
            dm_em.add_field(name="Total Warnings", value=str(count))
            await member.send(embed=dm_em)
            dm_sent = "DM sent ✅"
        except discord.Forbidden:
            dm_sent = "DM blocked ❌"

        em = discord.Embed(
            title       = f"⚠️ Warning #{count}",
            description = f"**{member.mention}** has been warned.",
            color       = 0xFFAA00,
        )
        em.add_field(name="Reason",        value=reason)
        em.add_field(name="Total Warnings", value=str(count))
        em.add_field(name="By",            value=ctx.author.mention)
        em.add_field(name="DM",            value=dm_sent)
        await ctx.send(embed=em)

    @commands.command(name="warnings", aliases=["warns"])
    @mod_only()
    async def show_warnings(self, ctx: commands.Context, member: discord.Member):
        """Show all warnings for a member."""
        warns = _get_warnings(ctx.guild.id, member.id)
        if not warns:
            await ctx.send(f"✅ **{member}** has no warnings. A rare and pure soul.")
            return
        em = discord.Embed(
            title = f"⚠️ Warnings for {member}",
            color = 0xFFAA00,
        )
        for i, w in enumerate(warns, 1):
            em.add_field(
                name  = f"#{i} — {w['at'][:10]}",
                value = f"**{w['reason']}** (by {w['by']})",
                inline=False,
            )
        await ctx.send(embed=em)

    @commands.command(name="clearwarn", aliases=["clearwarnings"])
    @mod_only()
    async def clear_warnings(self, ctx: commands.Context, member: discord.Member):
        """Clear all warnings for a member."""
        _clear_warnings(ctx.guild.id, member.id)
        await ctx.send(f"🧹 All warnings cleared for **{member}**. Fresh start, dark soul.")

    @commands.command(name="purge", aliases=["deletemsg"])
    @mod_only()
    async def purge_messages(self, ctx: commands.Context, count: int = 10):
        """Delete the last N messages in this channel (max 100)."""
        if count < 1 or count > 100:
            await ctx.send("❌ Count must be between 1 and 100.")
            return
        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=count)
        msg = await ctx.send(f"🧹 Swept away {len(deleted)} message(s) into the void.")
        await asyncio.sleep(3)
        await msg.delete()

    @commands.command(name="slowmode")
    @mod_only()
    async def slowmode(self, ctx: commands.Context, seconds: int = 0):
        """Set channel slowmode. 0 to disable. Usage: !!slowmode 10"""
        if seconds < 0 or seconds > 21600:
            await ctx.send("❌ Slowmode must be between 0 and 21600 seconds.")
            return
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await ctx.send("⚡ Slowmode disabled. Chaos can resume.")
        else:
            await ctx.send(f"🐢 Slowmode set to **{seconds}s** in this channel.")

    # ── IRC mod commands ──────────────────────────────────────────────────────

    @commands.command(name="irckick")
    @mod_only()
    async def irc_kick(self, ctx: commands.Context, nick: str, *, reason: str = "Kicked from Discord"):
        """Kick a user from IRC #BatCave. Luna must be an IRC op."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge is not running.")
            return
        success = self._bridge.kick_irc(nick, reason)
        if success:
            await ctx.send(f"👢 Sent IRC KICK for **{nick}** — reason: *{reason}*\n*(Luna needs to be an IRC op for this to work)*")
        else:
            await ctx.send("❌ IRC bridge is not connected right now.")

    @commands.command(name="ircban")
    @mod_only()
    async def irc_ban(self, ctx: commands.Context, nick: str):
        """Ban a user from IRC #BatCave (MODE +b). Luna must be an IRC op."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge is not running.")
            return
        success = self._bridge.ban_irc(nick)
        if success:
            await ctx.send(f"🔨 Sent IRC MODE +b for **{nick}**\n*(Luna needs to be an IRC op for this to work)*")
        else:
            await ctx.send("❌ IRC bridge is not connected right now.")

    @commands.command(name="ircnicks", aliases=["who"])
    async def irc_nicks(self, ctx: commands.Context, irc_channel: str = ""):
        """Show who's currently in an IRC channel (defaults to mapped channel)."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        ch    = irc_channel or self._bridge.get_irc_for_discord(ctx.channel.name) or config.IRC_CHANNEL
        nicks = self._bridge.get_channel_nicks(ch)
        if not nicks:
            await ctx.send(f"🌑 No one tracked in **{ch}** yet.")
            return
        nick_list = " • ".join(sorted(nicks, key=str.lower))
        em = discord.Embed(
            title       = f"👥 IRC {ch} — {len(nicks)} soul(s)",
            description = nick_list,
            color       = config.BOT_COLOR,
        )
        await ctx.send(embed=em)

    # ── IRC bridge management ─────────────────────────────────────────────────

    @commands.command(name="ircjoin")
    @mod_only()
    async def irc_join(self, ctx: commands.Context, irc_channel: str,
                       discord_channel: str = ""):
        """Bridge this Discord channel to an IRC channel.
        Usage: !!ircjoin #IRCroom [#discord-channel]"""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        disc_ch = discord_channel.lstrip("#") if discord_channel else ctx.channel.name
        ok = self._bridge.join_channel(irc_channel, disc_ch)
        if ok:
            await ctx.send(
                f"🌉 Bridge added: **#{disc_ch}** ↔ **{irc_channel}**\n"
                f"Luna is joining that IRC channel now."
            )
        else:
            await ctx.send(f"ℹ️ **#{disc_ch}** is already bridged to **{irc_channel}**.")

    @commands.command(name="ircleave", aliases=["ircpart"])
    @mod_only()
    async def irc_leave(self, ctx: commands.Context, irc_channel: str):
        """Remove the bridge to an IRC channel and PART it.
        Usage: !!ircleave #IRCroom"""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        self._bridge.leave_channel(irc_channel)
        await ctx.send(f"🚪 Bridge removed: Luna has left **{irc_channel}**.")

    @commands.command(name="ircbridges", aliases=["relay", "bridges"])
    async def irc_bridges(self, ctx: commands.Context):
        """List all active Discord↔IRC bridge mappings."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        pairs = self._bridge.list_bridges()
        if not pairs:
            await ctx.send("🌑 No active bridges.")
            return
        lines = [f"• **#{d}** ↔ **{i}**" for d, i in sorted(pairs)]
        em = discord.Embed(
            title       = "🌉 Active IRC Bridges",
            description = "\n".join(lines),
            color       = config.BOT_COLOR,
        )
        await ctx.send(embed=em)

    @commands.command(name="ircping")
    async def irc_ping(self, ctx: commands.Context):
        """Check IRC connection status."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        if self._bridge.is_connected():
            latency_ms = round(self.bot.latency * 1000)
            await ctx.send(
                f"🟢 IRC connected | Discord latency: **{latency_ms}ms**"
            )
        else:
            await ctx.send("🔴 Not connected to IRC. Try `!!ircreconnect`.")

    @commands.command(name="ircinfo")
    async def irc_info(self, ctx: commands.Context):
        """Show IRC bridge status — server, nick, bridges, uptime."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        pairs     = self._bridge.list_bridges()
        status    = "🟢 Connected" if self._bridge.is_connected() else "🔴 Disconnected"
        bridge_txt = "\n".join(f"• #{d} ↔ {i}" for d, i in pairs) or "None"
        em = discord.Embed(title="🌙 Luna IRC Info", color=config.BOT_COLOR)
        em.add_field(name="Status",     value=status,                inline=True)
        em.add_field(name="Server",     value=f"`{config.IRC_SERVER}`", inline=True)
        em.add_field(name="Nick",       value=f"`{config.IRC_NICK}`",   inline=True)
        em.add_field(name="Bridges",    value=bridge_txt,             inline=False)
        await ctx.send(embed=em)

    @commands.command(name="irctopic")
    async def irc_topic(self, ctx: commands.Context, irc_channel: str = ""):
        """Show the current IRC channel topic."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        ch    = irc_channel or self._bridge.get_irc_for_discord(ctx.channel.name) or config.IRC_CHANNEL
        topic = self._bridge.get_topic(ch)
        if topic:
            await ctx.send(f"📌 **Topic for {ch}:** {topic}")
        else:
            await ctx.send(f"📌 No topic cached for **{ch}** yet.")

    @commands.command(name="ircreconnect")
    @mod_only()
    async def irc_reconnect(self, ctx: commands.Context):
        """Force Luna to drop and re-establish the IRC connection."""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        self._bridge.reconnect()
        await ctx.send("🔄 Force-reconnect triggered. Luna will rejoin IRC momentarily.")

    @commands.command(name="ircnick")
    @mod_only()
    async def irc_nick(self, ctx: commands.Context, new_nick: str):
        """Change Luna's IRC nick. Usage: !!ircnick NewNick"""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        ok = self._bridge.change_nick(new_nick)
        if ok:
            await ctx.send(f"🏷️ Sent IRC NICK change → **{new_nick}**")
        else:
            await ctx.send("❌ IRC bridge is not connected right now.")

    @commands.command(name="ircraw")
    @commands.is_owner()
    async def irc_raw(self, ctx: commands.Context, *, command: str):
        """Send a raw IRC command (owner only). Example: !!ircraw MODE #BatCave +m"""
        if not self._bridge:
            await ctx.send("❌ IRC bridge not running.")
            return
        self._bridge.send_raw(command)
        await ctx.send(f"📡 Sent: `{command}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))

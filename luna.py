#!/usr/bin/env python3
"""
Luna — IRC Relay Bot
Pure relay bot: bridges Discord channels to IRC channels.
No AI, no tarot, no economy. Just relay + IRC management commands.

Setup:
  export DISCORD_TOKEN=your_token
  export DISCORD_GUILD_ID=your_server_id
  python3 luna.py
"""

import asyncio
import os
import discord
from discord.ext import commands

import config
from discord.ext import tasks
from utils.irc_bridge import IRCBridge
from utils.staralign_outbound import fetch_tagged_messages, is_configured as _outbound_configured
from utils.relay_state import (
    RELAY_TO_IRC,
    RELAY_TO_STARALIGN,
    relay_state,
)
from utils.staralign_relay import relay_to_staralign

_luna_ready_fired = False   # guard: prevents duplicate on_ready init
_bridge_started   = False   # guard: prevents duplicate IRC bridge start
_outbound_cursor  = 0       # last StarAlign msg ts pulled (reverse relay)
_outbound_started = False   # guard: prevents duplicate poll loop start

# ── Intents ───────────────────────────────────────────────────────────────────

intents                 = discord.Intents.default()
intents.message_content = True
intents.members         = True

# ── Bot setup ─────────────────────────────────────────────────────────────────

bot = commands.Bot(
    command_prefix = config.PREFIX,
    intents        = intents,
    help_command   = None,
)

bridge          = IRCBridge(bot)
bot._irc_bridge = bridge   # exposed to cogs


def _credit() -> str:
    """Author credit for !!about. Empty unless LUNA_CREDIT is set, so this
    public repo never carries a real person's handle."""
    who = os.getenv("LUNA_CREDIT", "").strip()
    return f" created by **{who}**" if who else ""



COGS = [
    "cogs.admin_cog",
    "cogs.ai_cog",
    "cogs.ircmod_cog",
    "cogs.economy_cog",
    "cogs.shared_cog",
    "cogs.social_cog",
    "cogs.spells_cog",
    "cogs.tarot_cog",
]

# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _luna_ready_fired, _bridge_started
    if _luna_ready_fired:
        print("[luna] on_ready fired again — ignored (double-login guard).")
        return
    _luna_ready_fired = True
    print(f"[luna] Logged in as {bot.user} ({bot.user.id})")
    print(f"[luna] Prefix: {config.PREFIX}")
    for g in bot.guilds:
        print(f"[luna] Guild: {g.name} | ID: {g.id} | Members: {g.member_count}")
        print("[luna] TextChannels: " + ", ".join(repr(c.name) for c in g.text_channels))

    if not _bridge_started:
        _bridge_started = True
        bridge.start(asyncio.get_event_loop())
    else:
        print("[luna] IRC bridge already running, skipping.")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="the bridge 🌉"
        )
    )

    # Reverse relay: poll StarAlign for messages that tag the bot so the
    # Vampire bot (in the Discord bridge channel) can answer them.
    global _outbound_started, _outbound_cursor
    if not _outbound_started and _outbound_configured():
        import time as _t
        _outbound_cursor = int(_t.time() * 1000)  # start "now" — skip history
        _outbound_started = True
        staralign_outbound_poll.start()
        print("[luna] StarAlign reverse relay (tag-the-bot) active.")

    print("[luna] Ready. Relay bot is live.")


def _find_bridge_discord_channel() -> "discord.TextChannel | None":
    """Locate the Discord text channel that mirrors the bridge."""
    target = (config.BRIDGE_CHANNEL or "").lower()
    for guild in bot.guilds:
        if config.DISCORD_GUILD_ID and guild.id != config.DISCORD_GUILD_ID:
            continue
        for channel in guild.text_channels:
            if channel.name.lower() == target:
                return channel
    return None


# Poll interval: 6s meant ~14,400 HTTP calls/day for a low-traffic relay.
# 20s cuts that by ~70% with no practical latency cost. Tune via STARALIGN_POLL_SECS.
@tasks.loop(seconds=float(os.getenv("STARALIGN_POLL_SECS", "20")))
async def staralign_outbound_poll() -> None:
    """Forward StarAlign users' @bot messages into the Discord channel."""
    global _outbound_cursor
    if not relay_state.is_enabled(RELAY_TO_STARALIGN):
        return
    try:
        messages = await fetch_tagged_messages(_outbound_cursor)
    except Exception as e:  # noqa: BLE001 - never let the loop die
        print(f"[luna] outbound poll error: {e}")
        return
    if not messages:
        return

    channel = _find_bridge_discord_channel()
    if channel is None:
        return

    for msg in messages:
        _outbound_cursor = max(_outbound_cursor, msg.ts)
        # Posted by Luna → her own on_message skips it (no loop). The
        # Vampire bot sees it and replies; that reply relays back as "bot".
        try:
            tagged = f"\x0313:discord:\x03 {msg.sender_name}: {msg.text}"
            await channel.send(tagged)
            # Also relay directly to IRC so the lounge stays busy
            # (Luna skips its own Discord messages, so we push IRC ourselves)
            bridge.send_to_irc(tagged, discord_channel=config.BRIDGE_CHANNEL)
        except Exception as e:  # noqa: BLE001
            print(f"[luna] outbound send error: {e}")


@staralign_outbound_poll.before_loop
async def _before_outbound_poll() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_message(message: discord.Message):
    # Never relay Luna's own messages (prevents an infinite relay loop).
    if bot.user is not None and message.author.id == bot.user.id:
        return

    # Other bots in the bridged channel (e.g. the Vampire bot) ARE relayed,
    # but as the single shared identity "bot" on StarAlign.
    author_is_bot = bool(message.author.bot)
    relay_kind = "bot" if author_is_bot else "user"
    relay_username = "bot" if author_is_bot else message.author.display_name

    # Relay Discord → IRC for any channel that has a bridge mapping.
    # Suppress command messages (~prefix) — they stay in Discord only.
    if (
        message.guild
        and hasattr(message.channel, "name")
        and not message.clean_content.startswith(config.PREFIX)
        and bridge.get_irc_for_discord(message.channel.name)
    ):
        # Discord -> IRC relay. Screen it first: relayed text arrives in IRC
        # under Luna's opped nick, which every moderator bot treats as exempt,
        # so nothing downstream will ever check it.
        blocked = None
        try:
            blocked = bridge.moderator.screen_relay(message.clean_content)
        except Exception as e:  # noqa: BLE001 — never break the relay
            print(f"[luna] relay screen error: {e}")
        if blocked:
            print(f"[luna] withheld a message from IRC: {blocked}")
            try:
                await message.channel.send(
                    f"*(not carried across — {blocked})*", delete_after=20)
            except Exception:
                pass
            await bot.process_commands(message)
            return

        if relay_state.is_enabled(RELAY_TO_IRC):
            irc_msg = f"\x0313:discord:\x03 {relay_username}: {message.clean_content[:380]}"
            bridge.send_to_irc(irc_msg, discord_channel=message.channel.name)
            relay_state.stats.record_message()
            relay_state.recent.append(
                "to_irc", relay_username,
                message.clean_content[:200],
            )

        # Discord -> StarAlign relay (fire-and-forget)
        if relay_state.is_enabled(RELAY_TO_STARALIGN):
            asyncio.ensure_future(
                relay_to_staralign(
                    username=relay_username,
                    text=message.clean_content[:500],
                    kind=relay_kind,
                )
            )

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 You don't have permission to use that command.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Check `{config.PREFIX}help`.")
        return
    print(f"[luna] Command error in {ctx.command}: {error}")


# ── Help ──────────────────────────────────────────────────────────────────────

@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx):
    """Show all available commands."""
    p = config.PREFIX
    em = discord.Embed(
        title       = "🌉 Luna — relay",
        description = (
            "Luna carries this channel to a linked IRC room and back. "
            "Anything said in a bridged channel crosses over."
        ),
        color       = config.BOT_COLOR,
    )
    em.add_field(
        name  = "🌉 The bridge",
        value = f"`{p}ircping` `{p}ircinfo` `{p}ircbridges` — status\n"
                f"`{p}ircwho` — who is in the IRC room, and can I act there\n"
                f"`{p}ircnicks [#irc]` · `{p}irctopic [#irc]` — who/what is there\n"
                f"`{p}ircjoin #irc [#discord]` · `{p}ircleave #irc` — bridges *(mod)*",
        inline=False,
    )
    em.add_field(
        name  = "🔨 Moderate IRC from here *(mod)*",
        value = f"`{p}op` `{p}deop` `{p}voice` `{p}devoice` <nick>\n"
                f"`{p}irckick <nick> [reason]` · `{p}ircban <nick> [reason]`\n"
                f"`{p}mute <nick>` · `{p}unmute <mask>` · `{p}ircunban <mask>`\n"
                f"*Acts on the IRC room bridged to whichever channel you type in.*",
        inline=False,
    )
    em.add_field(
        name  = "🔧 Connection *(mod)*",
        value = f"`{p}ircnick <nick>` · `{p}ircreconnect` · "
                f"`{p}ircraw <cmd>` — raw IRC *(owner)*",
        inline=False,
    )
    em.add_field(
        name  = "🎭 Everyday",
        value = f"`{p}ai <question>` — ask Luna, or just say her name\n"
                f"`{p}roll` `{p}flip` `{p}choose` `{p}calc` `{p}weather` `{p}ping`",
        inline=False,
    )
    # Discord moderation was removed: Discord does all of it natively, with an
    # audit log, and a relay bot reimplementing it is a second place to get
    # bans wrong.
    em.set_footer(text=f"Prefix: {p}  |  A relay, not a moderator.")
    await ctx.send(embed=em)


@bot.command(name="about")
async def about(ctx):
    em = discord.Embed(
        title       = "🌉 About Luna",
        description = (
            # Public repo: the author credit comes from a secret, not source.
            f"**Luna** is a pure IRC relay bot{_credit()}.\n\n"
            "She bridges Discord channels to IRC channels — "
            "everything said in a bridged Discord channel appears in IRC, "
            "and vice versa.\n\n"
            f"Use `{config.PREFIX}ircjoin #ircchannel` to activate a bridge in any Discord channel."
        ),
        color=config.BOT_COLOR,
    )
    em.add_field(name="Prefix",  value=config.PREFIX,              inline=True)
    em.add_field(name="IRC",     value=config.IRC_SERVER,          inline=True)
    em.add_field(name="Help",    value=f"`{config.PREFIX}help`",                 inline=True)
    em.set_footer(text="Relay bot — always listening, never talking. 🌉")
    await ctx.send(embed=em)


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"🏓 `{round(bot.latency * 1000)}ms`")


# ── Startup ───────────────────────────────────────────────────────────────────

async def main():
    async with bot:
        for cog in COGS:
            try:
                await bot.load_extension(cog)
                print(f"[luna] Loaded: {cog}")
            except Exception as e:
                print(f"[luna] Failed to load {cog}: {e}")

        if not config.DISCORD_TOKEN:
            print("[luna] ERROR: DISCORD_TOKEN not set.")
            return

        await bot.start(config.DISCORD_TOKEN)


def _install_signal_handlers() -> None:
    """Leave IRC cleanly when the host stops us.

    GitHub Actions sends SIGTERM at the job timeout — roughly four times a day.
    Without a QUIT the old session lingers until ping-timeout and the NEXT run
    finds Luna1 taken, so it lands on Luna1_ and has to ghost its way back.
    """
    import signal

    def _bye(signum, _frame):
        print(f"[luna] signal {signum} — leaving IRC cleanly.")
        try:
            bridge.quit()
        except Exception as e:  # noqa: BLE001 — never block the exit
            print(f"[luna] quit error: {e}")
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass          # not the main thread / unsupported platform


if __name__ == "__main__":
    import errno as _errno
    import socket as _socket

    _install_signal_handlers()

    # ── Single-instance lock — prevents duplicate Luna processes ──────────
    try:
        import fcntl as _fcntl
        _lockfile = open("/tmp/luna.lock", "w")
        _fcntl.flock(_lockfile, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except ImportError:
        pass   # Windows / non-Unix — skip lock
    except BlockingIOError:
        print("[luna] Another Luna is already running. Exiting.")
        raise SystemExit(0)

    _LUNA_PORT = int(os.getenv("LUNA_KEEP_ALIVE_PORT", "8081"))

    try:
        from keep_alive import keep_alive, set_bot_ref
        set_bot_ref(bot, bridge)
        keep_alive(port=_LUNA_PORT)   # keep_alive binds with SO_REUSEADDR
    except OSError as _e:
        import errno as _errno
        if _e.errno in (_errno.EADDRINUSE, 98):
            print(f"[luna] Port {_LUNA_PORT} in use — another Luna is running. Exiting.")
            raise SystemExit(0)
        print(f"[dashboard] {_e}")
    except Exception as e:
        print(f"[dashboard] {e}")

    print("🌉 Luna relay bot starting...")
    asyncio.run(main())

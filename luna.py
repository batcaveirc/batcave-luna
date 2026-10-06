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
    # Counts the days each IRC nick was heard on, out of the relayed history
    # Discord already keeps, and hands the tally to Dracula. Dracula's own
    # promotion could never see a regular: it counts messages in memory and the
    # host hands over every six hours, so the count restarts before anybody
    # ordinary reaches it. Evidence only — Dracula applies every gate itself.
    "cogs.attendance_cog",
    "cogs.ircmod_cog",
    # The moderator memory neither bot has. Dracula's warns, strike counts and
    # cross-room sightings all live in maps that die with its process every six
    # hours, so "has this person been warned before?" had no true answer. Discord
    # keeps what it is told, so the record lives here.
    "cogs.records_cog",
    # The three things only Luna can do: IRC has no scrollback and no memory,
    # and Dracula forgets everything at each six-hourly handover. Discord keeps
    # what it is told, so search, offline messages and room activity live here.
    "cogs.memory_cog",
    "cogs.shared_cog",
    "cogs.social_cog",
]

# REMOVED, on the owner's instruction to cut what is not needed:
#
#   economy_cog  — it cannot work here. Balances live in data/shards.json,
#                  that directory is not tracked in git, and the runner is
#                  ephemeral: every job ends and takes the file with it. So
#                  $shards, $richest and the leaderboard silently reset every
#                  few hours and always have. A scoreboard that forgets is
#                  worse than no scoreboard, because people trust it.
#   tarot_cog    — novelty, and excluded by this file's own description above.
#   spells_cog   — the same.
#
# ai_cog and social_cog are kept deliberately. The docstring says "no AI", but
# $ai is the thing people actually use Luna for — she answers by name in the
# room every day — and that line is simply stale.

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
        name  = "🎯 Which room am I talking to?",
        value = f"`{p}to` — where messages typed here go, and the alternatives\n"
                f"`{p}to <#room>` — point this channel at a different IRC room\n"
                f"*Or start one message with a room name to send just that line "
                f"there. Two IRC rooms feed this channel and a message can only "
                f"go to one, so without this the second room is unreachable.*",
        inline=False,
    )
    em.add_field(
        name  = "🌉 The bridge",
        value = f"`{p}ircinfo` — status: server, nick, bridges, uptime\n"
                f"`{p}ircwho` — who is in the IRC room, and can I act there\n"
                f"`{p}irctopic [#irc]` — the room's topic\n"
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
                f"`{p}roll` `{p}flip` `{p}choose` `{p}calc` `{p}weather` `{p}ping`\n"
                f"`{p}nicks` — who is in the IRC room · `{p}say <msg>` — cross-post\n"
                f"`{p}mod on|off` *(ops)* — the automatic cover Dracula cannot see",
        inline=False,
    )
    em.add_field(
        name  = "🧠 Memory",
        value = f"`{p}find <text>` — search what the room actually said (IRC has no\n"
                "scrollback; I do)\n"
                f"`{p}tell <nick> <msg>` — leave it, I hand it over when they next speak\n"
                f"`{p}quote [nick]` — something somebody actually said\n"
                f"`{p}onthisday [days]` — what was being said a week ago\n"
                f"`{p}stats` — who talks here, and when the room is awake\n"
                f"`{p}mood` — what sort of evening I am having",
        inline=False,
    )
    em.add_field(
        name  = "📋 Records",
        value = f"`{p}warn <nick> <reason>` *(mod)* — a warning that survives restarts\n"
                f"`{p}warnings <nick>` — the whole history, who gave it and when\n"
                f"`{p}clearwarns <nick>` *(mod)* · `{p}seen <nick>` — last heard from\n"
                f"`{p}slowmode <secs>` *(mod)* — one line per n seconds in the IRC\n"
                "room this channel bridges to; anyone faster is **devoiced**, never kicked",
        inline=False,
    )
    em.add_field(
        name  = "📊 Regulars *(mod)*",
        value = f"`{p}regulars [n]` — who talks here on the most separate days, "
                "counted from the relayed history\n"
                f"`{p}sendregulars` — hand that tally to Dracula now\n"
                "*Evidence only: Dracula still requires a registered account "
                "and a clean record before trusting anybody.*",
        inline=False,
    )
    # These were registered and completely undocumented — the only way to find
    # them was to read the source, which is not a discovery mechanism.
    em.add_field(
        name  = "🎲 Social",
        value = f"`{p}ship <a> <b>` `{p}vibe` `{p}tod` `{p}truth` `{p}dare`\n"
                f"`{p}confess` `{p}tea` `{p}seduce`",
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


def _dispatch_successor(reason: str) -> None:
    """Phase 1: ask GitHub Actions to start the next Luna run before we quit,
    so Andromeda is not offline while the throttled cron catches up.

    Trap: a workflow dispatched by GITHUB_TOKEN does NOT create a new run
    (GitHub's recursion guard). This needs GH_PAT — a PAT with 'workflow'
    scope. Without it we no-op and log, and the cron is the backstop (same as
    before Phase 1). Called from the signal handler, so this is BLOCKING on
    purpose — the HTTP request has to finish before SystemExit.
    """
    import urllib.request
    import urllib.error
    import json
    token = os.getenv("GH_PAT", "").strip()
    if not token:
        print(f"[luna] self-restart: no GH_PAT set — cron is the backstop ({reason})")
        return
    repo = os.getenv("GITHUB_REPOSITORY", "batcaveirc/batcave-luna")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/luna.yml/dispatches"
    body = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            print(f"[luna] self-restart: successor dispatched ({reason}, HTTP {res.status})")
    except urllib.error.HTTPError as e:
        print(f"[luna] self-restart: HTTP {e.code} — {e.read()[:120]!r} ({reason})")
    except Exception as e:  # noqa: BLE001 — must never block exit
        print(f"[luna] self-restart: {e} ({reason})")


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
        # Hand the room over to a fresh runner BEFORE SystemExit, so Andromeda
        # does not vanish while the throttled cron catches up.
        try:
            _dispatch_successor(f"signal {signum}")
        except Exception as e:  # noqa: BLE001 — must never block exit
            print(f"[luna] dispatch error: {e}")
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass          # not the main thread / unsupported platform


def _install_crash_recorders() -> None:
    """Protect against the exact failure mode we hit 2026-10-06: Luna run
    FAILED at 3h5m, GitHub purged the log blob before I could read it, and
    there was zero evidence left of what killed her. Now every unhandled
    exception — in the main thread, in a worker thread, or in a background
    task — is PRINTED to stdout BEFORE we exit, so the Actions log captures
    it. GitHub keeps stdout even when it later expires the blob store.
    """
    import sys as _sys
    import threading as _threading
    import traceback as _tb

    def _main_hook(exc_type, exc_value, exc_tb):
        print("[luna] ★★ UNHANDLED EXCEPTION in main thread — evidence below ★★", flush=True)
        _tb.print_exception(exc_type, exc_value, exc_tb)
        _sys.stdout.flush()
        # Call default hook too (which prints the same + exits).
        _sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        print(f"[luna] ★★ UNHANDLED EXCEPTION in thread {args.thread.name!r} — evidence below ★★", flush=True)
        _tb.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
        _sys.stdout.flush()
        # Default behaviour: don't kill the process, just the thread.

    _sys.excepthook = _main_hook
    _threading.excepthook = _thread_hook


if __name__ == "__main__":
    import errno as _errno
    import socket as _socket

    _install_crash_recorders()
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
    # Guard asyncio.run against a crash in main() or any task it awaits.
    # Without this, an unhandled exception in the Discord loop or any
    # background task propagates out and the process exits silently from
    # the host's perspective. We print the traceback so the Actions log
    # captures it (even though the blob store expires fast on failed
    # runs — stdout is preserved longer).
    try:
        asyncio.run(main())
    except SystemExit:
        raise                        # intentional exit (signal handler)
    except KeyboardInterrupt:
        print("[luna] KeyboardInterrupt — bye.", flush=True)
    except Exception as _exc:        # noqa: BLE001
        import traceback as _tb
        print("[luna] ★★ asyncio.run(main()) CRASHED — evidence below ★★", flush=True)
        _tb.print_exc()
        # Hand off to a fresh runner before we exit so Andromeda is not
        # offline during the cron gap. _dispatch_successor already no-ops
        # cleanly if GH_PAT is unset.
        try:
            _dispatch_successor("main-crashed")
        except Exception as _derr:   # noqa: BLE001
            print(f"[luna] dispatch on crash failed: {_derr}", flush=True)
        raise SystemExit(1)

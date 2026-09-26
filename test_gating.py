"""Who is allowed to use which command.

    python3 test_gating.py

Written after an audit found 26 of Luna's 43 commands carrying no authority
check at all — including $sendregulars, which triggers the HMAC-signed
attendance feed that decides who Dracula trusts, and $to, which re-routes where
a whole Discord channel's messages land in IRC. Both were open to anybody who
could type.

admin_cog's own docstring says "Mod commands require the MOD_ROLE or
kick_members/ban_members Discord permission". Six of its eleven commands did not
honour that. A contract stated in a docstring and not enforced in code is the
same failure as a command advertised in help that no handler answers: the room
believes something about the bot that is not true.

The static half of this file is the part that keeps paying: adding a new command
without a check fails the suite by DEFAULT, and opening one on purpose means
adding it to OPEN and saying why. That is the difference between a rule and a
rule somebody has to remember.
"""
import asyncio
import inspect
import sys
from unittest.mock import MagicMock

import discord
from discord.ext import commands

import config
from cogs import admin_cog, attendance_cog, ai_cog, ircmod_cog, memory_cog, records_cog, shared_cog, social_cog

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# Commands anybody may use, and the reason each one is open. Anything NOT in
# here must carry a check, so the default for a new command is "restricted".
OPEN = {
    "ai":       "talking to Luna is the point of her",
    "find":     "searching relay history the room already saw",
    "tell":     "leaving a message for someone who is offline",
    "stats":    "counts, nothing identifying",
    "mood":     "reads back the mood she is already showing",
    "seen":     "when somebody was last around",
    "quote":    "something the room already said, out loud again",
    "onthisday": "the room's own history, which is not private to mods",
    "irctopic": "read-only, and a channel topic is public by definition",
    "tea":      "social", "confess": "social", "truth": "social", "dare": "social",
    "ship":     "social", "seduce": "social", "tod": "social", "vibe": "social",
    "s":        "gated in the body by is_discord_authorized(), not by a decorator",
}

MODULES = [admin_cog, attendance_cog, ai_cog, ircmod_cog, memory_cog,
           records_cog, shared_cog, social_cog]

found = {}
for mod in MODULES:
    for _, klass in inspect.getmembers(mod, inspect.isclass):
        if klass.__module__ != mod.__name__:
            continue
        for attr in vars(klass).values():
            if isinstance(attr, commands.Command):
                found[attr.name] = (mod.__name__, attr)

print(f"— {len(found)} commands discovered —")
ungated = sorted(n for n, (_, cmd) in found.items() if not cmd.checks and n not in OPEN)
c("every command either carries a check or is listed as deliberately open",
  not ungated, f"no check and not in OPEN: {', '.join(ungated)}")

stale = sorted(n for n in OPEN if n not in found)
c("the OPEN list has no entries for commands that no longer exist",
  not stale, f"stale: {', '.join(stale)}")

print("\n— the ten that were open and should not have been —")
for name in ["to", "ircwho", "sendregulars", "regulars", "warnings",
             "batstatus", "ircnicks", "ircbridges", "ircping", "ircinfo"]:
    entry = found.get(name)
    c(f"${name} is gated", bool(entry and entry[1].checks),
      "not found at all" if not entry else "no check attached")

print("\n— what the gate actually decides —")


def role(name):
    # MagicMock(name=...) names the MOCK, it does not set a .name attribute —
    # so it has to be assigned afterwards or every role silently reads as a
    # repr string and the role check can never match.
    r = MagicMock()
    r.name = name
    return r


def member(admin=False, kick=False, ban=False, roles=()):
    m = MagicMock(spec=discord.Member)
    m.guild_permissions.administrator = admin
    m.guild_permissions.kick_members = kick
    m.guild_permissions.ban_members = ban
    m.roles = [role(r) for r in roles]
    return m


def ctx_of(author):
    return type("Ctx", (), {"author": author})()


c("a plain member is refused", admin_cog._is_mod(ctx_of(member())) is False)
c("an administrator is allowed",
  admin_cog._is_mod(ctx_of(member(admin=True))) is True)
c("kick_members is enough",
  admin_cog._is_mod(ctx_of(member(kick=True))) is True)
c("ban_members is enough",
  admin_cog._is_mod(ctx_of(member(ban=True))) is True)
c(f"the {config.MOD_ROLE} role is enough",
  admin_cog._is_mod(ctx_of(member(roles=(config.MOD_ROLE,)))) is True)
c("the role match ignores case",
  admin_cog._is_mod(ctx_of(member(roles=(config.MOD_ROLE.upper(),)))) is True)
# A DM has no Member object at all. Refusing there is not paranoia: it is the
# one place Discord's own permission model does not apply.
c("somebody in a DM, where there are no roles to check, is refused",
  admin_cog._is_mod(ctx_of(object())) is False)

# The workflow passes MOD_ROLE with no fallback, so an unset secret arrives as
# an empty string — and os.getenv's default does NOT apply to an empty value.
# config.MOD_ROLE was "", so this check compared role names against "" and the
# whole MOD_ROLE route was dead while looking perfectly configured.
c("an unset MOD_ROLE secret still leaves a usable role name",
  bool(config.MOD_ROLE.strip()), f"MOD_ROLE is {config.MOD_ROLE!r}")
c("and an empty role name never matches a real role anyway",
  not admin_cog._is_mod(ctx_of(member(roles=("",)))),
  "an empty allow-value must never mean 'everyone'")

print("\n— and it says so, rather than failing silently —")
deco = admin_cog.mod_only()
target = MagicMock()
target.__commands_checks__ = []
deco(target)
pred = target.__commands_checks__[0]

said = []


class Ctx:
    def __init__(self, author):
        self.author = author

    async def send(self, msg):
        said.append(msg)


allowed = asyncio.run(pred(Ctx(member(admin=True))))
c("a mod passes the check", allowed is True)
c("and is not spoken to about it", not said)

refused = asyncio.run(pred(Ctx(member())))
c("a non-mod is refused", refused is False)
c("and is TOLD, not left wondering whether the bot is broken", len(said) == 1,
  "silence here reads as a dead command, and the room reports it as a bug")

print("\n— mod commands are hidden from ordinary users in $help —")
import shared_cmds


class _B:
    loop = None
    bot = None
    def __init__(self, ops): self._ops = {o.lower() for o in ops}
    def has_prefix(self, ch, n): return n.lower() in self._ops


sc = shared_cmds.SharedCommands.get(bot=None, bridge=_B(ops=("vikram",)))
sc._channel = "#batcave"
top_user = sc.cmd_help("irc", "randomuser", "")
c("a normal user's top help shows no mod command names",
  not any(w in top_user for w in ("irckick", "ircban", "devoice", "{p}op")),
  "the owner asked that ordinary users not even get to know them")
c("and does not even point them at $help mod", "help mod" not in top_user)
c("a normal user asking $help mod is refused",
  sc.cmd_help("irc", "randomuser", "mod") == "That half is for operators.")
c("but an operator still sees the mod list",
  "op" in sc.cmd_help("irc", "vikram", "mod").lower()
  and "irckick" in sc.cmd_help("irc", "vikram", "mod"))

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

"""The canned fun commands — stateless, so this is quick but real.

    python3 test_fun.py
"""
import sys

from utils import fun
import shared_cmds

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("— the module returns something sane —")
c("8ball answers", bool(fun.eightball()))
c("dadjoke answers", bool(fun.dadjoke()))
c("fact answers", bool(fun.fact()))
c("icebreaker answers", bool(fun.icebreaker()))

print("\n— action verbs name both people —")
line = fun.action("hug", "vikram", "aishwarya")
c("a targeted action names sender and target",
  "vikram" in line and "aishwarya" in line, line)
c("no target aims at the room, not a blank",
  "the room" in fun.action("hug", "vikram", ""), fun.action("hug", "vikram", ""))
c("an unknown verb returns nothing (so it falls through)", fun.action("floop", "a", "b") == "")
c("every advertised verb resolves",
  all(fun.action(v, "a", "b") for v in fun.ACTION_VERBS),
  f"verbs: {fun.ACTION_VERBS}")

print("\n— the shared-command wrappers work on IRC and Discord —")
sc = shared_cmds.SharedCommands.get(bot=None, bridge=None)
c("$8ball needs a question", "question" in sc.cmd_8ball("irc", "x", "").lower())
c("$8ball answers when asked", "🎱" in sc.cmd_8ball("irc", "x", "will it?"))
c("$hug <nick> works", "aishwarya" in sc.cmd_hug("irc", "vikram", "aishwarya"))
c("$dadjoke works on discord too", bool(sc.cmd_dadjoke("discord", "x", "")))
# these are the ones the owner explicitly did NOT want — make sure they were
# not added by reflex.
for unwanted in ("big", "tiny", "emojify", "zalgo", "ascii", "rps", "roulette"):
    c(f"${unwanted} was NOT added", not hasattr(sc, f"cmd_{unwanted}"),
      "the owner said these are not needed")



# ── the visibility fix: fun replies go to the ROOM, not a private notice ────
print("\n— $hug and friends are seen by the room, not just the sender —")
import types
from utils.irc_bridge import IRCBridge

def _b():
    b = IRCBridge.__new__(IRCBridge)
    b.to_room, b.to_nick = [], []
    b._queue = lambda ch, m, *a: b.to_room.append((ch, m))
    b._notice = lambda n, m: b.to_nick.append((n, m))
    return b

b = _b()
b._deliver_command_reply("hug", "vikram", "#batcave", "vikram hugs nora")
c("$hug posts to the channel (everyone sees it)",
  b.to_room and b.to_room[0][0] == "#batcave" and not b.to_nick,
  f"room={b.to_room} nick={b.to_nick} — the bug was this going to nick only")

b = _b()
b._deliver_command_reply("8ball", "vikram", "#batcave", "🎱 Yes.")
c("$8ball is public too", bool(b.to_room) and not b.to_nick)

b = _b()
b._deliver_command_reply("help", "vikram", "#batcave", "help text")
c("$help stays a private notice", bool(b.to_nick) and not b.to_room,
  "a help listing is for the asker, not the room")

b = _b()
b._deliver_command_reply("hug", "vikram", "vikram", "in a PM")
c("in a PM (target is a nick, not #chan) it stays a notice",
  bool(b.to_nick) and not b.to_room)

print(f"\n{fails} FAILED" if fails else "\nALL PASS")

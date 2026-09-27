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

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

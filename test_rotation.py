"""Luna changing her name without getting killed for it.

    python3 test_rotation.py

The owner asked for both bots to rotate nicks and named the constraint himself:
"i dont wanna be banned cause of fast nick changes thats why i wanna do it."

So what is under test is not "can she change her name". It is everything that
must NOT happen when she does. The reclaim check ran every 60 seconds against
config.IRC_NICK, so ANY other name read as "we lost our nick" and triggered a
GHOST/RELEASE/NICK — it would have undone every rotation within a minute.

This calls the real methods with the socket stubbed, because the bug that got
past py_compile was `random.choice` with no `import random`: the module imports
perfectly and only fails the first time rotation actually fires. A test that
imports is not a test that runs.
"""
import sys
import types

import config
from utils.irc_bridge import IRCBridge

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def bridge(pool=("Selene", "Carmilla", "Lilith"), rotate=True, cap=2):
    """A bridge with the socket replaced by a list of what it would have sent."""
    b = IRCBridge.__new__(IRCBridge)          # no __init__: it opens sockets
    b._connected = True
    b._nick = config.IRC_NICK
    b._wanted_nick = config.IRC_NICK
    b._pending_rotation = ""
    b._rotations_at = []
    b._last_rotate = 0.0
    b._nick_times = []
    b._hosts = {}
    b.sent = []
    b._raw = lambda m: b.sent.append(m)
    b._reclaim_nick = lambda: b.sent.append("RECLAIM")
    config.IRC_NICK_ROTATE = rotate
    config.IRC_NICK_POOL = list(pool)
    config.IRC_NICK_MAX_PER_HOUR = cap
    return b


print("— it actually runs —")
b = bridge()
ok = b._rotate_nick()
# The point of this one: random.choice with no `import random` raises NameError
# here and nowhere earlier.
c("rotating does not throw, and asks the server for a name", ok and len(b.sent) == 1,
  f"sent {b.sent}")
c("the name it asked for came from the pool",
  b.sent and b.sent[0].split()[-1] in config.IRC_NICK_POOL, f"sent {b.sent}")
c("case is preserved — Selene, not selene",
  b.sent and b.sent[0].split()[-1][0].isupper(), f"sent {b.sent}")

print("\n— the cap, which is the actual safety feature —")
b = bridge(cap=2)
b._rotate_nick(); b._pending_rotation = ""
b._rotate_nick(); b._pending_rotation = ""
before = len(b.sent)
third = b._rotate_nick()
c("a third change in the same hour is refused", third is False and len(b.sent) == before,
  f"{len(b.sent)} NICKs sent")

print("\n— one at a time —")
b = bridge()
b._rotate_nick()
c("a second is refused while one is still in flight", b._rotate_nick() is False)
b._clear_pending_rotation(b._pending_rotation)
c("and allowed again once the request expires", b._pending_rotation == "")

print("\n— off unless asked for —")
b = bridge(rotate=False)
c("nothing happens when rotation is off", b._rotate_nick() is False and not b.sent)
b = bridge(pool=())
c("nothing happens with an empty pool", b._rotate_nick() is False and not b.sent,
  "an empty list must mean 'nothing to do', never 'anything goes'")

print("\n— putting it back —")
b = bridge()
b._rotate_nick()
chosen = b.sent[0].split()[-1]
b._wanted_nick = chosen
b._nick = chosen
b._pending_rotation = ""
b.sent.clear()
b._revert_nick("kicked")
c("reverting aims at the configured name again", b._wanted_nick == config.IRC_NICK)
c("and reclaims, because we are still wearing the other one", "RECLAIM" in b.sent)

# A bot that never rotates must behave exactly as it did before this existed.
b = bridge(rotate=False)
b._nick = "Guest12345"
b.sent.clear()
b._revert_nick("enforced")
c("a bot that never rotates still recovers a lost nick", "RECLAIM" in b.sent)

print("\n— knowing our own, whatever they are called —")
b = bridge()
b._hosts["dracula"] = "bot@Sat.Chit.Ananda"
b._hosts["nosferatu"] = "bot@Sat.Chit.Ananda"
b._hosts["stranger"] = "u@1.2.3.4"
b._hosts["liar"] = "u@evil-Sat.Chit.Ananda"
c("a bot on our vhost is one of ours", b.is_one_of_ours("dracula"))
c("and still is under a rotated name", b.is_one_of_ours("nosferatu"),
  "this is the whole reason rotation is safe")
c("a stranger is not", not b.is_one_of_ours("stranger"))
c("and neither is a host that merely ENDS with ours without a dot",
  not b.is_one_of_ours("liar"),
  "suffix matching without the dot lets evil-Sat.Chit.Ananda pass as ours")
c("somebody we have no host for is not assumed to be ours",
  not b.is_one_of_ours("unknown"))

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

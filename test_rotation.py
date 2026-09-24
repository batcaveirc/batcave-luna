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
    b._rotation_numbered = False
    b._isupport = {}
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

print("\n— off only when actually switched off —")
b = bridge(rotate=False)
c("nothing happens when rotation is off", b._rotate_nick() is False and not b.sent)

# CHANGED ON PURPOSE. This used to assert that an empty pool meant "nothing to
# do" — the right call while a pool was the only source of names. The owner
# rejected that whole approach: "i dont want to do this manually i want it to be
# done by the bot itself ... can add a number on back of it to avoid any
# conflicts." An empty pool is now the NORMAL case and means "build one from my
# own name", so the old assertion encoded a rule that no longer exists.
print("\n— with nothing configured at all —")
b = bridge(pool=())
ok = b._rotate_nick()
asked = b.sent[0].split()[-1] if b.sent else ""
c("an empty pool still rotates, using her own name", ok and asked,
  "this is the whole point: no pool, no NickServ GROUP, no manual step")
c("the name is her own with a number on the end",
  asked.startswith(config.IRC_NICK) and asked[len(config.IRC_NICK):].isdigit(),
  f"asked for {asked!r}")
c("and is not the bare name she is already wearing", asked != config.IRC_NICK)

print("\n— a taken name gets numbered, not abandoned —")
b = bridge(pool=("Selene",))
b._rotate_nick()
first = b.sent[0].split()[-1]
c("a pool name is tried plain first", first == "Selene", f"asked {first!r}")
retry = b._next_rotation_name(True, first)
c("and the retry is that SAME name with digits after it",
  retry.startswith("Selene") and retry[len("Selene"):].isdigit(),
  f"retry was {retry!r} — numbering a DIFFERENT pool name answers a question nobody asked")

print("\n— a long base cannot overflow the network's nick limit —")
b = bridge(pool=("A" * 40,))
made = b._next_rotation_name(True, "A" * 40)
c("a generated name fits inside IRC_NICK_MAXLEN",
  0 < len(made) <= config.IRC_NICK_MAXLEN,
  f"{len(made)} chars, limit {config.IRC_NICK_MAXLEN} — an over-long NICK is "
  "REJECTED, which reads exactly like the name being taken")

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

print("\n— asking the server instead of guessing —")
b = bridge()
b._isupport = {}
# A real ISUPPORT line from an InspIRCd network, trailing human text and all.
line = (":irc.hybridirc.com 005 Luna AWAYLEN=200 CASEMAPPING=ascii CHANNELLEN=64 "
        "KICKLEN=255 NICKLEN=18 TOPICLEN=330 MODES=20 MONITOR=30 "
        ":are supported by this server")
b._handle_line(line)
c("it reads the limits out of 005", b._isupport.get("NICKLEN") == "18",
  f"parsed: {b._isupport}")
c("and the flag-only tokens too", "MONITOR" in b._isupport, f"parsed: {b._isupport}")
c("the trailing ':are supported by this server' is not mistaken for a token",
  not any(k.startswith(":") or k in ("ARE", "SUPPORTED", "BY", "THIS", "SERVER")
          for k in b._isupport), f"parsed: {sorted(b._isupport)}")
c("the nick limit now comes from the server, not the config",
  b.nick_limit() == 18, f"got {b.nick_limit()}")

# The limit has to actually constrain the name, or reading it changes nothing.
made = b._next_rotation_name(True, "A" * 40)
c("and a generated name respects it", 0 < len(made) <= 18,
  f"{made!r} is {len(made)} chars against a server limit of 18")

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

"""Following the community's rooms, and leaving the quiet ones.

    python3 test_follow.py

The owner wanted the bots to sit where his regulars are and "join leave those
rooms if there is no activity in them", and separately: "i dont wanna be banned
cause of fast nick changes". Those two pull against each other — auto-LEAVE is
harmless, but auto-REJOIN on a clock is join/part churn, the exact abuse
signature this network kills bots for. So the design is deliberately lopsided:
Luna parts a quiet room herself, and only comes back on an INVITE or an op's
command. That asymmetry is the whole point, and it is what these tests pin.
"""
import sys
import threading
import time
import types

from utils.irc_bridge import IRCBridge, _FOLLOW_IDLE

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def bridge(follow_on=True, follow=("#hangout",), home=("#batcave",), trusted=("vikram",)):
    b = IRCBridge.__new__(IRCBridge)
    b._connected = True
    b._map_lock = threading.Lock()
    b._i2d = {h.lower(): "d" for h in home}
    b._follow_on = follow_on
    b._follow = set(follow)
    b._last_activity = {}
    b._trusted = {t.lower() for t in trusted}
    b._trusted_pending = set()
    b._hosts = {}
    b._unusable_names = set()
    b.sent = []
    b._ops = set()
    b.has_prefix = lambda ch, n: n.lower() in b._ops
    b._raw = lambda m: b.sent.append(m)
    b._notice = lambda who, m: b.sent.append(f"NOTICE {who} :{m}")
    import os
    os.environ["IRC_EXTRA_CHANNELS"] = ""
    return b


print("— a quiet followed room is left, by itself —")
b = bridge()
b._last_activity["#hangout"] = time.time() - _FOLLOW_IDLE - 60
b._follow_sweep()
c("it PARTs the room after the idle window", any(x.startswith("PART #hangout") for x in b.sent),
  f"{b.sent}")
c("and drops it from the set", "#hangout" not in b._follow)

print("\n— but an active one is kept —")
b = bridge()
b._last_activity["#hangout"] = time.time() - 30
b._follow_sweep()
c("a room that just spoke is not parted", not any("PART" in x for x in b.sent), f"{b.sent}")

print("\n— a home room is NEVER auto-parted —")
b = bridge(follow=("#batcave",), home=("#batcave",))
b._last_activity["#batcave"] = time.time() - _FOLLOW_IDLE - 9999
b._follow_sweep()
c("the bridged room stays even when silent for hours",
  not any("PART" in x for x in b.sent),
  "leaving it would take the relay down — a silent outage")

print("\n— it never rejoins on a clock —")
b = bridge(follow=())
# sweep with nothing to do, many times: it must emit no JOIN at all
for _ in range(5):
    b._follow_sweep()
c("the sweep only ever parts, never joins", not any("JOIN" in x for x in b.sent),
  f"a clock-driven JOIN is the churn that gets a bot killed: {b.sent}")

print("\n— coming back is event-driven —")
b = bridge(follow=())
line = ":vikram!u@host INVITE Luna :#hangout"
b._handle_line(line) if hasattr(b, "_handle_line") else None
# _handle_line does far more than we stubbed; test the invite path via its guard
b2 = bridge(follow=())
b2.is_trusted = lambda n: n.lower() == "vikram"
b2.is_one_of_ours = lambda n: False
b2.follow_add("#hangout")
c("an invited room is joined", any(x.startswith("JOIN #hangout") for x in b2.sent), f"{b2.sent}")
c("and added to the set", "#hangout" in b2._follow)
c("with a grace stamp so the very next sweep does not part it",
  "#hangout" in b2._last_activity)

print("\n— op-gated, and only acts on rooms you name —")
b = bridge(follow=())
b.is_trusted = lambda n: False
b.is_one_of_ours = lambda n: False
handled = b.try_follow_command("#batcave", "randomguy", "$follow #anything")
c("a non-op is refused (silently, handled)", handled and not any("JOIN" in x for x in b.sent),
  f"{b.sent}")
b = bridge(follow=())
b._ops = {"vikram"}
b.try_follow_command("#batcave", "vikram", "$follow #hangout")
c("an op can add a room", "#hangout" in b._follow and any("JOIN #hangout" in x for x in b.sent))
b.try_follow_command("#batcave", "vikram", "$unfollow #hangout")
c("and remove it", "#hangout" not in b._follow and any("PART #hangout" in x for x in b.sent))

print("\n— off by default means nothing happens —")
b = bridge(follow_on=False, follow=("#hangout",))
b._last_activity["#hangout"] = time.time() - _FOLLOW_IDLE - 999
b._follow_sweep()
c("with IRC_FOLLOW off, no room is parted", not any("PART" in x for x in b.sent),
  "the feature must never surprise the network until it is switched on")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

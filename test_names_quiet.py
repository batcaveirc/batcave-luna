"""Luna must not load member lists by herself (owner, 2026-10-18).

Live: at every connect she sent NAMES for five rooms on top of the list the
server already sends on JOIN, and her own flood shield dropped all five. The
server's list is enough; extra asks are rate-limited to one per room per 2 min.
"""
import os
import sys
import threading

import config
from utils.irc_bridge import IRCBridge

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok or not detail else ' — ' + detail}")


def bridge():
    b = IRCBridge.__new__(IRCBridge)          # no __init__: it opens sockets
    b._nick = "Astra48"
    b._connected = True
    b._map_lock = threading.Lock()
    b._nicks_lock = threading.Lock()
    b._i2d = {"#batcave": "batcave"}
    b._d2i = {"batcave": "#batcave"}
    b._shadow = {"#desilivechat.com"}
    b._follow = set()
    b._nicks = {"#batcave": {"Megha", "Astra48"}, "#desilivechat.com": {"Megha"}}
    b._prefixes = {}
    b._hosts = {}
    b._locked_out = {}
    b.sent = []
    b._raw = lambda m: b.sent.append(m)
    b.logged = []
    b._joinlog = lambda text: b.logged.append(text)
    b.moderator = type("M", (), {"check_join": lambda *a: None})()
    return b


print("— her own JOIN —")
b = bridge()
b._handle_line(f":{config.IRC_NICK}!u@h JOIN #batcave * :Luna")
c("no NAMES sent on her own join", not any(x.startswith("NAMES") for x in b.sent), str(b.sent))

print("\n— a cache miss for every stranger who speaks —")
b = bridge()
for n in ("a1", "b2", "c3", "d4", "e5"):
    b.has_prefix("#batcave", n)
names = [x for x in b.sent if x.startswith("NAMES")]
c("five misses ask the server once, not five times", len(names) == 1, str(names))
b.request_names("#batcave")
c("and an explicit request inside the gap is folded in too",
  len([x for x in b.sent if x.startswith("NAMES")]) == 1, str(b.sent))

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
raise SystemExit(1 if fails else 0)

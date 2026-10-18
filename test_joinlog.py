"""Discord join/leave log (owner request 2026-10-18).

Real IRC lines go through the real line handler; the test captures what would
be posted to Discord. Fails if a home-room join, part, quit, kick or rename is
silently dropped, if the host is missing, or if a room she only shadows leaks
into the log by default.
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


def feed(b, line):
    try:
        b._handle_line(line)
    except Exception as e:                    # noqa: BLE001
        # Other handlers may need state this stub lacks; what matters is
        # whether the join log saw the event before that point.
        b.logged.append(f"(handler raised {type(e).__name__}: {e})")


os.environ.pop("LUNA_JOINLOG", None)
os.environ.pop("LUNA_JOINLOG_ALL", None)

print("— home-room membership reaches Discord, with the host —")
b = bridge()
feed(b, ":Ayaan8201!webchat@lu5.qq5.149.45.IP JOIN #batcave Ayaan :Kiwi IRC User")
c("a join is logged", any("Ayaan8201" in x and "joined #batcave" in x for x in b.logged), str(b.logged))
c("with the user@host", any("webchat@lu5.qq5.149.45.IP" in x for x in b.logged), str(b.logged))
c("and the account it is logged in as", any("account `Ayaan`" in x for x in b.logged), str(b.logged))

b = bridge()
feed(b, ":Guest77!webchat@ab.cd.IP JOIN #batcave * :Kiwi IRC User")
c("an unregistered join says so", any("unregistered" in x for x in b.logged), str(b.logged))

b = bridge()
feed(b, ":Megha!webchat@x.y.IP PART #batcave :bye all")
c("a part is logged with its reason", any("left #batcave" in x and "bye all" in x for x in b.logged), str(b.logged))

b = bridge()
feed(b, ":Megha!webchat@x.y.IP QUIT :Quit: Client closed")
c("a quit is logged for someone who was in a home room",
  any("quit" in x and "Megha" in x and "#batcave" in x for x in b.logged), str(b.logged))
c("and does not name the shadow room she was also in",
  not any("desilivechat" in x for x in b.logged), str(b.logged))

b = bridge()
feed(b, ":Noctis87!d@Keeping.The.Night.Company KICK #batcave Megha :abuse")
c("a kick is logged with who and why",
  any("Megha" in x and "kicked" in x and "Noctis87" in x and "abuse" in x for x in b.logged), str(b.logged))

b = bridge()
feed(b, ":Megha!webchat@x.y.IP NICK :Megha_")
c("a rename is logged", any("Megha" in x and "Megha_" in x for x in b.logged), str(b.logged))

print("\n— what must NOT be logged —")
b = bridge()
feed(b, ":Someone!u@h.IP JOIN #desilivechat.com * :x")
c("a shadow room is not logged by default", not any("joined" in x for x in b.logged), str(b.logged))
os.environ["LUNA_JOINLOG_ALL"] = "on"
b = bridge()
feed(b, ":Someone!u@h.IP JOIN #desilivechat.com * :x")
c("unless LUNA_JOINLOG_ALL=on", any("joined #desilivechat.com" in x for x in b.logged), str(b.logged))
os.environ.pop("LUNA_JOINLOG_ALL", None)

b = bridge()
feed(b, ":Astra48!luna@Sat.Chit.Ananda JOIN #batcave Andromeda :Luna")
c("her own join is not logged", not any("joined" in x for x in b.logged), str(b.logged))

os.environ["LUNA_JOINLOG"] = "off"
b = bridge()
feed(b, ":Ayaan!w@h.IP JOIN #batcave * :x")
c("LUNA_JOINLOG=off turns it off", not any("joined" in x for x in b.logged), str(b.logged))
os.environ.pop("LUNA_JOINLOG", None)

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

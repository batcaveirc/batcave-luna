"""The server said the bridge flooded it. What happens next must be gentler.

HybridIRC kills with "RecvQ exceeded" — its buffer of what we have already SENT.
The shield in _raw caps the rate; this pins the second half, which is the part
that turned one kill into a loop: recognising the reason and waiting, instead of
reconnecting straight into the same queue with the same burst.
"""
import sys

import config  # noqa: F401 — imported the same way the bridge does
from utils.irc_bridge import IRCBridge

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok or not detail else ' — ' + detail}")


def bridge():
    # No __init__: that opens sockets. The method under test only reads and
    # writes attributes, which is exactly why it was made a method.
    return IRCBridge.__new__(IRCBridge)


print("— an ordinary error is not a flood complaint —")
b = bridge()
c("a ping timeout earns no cooldown",
  b._note_server_error("ERROR :Closing Link (Ping timeout)") == 0)
c("and counts nothing", getattr(b, "_flood_kills", 0) == 0)

print("\n— a flood kill is recognised and backed off —")
b = bridge()
first = b._note_server_error("ERROR :Closing Link: (RecvQ exceeded)")
c("RecvQ is recognised", first > 0, repr(first))
c("the first wait is seconds, not a reconnect storm", first >= 10, repr(first))
c("and the cooldown is recorded for the reconnect loop", b._flood_cooldown == first)
second = b._note_server_error("ERROR :Closing Link: (Excess Flood)")
c("a second kill waits longer", second > first, f"{first} → {second}")
c("the count is kept", b._flood_kills == 2, str(b._flood_kills))
c("the wait is capped so the bot still comes back",
  b._note_server_error("recvq") <= 300)

print("\n— the shield sits under the network's ceiling, not on it —")
c("the cap is below the ~10/s the network tolerates",
  IRCBridge._FLOOD_LIMIT < 10, str(IRCBridge._FLOOD_LIMIT))
c("and not so low that ordinary traffic is lost",
  IRCBridge._FLOOD_LIMIT >= 4, str(IRCBridge._FLOOD_LIMIT))
c("protocol keepalives still bypass it", "PING" in IRCBridge._FLOOD_EXEMPT)

print("\n— the wiring, which is where this has to live —")
src = open("utils/irc_bridge.py", encoding="utf-8").read()
c("the ERROR path notices the complaint", "_note_server_error(line)" in src)
c("the reconnect waits for that cooldown",
  "_flood_cooldown" in src and "max(delay, cooldown)" in src)
c("a long clean session forgives the escalation",
  "> 600:" in src and "_flood_kills = 0" in src)
handler = src[src.index("def _note_server_error"):src.index("def _run_forever")]
c("a flood kill never exits the process", "os._exit" not in handler,
  "a kill is not a reason to quit — exiting is how the room loses its bot")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

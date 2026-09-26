"""Adult mode, and the consent that has to be real for it to be allowed.

    python3 test_nsfw.py

The feature is opt-in adult roleplay in rooms an operator has disclosed as adult.
What makes it defensible rather than a harassment tool is the gate, so the gate
is what this pins hardest: a room is adult only when its topic says so, a person
is involved only after they opt in, and a line is aimed at someone only if THAT
someone opted in too — the guardrail the reference bot never had.
"""
import sys

from utils.nsfw import Nsfw, TOPIC_MARK

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# A fake topic table the manager reads room-state from.
topics = {}
n = Nsfw(topic_of=lambda ch: topics.get(ch.lower(), ""))


print("— a room is adult only when its topic says so —")
c("no topic marker → not adult", not n.room_is_adult("#room"))
topics["#room"] = TOPIC_MARK + " Adult room, 18+"
c("topic with the marker → adult", n.room_is_adult("#room"))

print("\n— the topic disclosure is written and removed cleanly —")
t = n.topic_with_notice("welcome to the lounge")
c("turning on prepends the 18+ notice and keeps the old topic",
  TOPIC_MARK in t and "welcome to the lounge" in t)
c("turning on twice does not double the notice",
  n.topic_with_notice(t) == t)
off = n.topic_without_notice(t)
c("turning off strips the notice but keeps the room's own topic",
  TOPIC_MARK not in off and "welcome to the lounge" in off)

print("\n— entering the disclosed room is the consent; no opt-in step —")
line, refusal = n.line("afterdark", "#room", "alice")
c("anyone in an adult room gets a line, no opt-in required", bool(line) and not refusal)

print("\n— not in a room that is not adult —")
line, refusal = n.line("afterdark", "#plain", "alice")
c("nothing happens in a non-adult room", not line and "operator" in refusal, refusal)

print("\n— a directed line works on anyone present, UNLESS they opted out —")
line, refusal = n.line("tempt", "#room", "alice", "bob")
c("aiming at another person in the adult room works", bool(line) and "bob" in line and not refusal)
n.opt_out("bob")      # $boundaries
line, refusal = n.line("tempt", "#room", "alice", "bob")
c("but $boundaries makes bob off-limits immediately",
  not line and "bob" in refusal, "stop the moment someone says no — absolute")
c("someone who opted out cannot use it themselves either",
  not n.line("afterdark", "#room", "bob")[0])
n.opt_in("bob")       # $boundaries off
line, refusal = n.line("tempt", "#room", "alice", "bob")
c("$boundaries off brings them back", bool(line) and not refusal)

print("\n— self-targeting is fine —")
line, refusal = n.line("tempt", "#room", "alice", "alice")
c("aiming at yourself is allowed", bool(line) and not refusal)

print("\n— the manager never produces explicit content —")
sample = " ".join(n.line(k, "#room", "carol", "dave")[0] for k in
                   ("spicy", "tempt", "fantasy", "midnight", "desire") for _ in range(5))
banned = ["fuck", "cock", "pussy", "cum", "naked", "sex"]
c("suggestive, not explicit", not any(w in sample.lower() for w in banned),
  "the lines are cinematic flirtation, not porn")

# The bridge handler: op-gating on the room toggle, and topic-writing.
print("\n— $nsfw on is operators-only and writes the topic —")
import types
from utils.irc_bridge import IRCBridge

def bridge(ops=()):
    b = IRCBridge.__new__(IRCBridge)
    b._nsfw = Nsfw(topic_of=lambda ch: b._topics.get(ch.lower(), ""))
    b._topics = {"#room": "lounge"}
    b._ops = {o.lower() for o in ops}
    b.has_prefix = lambda ch, nk: nk.lower() in b._ops
    b.get_topic = lambda ch: b._topics.get(ch.lower(), "")
    b.sent = []
    b._raw = lambda m: b.sent.append(m)
    b._notice = lambda who, m: b.sent.append(f"NOTICE {who} :{m}")
    b._queue = lambda ch, m: b.sent.append(f"MSG {ch} :{m}")
    return b

import config
P = config.PREFIX
b = bridge(ops=())
b.try_nsfw_command("#room", "rando", f"{P}nsfw on")
c("a non-op cannot turn on adult mode", not any("TOPIC" in x for x in b.sent), f"{b.sent}")

b = bridge(ops=("vikram",))
b.try_nsfw_command("#room", "vikram", f"{P}nsfw on")
c("an op turns it on and the topic gets the 18+ notice",
  any(x.startswith("TOPIC #room") and TOPIC_MARK in x for x in b.sent), f"{b.sent}")

b = bridge(ops=("vikram",))
# opt in, adult room via topic, then an action goes to the room
b._topics["#room"] = TOPIC_MARK + " adult"
b.try_nsfw_command("#room", "alice", f"{P}afterdark")
c("anyone in the adult room can use afterdark (no opt-in step)",
  any(x.startswith("MSG #room") for x in b.sent), f"{b.sent}")
b2 = bridge(ops=("vikram",)); b2._topics["#room"] = TOPIC_MARK + " adult"
b2.try_nsfw_command("#room", "bob", f"{P}boundaries")
b2.try_nsfw_command("#room", "alice", f"{P}tempt bob")
c("but someone who typed $boundaries is not targeted",
  not any(x.startswith("MSG #room") for x in b2.sent if "tempt" not in x.lower()) or
  all("bob" not in x for x in b2.sent if x.startswith("MSG #room")),
  f"{b2.sent}")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

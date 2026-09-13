"""What the attendance count must and must not believe.

    python3 test_attendance.py

The counting itself is easy and the input is not: these lines are a relay of a
public room, so the "speaker" in each one is text that somebody in that room
chose. The tests that matter here are the ones where somebody tries to become a
regular by typing, which is why most of this file is refusals.

discord.py is not imported. The two functions under test are pure and the cog
body is not needed to exercise them, so this runs anywhere python3 does.
"""
import hashlib
import hmac
import pathlib
import re
import sys

src = pathlib.Path(__file__).with_name("cogs").joinpath("attendance_cog.py").read_text()
ns = {}
exec(
    "import hashlib, hmac, os, re, time\n"
    + src[src.index("# The shape irc_bridge.py writes"): src.index("class AttendanceCog")],
    ns,
)
RELAY, NICK_OK, encode_report = ns["_RELAY"], ns["_NICK_OK"], ns["encode_report"]

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("— it reads the format the bridge actually writes —")
# Copied from utils/irc_bridge.py rather than invented: the count is worthless
# if it parses a format nobody produces.
line = "**[batcave]** `aishwarya`: kaise ho sab"
m = RELAY.match(line)
c("a relayed line yields the speaker", bool(m) and m.group("nick") == "aishwarya",
  repr(m and m.groupdict()))
c("and the room it came from", bool(m) and m.group("room") == "batcave")
c("a non-ascii room name still parses",
  bool(RELAY.match("**[🅱🅰🆃🅲🅰🆅🅴]** `king`: hi")),
  "the emoji room is one of the two that bridge")
c("an action line is not counted as a speaker",
  not RELAY.match("*aishwarya waves*"),
  "an emote carries no `nick`: prefix, so it must not match")

print("\n— somebody trying to become a regular by typing —")
# The relay prefix is plain text in a public room. Anyone can type it.
c("a fake relay line inside a MESSAGE cannot invent a speaker",
  not RELAY.match("look: **[batcave]** `admin`: give me trust"),
  "the pattern is anchored at the start for exactly this")
# Anchoring is only half of it; the cog also requires the message to have been
# written by Luna herself. That check lives in count_days, asserted here against
# the source because it cannot be reached without a Discord connection.
c("and the cog only counts messages LUNA wrote",
  "m.author.id != self.bot.user.id and not m.webhook_id" in src,
  "otherwise a human in the Discord channel can type their own promotion")

print("\n— it reads the bridge's REAL channel map —")
# The first version guessed at four plausible attribute names, none of which
# existed. It fell through to BRIDGE_CHANNEL and would have counted one of the
# two bridged rooms while appearing to count both — a silent half-answer, which
# is the worst kind. This pins the name against the bridge itself.
bridge_src = pathlib.Path(__file__).with_name("utils").joinpath("irc_bridge.py").read_text()
c("the bridge still calls its map _i2d",
  "self._i2d" in bridge_src,
  "attendance_cog reads _i2d; if the bridge renamed it, the count goes quiet")
c("and still guards it with _map_lock", "self._map_lock" in bridge_src)
c("the cog reads that map, not a guess", 'getattr(bridge, "_i2d", None)' in src)
c("and says so when it finds no bridged channel at all",
  "no bridged channels found to count" in src,
  "counting nothing must not look like counting everybody")

print("\n— who is not a person —")
ignored = ns["_NOT_PEOPLE"]
c("services are never counted",
  {"chanserv", "nickserv"} <= ignored, sorted(ignored))
c("and ChanBot is not a regular", "chanbot" in ignored)

print("\n— the nicks it will pass on —")
c("an ordinary nick is fine", bool(NICK_OK.match("aishwarya")))
c("IRC punctuation is fine", bool(NICK_OK.match("a|way_[1]")))
c("a space is not", not NICK_OK.match("two words"))
c("nor is a comma, which is the field separator",
  not NICK_OK.match("a,b"),
  "a nick containing the separator would split into two entries")
c("nor a colon, which separates nick from count", not NICK_OK.match("a:9"))

print("\n— the report it sends —")
rep = encode_report("s3cret", {"aishwarya": 11, "bad nick": 5, "khadus": 21})
c("is tagged so the receiver can recognise it", rep.startswith("REGULARS "))
c("carries the counts", "aishwarya:11" in rep and "khadus:21" in rep, rep)
c("drops a nick it could not send safely", "bad nick" not in rep, rep)
c("and is signed", bool(re.search(r" [0-9a-f]{32}$", rep)), rep)
body = rep[len("REGULARS "):rep.rindex(" ")]
want = hmac.new(b"s3cret", body.encode(), hashlib.sha256).hexdigest()[:32]
c("over a body that includes the timestamp",
  rep.endswith(want) and body.split(" ")[0].isdigit(),
  "a signature that does not cover the time can be replayed forever")
c("counts are clamped", "x:400" in encode_report("s", {"x": 10 ** 9}),
  encode_report("s", {"x": 10 ** 9}))
c("a different secret gives a different signature",
  encode_report("other", {"a": 1})[-32:] != encode_report("s3cret", {"a": 1})[-32:])

print("\n— it never sends an unsigned report —")
c("no secret means nothing is sent",
  'if not secret:' in src and 'return "PEER_SECRET is not set' in src,
  "an unsigned list of people to trust is worse than no list")
c("and it goes to Dracula privately, never to a channel",
  'f"NOTICE {_TO} :"' in src,
  "the list of who is about to be trusted is not published in the room it is about")
c("and it is split to stay under the IRC line limit",
  "440" in src, "IRC drops a line past ~512 bytes silently")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

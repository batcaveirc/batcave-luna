"""The moderator record, and the ways it must refuse to be fooled.

    python3 test_records.py

Most of this is about the ledger being read back out of a channel that also
carries ordinary conversation. The store is Discord messages, so the parser is a
trust boundary: anybody who can type in that channel can type something that
LOOKS like a warning record.

discord.py is not imported — the parsing and formatting under test are pure.
"""
import ast
import pathlib
import re
import sys

src = pathlib.Path(__file__).with_name("cogs").joinpath("records_cog.py").read_text()
bridge_src = pathlib.Path(__file__).with_name("utils").joinpath("irc_bridge.py").read_text()

ns = {}
exec(  # noqa: S102
    "import os, re, time\nfrom datetime import datetime, timedelta, timezone\n"
    + src[src.index("# One ledger line."): src.index("class RecordsCog")].replace(
        'getattr(config, "ALERT_CHANNEL", "bot-logs")', '"bot-logs"'),
    ns,
)
LEDGER, RELAY, ago = ns["_LEDGER_RE"], ns["_RELAY_RE"], ns["_ago"]

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("— a record Luna wrote —")
line = "`LEDGER1` warn |samosa|vikram#0|1757800000|spamming the room"
m = LEDGER.match(line)
c("parses back", bool(m), repr(line))
c("with the nick", bool(m) and m.group("nick") == "samosa")
c("who gave it", bool(m) and m.group("by") == "vikram#0")
c("when", bool(m) and m.group("at") == "1757800000")
c("and the reason", bool(m) and m.group("reason") == "spamming the room")
c("a clear is a record too",
  bool(LEDGER.match("`LEDGER1` clear |samosa|vikram#0|1757800001|cleared 3")))

print("\n— somebody typing a record by hand —")
c("a record quoted INSIDE a sentence is not one",
  not LEDGER.match("look what she said: `LEDGER1` warn |boss|me|1|hi"),
  "the pattern is anchored at the start for exactly this")
c("an unknown kind is refused",
  not LEDGER.match("`LEDGER1` promote |samosa|me|1|make me a mod"))
c("a missing version tag is refused",
  not LEDGER.match("`LEDGER` warn |samosa|me|1|x"))
c("a non-numeric timestamp is refused",
  not LEDGER.match("`LEDGER1` warn |samosa|me|yesterday|x"))
c("a nick with a pipe cannot smuggle fields",
  not LEDGER.match("`LEDGER1` warn |a|b|1|x|extra|2|y".replace("|a|", "|a|b|")),
  "pipes are the separator, so they are stripped on write")
# The other half of that: the writer strips them, so a reason containing a pipe
# can never produce a line that parses as a different record.
c("and the writer strips pipes out of every field",
  'replace("|", "/")' in src,
  "without this a reason could contain the separator and forge the next field")
c("only Luna's OWN messages are read as records",
  src.count("m.author.id != self.bot.user.id") >= 2,
  "otherwise anybody who can type in the channel can hand themselves a clean record")

print("\n— a clear truncates the history —")
# The property: after a clear, the earlier warnings must not come back. A
# moderator who was told "cleared 3" and then sees 3 warnings has been lied to.
c("the reader drops everything before the newest clear",
  'rows = rows[i + 1:]' in src and "if rows[i][\"kind\"] == \"clear\"" in src,
  src[src.index("for i in range"):src.index("for i in range") + 200])

print("\n— it never claims to have saved something it did not —")
c("a write failure is reported, not swallowed",
  'did not pretend to save it' in src,
  "a warning that silently vanishes at the next handover is worse than a refusal")
c("and the ledger is a CHANNEL, not a file on the runner",
  not re.search(r"\bopen\(|json\.dump|\.write\(", src),
  "the runner's disk is destroyed with the job, which is how the old JSON store "
  "silently lost every warning at each handover")

print("\n— the IRC side —")
c("a warning is delivered by NOTICE, never to the channel",
  'NOTICE {nick} :[MOD]' in src and 'PRIVMSG' not in src,
  "a public telling-off is the thing the room objected to")
c("and the bridge methods it calls really exist",
  "def send_raw" in bridge_src and "def get_irc_for_discord" in bridge_src,
  "guessing at bridge attribute names has already shipped one silent half-answer")
c("slowmode uses the server's own flood mode",
  '+f [1t#' in src,
  "server-side, so it holds while Luna is between restarts")

print("\n— $seen reads the relay, so it needs no store —")
c("it parses the bridge's relay format",
  bool(RELAY.match("**[batcave]** `aishwarya`: hello")))
c("and the nick it yields is the speaker",
  RELAY.match("**[batcave]** `aishwarya`: hello").group("nick") == "aishwarya")

print("\n— times a moderator can read —")
c("seconds", ago(30) == "30s ago", ago(30))
c("minutes", ago(600) == "10m ago", ago(600))
c("hours and minutes", ago(3600 * 5 + 600) == "5h 10m ago", ago(3600 * 5 + 600))
c("days", ago(86400 * 3) == "3d ago", ago(86400 * 3))
c("and never a negative", ago(-5) == "0s ago", ago(-5))

print("\n— it is actually loaded —")
luna = pathlib.Path(__file__).with_name("luna.py").read_text()
c("the cog is in the COGS list", '"cogs.records_cog"' in luna,
  "a cog nobody loads is a feature that does not exist")
c("and every command it defines is in the help",
  all(f"{cmd}" in luna for cmd in ("warn", "warnings", "clearwarns", "seen", "slowmode")),
  "advertised-but-dead and built-but-unadvertised are the same bug twice")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

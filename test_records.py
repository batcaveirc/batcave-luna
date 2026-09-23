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
c("slowmode never SENDS a flood mode this server would reject",
  not re.search(r"send_raw\([^)]*\+f", src),
  "'+f [1t#n]' is UnrealIRCd syntax; InspIRCd rejects it silently, and its own +f kicks")
c("it enforces by DEVOICING, not kicking",
  'MODE {room} -v {nick}' in src and 'KICK' not in src,
  "a kick is the thing the room objected to")
c("and it acts once, not in a mode war",
  "self._told" in src and "< 300" in src)
c("services and our own bots are never rate-limited",
  '"chanserv"' in src and '"dracula"' in src,
  "Luna devoicing Dracula is two bots fighting for a human to break up")
c("the rate watcher reads the RELAY, so it needs no socket of its own",
  'on_message' in src and '_RELAY_RE.match(message.content' in src)

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

print("\n— refused at the door, not retried forever —")
# Measured 2026-09-13: one run made 154 connection attempts in five hours, 121 of
# them ending in "TLS/SSL connection has been closed (EOF)". Luna was absent from
# the room the whole time and the job looked perfectly healthy. Hammering a server
# that is dropping the handshake cannot fix an address block, and that pattern has
# already cost this project a GitHub account once.
ns2 = {}
_tree = ast.parse(bridge_src)
for _node in _tree.body:
    _want = (
        isinstance(_node, ast.FunctionDef) and _node.name == "_refused_at_the_door"
    ) or (
        isinstance(_node, ast.Assign)
        and any(getattr(t, "id", "").startswith("_REFUS") for t in _node.targets)
    )
    if _want:
        exec(ast.get_source_segment(bridge_src, _node), ns2)  # noqa: S102
refused = ns2["_refused_at_the_door"]
c("an immediate TLS EOF is recognised as a refusal",
  refused(Exception("TLS/SSL connection has been closed (EOF) (_ssl.c:1010)")),
  "this is the exact string the live log carried 121 times")
c("so is a refused connection", refused(ConnectionRefusedError("Connection refused")))
c("and a timeout", refused(TimeoutError("timed out")))
c("and an explicit line ban", refused(Exception("Closing link: Z-lined")))
c("but an ordinary mid-session drop is NOT",
  not refused(Exception("Ping timeout")) and not refused(Exception("broken pipe")),
  "a session that was working and dropped must keep its fast retry")
c("the long wait is at least ten minutes",
  ns2["_REFUSED_DELAY"] >= 600, str(ns2["_REFUSED_DELAY"]))
c("and a couple of flukes do not trigger it",
  ns2["_REFUSALS_BEFORE_SLOWDOWN"] >= 2, str(ns2["_REFUSALS_BEFORE_SLOWDOWN"]))
c("it only applies BEFORE we ever registered",
  "not self._ever_registered and _refused_at_the_door" in bridge_src,
  "a drop after a working session is a different fault with a different fix")
c("registering clears it", "the address is fine" in bridge_src)
c("and it says so out loud, once",
  "refusing this ADDRESS" in bridge_src,
  "five hours of silent failure is how this went unnoticed")


print("\n— reclaiming our own nick —")
# The owner: "luna1 is getting disconnected again and again changing to guest id".
# Guest#### is NickServ enforcement, and two faults in this sequence lead there.
# At the six-hourly handover the outgoing runner still holds Luna1, so the
# incoming one takes 433 and falls back to Luna1_.
c("IDENTIFY names the ACCOUNT, not just the password",
  "IDENTIFY {config.IRC_NICKSERV_ACCOUNT} {config.IRC_NICKSERV_PASS}" in bridge_src,
  "the one-argument form identifies the nick you are WEARING — and Luna1_ is not "
  "a registered account, so it identifies nothing and enforcement takes over")
c("and nothing still uses the one-argument form",
  "IDENTIFY {config.IRC_NICKSERV_PASS}" not in bridge_src,
  "one missed call site is one path back to Guest####")
c("the account defaults to the nick when unset",
  'IRC_NICKSERV_ACCOUNT = os.getenv("IRC_NICKSERV_ACCOUNT", "") or IRC_NICK'
  in pathlib.Path(__file__).with_name("config.py").read_text(),
  "no new secret required for the normal case")

# GHOST ends the stale session; it does not clear the hold NickServ then places on
# the nick, and a NICK into that hold is refused — leaving us on Luna1_,
# unidentified, which is what gets renamed.
_seq = [bridge_src.index(x) for x in ("NickServ :IDENTIFY", "NickServ :GHOST",
                                      "NickServ :RELEASE", 'NICK {config.IRC_NICK}')]
c("the reclaim runs IDENTIFY -> GHOST -> RELEASE -> NICK",
  _seq == sorted(_seq), f"order found at {_seq}")
c("both reclaim paths RELEASE before taking the nick",
  bridge_src.count("NickServ :RELEASE") >= 2,
  f"{bridge_src.count('NickServ :RELEASE')} RELEASE call(s) — the registration path "
  "had GHOST then NICK with no RELEASE between them")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

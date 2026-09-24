"""$find, $tell and $stats — and the trust boundary $tell creates.

    python3 test_memory.py

$tell is the one worth attacking. A message left for somebody is delivered later,
privately, in Luna's voice — so it must not become a way to put words in her mouth,
to harass somebody who cannot see who sent it, or to fill the room with memos.
"""
import ast
import pathlib
import re
import sys

here = pathlib.Path(__file__).parent
src = (here / "cogs" / "memory_cog.py").read_text()
rec = (here / "cogs" / "records_cog.py").read_text()
luna = (here / "luna.py").read_text()

ns = {}
exec(  # noqa: S102
    "import re\n" + rec[rec.index("# One ledger line."): rec.index("_LEDGER_CHANNEL")],
    ns,
)
LEDGER, RELAY = ns["_LEDGER_RE"], ns["_RELAY_RE"]

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("— the ledger carries messages now —")
c("a tell parses", bool(LEDGER.match("`LEDGER1` tell |samosa|vikram#0|1758000000|call me")))
c("and its delivery receipt", bool(LEDGER.match("`LEDGER1` told |samosa|vikram#0|1758000000|delivered")))
c("the older kinds still parse",
  bool(LEDGER.match("`LEDGER1` warn |samosa|v#0|1|spam"))
  and bool(LEDGER.match("`LEDGER1` clear |samosa|v#0|1|cleared 2")))
c("an invented kind does not",
  not LEDGER.match("`LEDGER1` promote |samosa|v#0|1|make me a mod"))

print("\n— a message left for somebody —")
c("carries who left it, so nobody hides behind Luna",
  "{r['by'].split('#')[0]} " in src and "left you this" in src,
  "delivered in her voice, so it must say whose words they are")
c("is delivered privately, not to the room",
  "NOTICE {who} :" in src and "PRIVMSG #" not in src)
c("is capped per sender, so it cannot become a flood",
  "_MAX_PENDING" in src and "already have" in src)
c("is refused for bots and services",
  "_NEVER_TOUCH" in src and "will not read its messages" in src)
c("is refused for a nick that cannot exist",
  "_NICK_OK.match(nick)" in src)
c("and says so rather than pretending, when there is nowhere to keep it",
  "quietly disappears" in src,
  "a message that vanishes at the next handover is worse than a refusal")

print("\n— and it is delivered once —")
c("a receipt is written when it is handed over",
  "`LEDGER1` told |" in src,
  "without one, every restart re-delivers everything ever left")
c("and the reader skips anything already receipted",
  'done.add(key)' in src and 'r["key"] not in done' in src)
c("with an in-process guard for the same run",
  "self._delivered" in src)

print("\n— $find —")
c("it reads the relay, which is the only scrollback this room has",
  "_RELAY_RE.match" in src and "ch.history" in src)
c("only Luna's own lines count as the room speaking",
  "m.author.id != self.bot.user.id" in src,
  "otherwise somebody types the relay format and invents a quote")
c("a one-letter search is refused", "at least three characters" in src)
c("and the output is bounded", "hits[:10]" in src and "len(hits) >= 60" in src)

print("\n— $stats —")
c("it counts people, hours and rooms", all(w in src for w in ("talkers", "hours", "rooms")))
c("and leaves the bots out of the tally", "_NEVER_TOUCH" in src)

print("\n— it is actually loaded and advertised —")
c("the cog is in the COGS list", '"cogs.memory_cog"' in luna,
  "a cog nobody loads is a feature that does not exist")
for cmd in ("find", "tell", "stats"):
    c(f"${cmd} is in the help", f"{{p}}{cmd}" in luna)

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

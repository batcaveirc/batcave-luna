"""Clapback — answering an attack on one of ours, in proportion.

The thing this file guards against is over-reaction. A bot that kicks over
"shut up" is a bot nobody can joke around, and the regulars it defends are the
first to notice. So the tiers matter more than the lines: mild is a reply and
nothing else, abuse goes through the ladder, and only a threat is removed.

    python3 test_clapback.py
"""
from __future__ import annotations

import sys

from utils.clapback import Clapback, tier_of

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


OURS = {"hazel", "vikram"}
is_protected = lambda n: (n or "").lower() in OURS            # noqa: E731

print("— severity is read from what was said —")
c("a mild insult is tier 1", tier_of("shut up hazel") == 1, str(tier_of("shut up hazel")))
c("a plain insult is tier 1", tier_of("hazel is an idiot") == 1)
c("abuse is tier 2", tier_of("hazel you bitch") == 2)
c("a threat is tier 3", tier_of("i will kill hazel") == 3)
c("an ordinary line is tier 0", tier_of("good morning hazel, how are you") == 0)
c("threat beats abuse when both appear", tier_of("shut up you bitch or i will kill you") == 3)

print("\n— it only answers attacks on a PROTECTED person —")
cb = Clapback(rng=lambda: 0.0, clock=lambda: 1000.0)
hit = cb.check("hazel is an idiot", "troll1", is_protected=is_protected)
c("an attack naming a regular is answered", bool(hit), repr(hit))
c("and names who was attacked", hit and hit["who"] == "hazel", hit and hit["who"])
c("with a reason the ladder can use", hit and hit["reason"] == "attacking a regular")
c("the reply does not repeat the insult", hit and "idiot" not in hit["line"].lower())

c("an insult aimed at nobody is left to the ladder",
  cb.check("you are all idiots", "troll2", is_protected=is_protected) is None)
c("one of ours is never answered",
  cb.check("hazel is an idiot", "vikram", is_protected=is_protected) is None,
  "regulars rib each other constantly")

print("\n— the severity decides the consequence —")
mk = lambda: Clapback(rng=lambda: 0.0, clock=lambda: 5000.0)   # noqa: E731
c("mild asks for nothing", mk().check("shut up hazel", "t1", is_protected=is_protected)["tier"] == 1)
c("abuse asks for the ladder", mk().check("hazel you bitch", "t2", is_protected=is_protected)["tier"] == 2)
c("a threat asks for removal", mk().check("i will kill hazel", "t3", is_protected=is_protected)["tier"] == 3)

print("\n— the adult register is available only where it was switched on —")
clean = mk().check("hazel is an idiot", "t4", is_protected=is_protected, nsfw=False)
adult = mk().check("hazel is an idiot", "t5", is_protected=is_protected, nsfw=True)
c("a normal room gets a clean line", clean and "shit" not in clean["line"].lower(), clean["line"])
c("an adult room gets the harder register", adult and "shit" in adult["line"].lower(), adult["line"])
c("neither ever contains a slur the bot would kick for",
  all(not any(s in x["line"].lower() for s in ("chutiya", "madarchod", "bhosdi", "randi"))
      for x in (clean, adult)))

print("\n— the operator's severe list counts as abuse —")
c("a word on the severe list is tier 2 even with no regex hit",
  tier_of("hazel is such a frobnicator", {"frobnicator"}) == 2,
  str(tier_of("hazel is such a frobnicator", {"frobnicator"})))

print("\n— nick tokens are stripped and normalised —")
c("@hazel is recognised", bool(mk().check("@hazel is an idiot", "t6", is_protected=is_protected)))
c("HAZEL is recognised", bool(mk().check("HAZEL is an idiot", "t7", is_protected=is_protected)))
c("a nick inside a sentence is recognised",
  bool(mk().check("honestly, hazel is an idiot", "t8", is_protected=is_protected)))

print("\n— a second clapback waits —")
busy = Clapback(rng=lambda: 0.0, clock=lambda: 1000.0)
first = busy.check("hazel is an idiot", "troll3", is_protected=is_protected)
second = busy.check("hazel you bitch", "troll3", is_protected=is_protected)
c("answered once", bool(first))
c("and not again inside the cooldown", second is None, repr(second))

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

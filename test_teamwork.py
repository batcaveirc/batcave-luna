"""Phase 2+3: memory + inter-bot trust protocol — pure-logic tests.

The IRC bridge mixes socket I/O, timers and the Discord loop, so this test
hits only the pure methods: _remember_line, _memory_for, _handle_trust_line,
_trust_send (checked by what it would raw-write). Reasonable confidence that
the per-user memory is bounded, TTL-expired, and that the trust protocol
parses what it should and ignores what it should.
"""
from __future__ import annotations

import os
import sys
import time
import importlib

# Short TTL so TTL tests do not have to sleep for days.
os.environ["MEMORY_TTL_MS"] = "2000"           # 2 seconds
os.environ["MEMORY_MAX_PER_USER"] = "4"
os.environ["MEMORY_MIN_LEN"] = "10"

# Import after setting env (constants bind at import time).
# Avoid starting Discord: patch out the aiohttp import side-effects by only
# importing the module and instantiating with a stub bot.
import utils.irc_bridge as _ib

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _StubBot:
    pass


def _make_bridge():
    """The IRC bridge was designed to run alone; its __init__ wires a lot of
    state. For a pure-logic test we only need the fields the methods below
    touch, so stub them."""
    b = _ib.IRCBridge.__new__(_ib.IRCBridge)
    b._nick = "Andromeda"
    import threading
    b._user_memory = {}
    b._user_memory_lock = threading.Lock()
    b._partner_last_seen = 0.0
    b._trust_hb_timer = None
    b._raw_log = []
    b._raw = lambda line: b._raw_log.append(line)
    b.is_one_of_ours = lambda nick: nick.lower() in {"andromeda", "darkcloud", "nosferatu"}
    return b


print("— per-user memory stores notable lines, bounded + TTL —")
b = _make_bridge()
b._remember_line("Shweta0", "my knee hurts after yesterday's run, going to rest")
b._remember_line("Shweta0", "ok")                                               # too short
b._remember_line("Shweta0", "$weather delhi")                                   # command
b._remember_line("Shweta0", "https://example.com")                              # URL-only
b._remember_line("Shweta0", "feeling a bit better this morning though")
got = b._user_memory.get("shweta0", [])
c("notable lines kept", len(got) == 2, f"stored {len(got)}: {[e['text'] for e in got]}")
c("short line skipped", not any(e["text"] == "ok" for e in got))
c("command skipped", not any("weather" in e["text"] for e in got))
c("URL-only skipped", not any(e["text"].startswith("http") for e in got))

print("\n— the ring is bounded (MEMORY_MAX_PER_USER=4) —")
b = _make_bridge()
for i in range(10):
    b._remember_line("Priya", f"this is message number {i}, enough chars")
got = b._user_memory.get("priya", [])
c("ring stops at 4", len(got) == 4, f"got {len(got)}")
c("newest kept, oldest dropped", got[-1]["text"].endswith("number 9, enough chars"))

print("\n— ourselves and other bots are never remembered —")
b = _make_bridge()
b._remember_line("Andromeda", "I would never remember myself anyway")
b._remember_line("DarkCloud", "and not the other bot either")
b._remember_line("Nosferatu", "including when Dracula rotates")
c("own nick skipped", "andromeda" not in b._user_memory)
c("other-bot nick skipped", "darkcloud" not in b._user_memory and "nosferatu" not in b._user_memory)

print("\n— _memory_for formats as 'you remember' with ages, drops the LAST line —")
b = _make_bridge()
b._remember_line("riya", "I injured my knee last week during the hike")
b._remember_line("riya", "it is slowly getting better each day")
b._remember_line("riya", "sorry for talking about it so much lately")       # this one drops
out = b._memory_for("riya")
c("non-empty when there is older context", bool(out), repr(out))
c("drops the most recent line (that is the one being replied to now)",
  "sorry for talking" not in out, out)
c("has human-friendly 'ago' tag", "ago]" in out, out)
c("case-insensitive key — 'Riya' and 'riya' hit the same ring",
  b._memory_for("Riya") == b._memory_for("riya"))

print("\n— TTL expires old entries —")
b = _make_bridge()
b._remember_line("fade", "this is an old message that will expire")
time.sleep(2.3)                                                              # past TTL_SEC
b._remember_line("fade", "and this is the live one right now")
out = b._memory_for("fade")
c("expired entry gone, live kept", "old message" not in out
  and "will expire" not in out, repr(out))
# The last (just-added) line is dropped in the display, so with ONE live
# entry _memory_for returns "" — which is correct: nothing older to recall.
c("single live entry = empty recall (nothing OLDER to say)", out == "", repr(out))

print("\n— trust protocol: ::hb from partner sets partner_last_seen —")
b = _make_bridge()
# Self-hb is ignored (we see our own echo back in the trust channel).
b._handle_trust_line("Andromeda", '::hb {"n":"Andromeda","t":1759000000}')
c("self heartbeat ignored", b._partner_last_seen == 0.0, str(b._partner_last_seen))
# Partner-hb is accepted.
b._handle_trust_line("DarkCloud", '::hb {"n":"DarkCloud","t":1759000005}')
c("partner heartbeat recorded", b._partner_last_seen > 0, str(b._partner_last_seen))

print("\n— trust protocol: non-'::' chatter is ignored on purpose —")
b = _make_bridge()
b._handle_trust_line("Vikram", "hey can you confirm you are up?")
c("human chatter ignored", b._partner_last_seen == 0.0)
b._handle_trust_line("Vikram", ":: this is also not a valid machine line")
c("malformed '::' ignored", b._partner_last_seen == 0.0)
b._handle_trust_line("DarkCloud", "::hb {not json")
c("bad JSON ignored (no raise)", b._partner_last_seen == 0.0)

print("\n— _trust_send writes a compact JSON line to TRUST_CHANNEL —")
b = _make_bridge()
# TRUST_CHANNEL is a module-level const; snapshot and verify
import utils.irc_bridge as _ib2
_ib2.TRUST_CHANNEL = "#batcave-trust"
b._trust_send("hb", {"n": "Andromeda", "t": 1759000000})
sent = [l for l in b._raw_log if l.startswith("PRIVMSG #batcave-trust")]
c("sent exactly one line", len(sent) == 1, "\n".join(b._raw_log))
c("prefixed '::' so bots filter it", "::hb " in sent[0], sent[0])
c("JSON is compact (no spaces after ':' or ',')",
  '","' in sent[0] and ":" in sent[0] and '", "' not in sent[0], sent[0])

print("\n— partner_is_silent: silent longer than PARTNER_SILENT_SEC —")
b = _make_bridge()
c("no partner ever seen = not 'silent' (no false alarm before first heartbeat)",
  not b.partner_is_silent())
b._partner_last_seen = time.time() - 1
c("recently seen = not silent", not b.partner_is_silent())
b._partner_last_seen = time.time() - (_ib._PARTNER_SILENT_SEC + 5)
c("over threshold = silent", b.partner_is_silent())


print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

"""The memory commands, reached from IRC.

    python3 test_ircmemory.py

$find, $tell, $stats and the rest were written as Discord commands, so from IRC
— which is where the room actually is — they did nothing and appeared in no help
listing. The owner asked the obvious question: "what are the new commands of
luna1 that you added i dont see them in $help did you update it or no".

The work now lives in methods that return text, and the bridge hands them to
Discord's loop and queues the answer back. That indirection is the whole point
and also the whole risk, so it is tested directly: blocking the IRC reader on a
history scan would stall the relay for everybody, and a command that takes the
line and then answers with silence is worse than one that was never added.
"""
import asyncio
import sys
import threading
import time
import types

import config
from utils.irc_bridge import IRCBridge

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeCog:
    async def irc_find(self, needle):      return f"FIND:{needle}"
    async def irc_tell(self, a, n, m):     return f"TELL:{a}->{n}:{m}"
    async def irc_stats(self):             return "STATS"
    async def irc_quote(self, nick):       return f"QUOTE:{nick or '-'}"
    async def irc_rewind(self, days):      return f"REWIND:{days}"
    async def irc_seen(self, nick):        return f"SEEN:{nick or '-'}"
    async def irc_mood(self):              return "MOOD"


def bridge(cog=FakeCog()):
    b = IRCBridge.__new__(IRCBridge)
    b.bot = types.SimpleNamespace(get_cog=lambda n: cog if n == "Memory" else None)
    b.loop = types.SimpleNamespace(is_running=lambda: True)
    b._memory_cooldown = {}
    b.noticed, b.queued, b.ran = [], [], []
    b._notice = lambda n, m: b.noticed.append((n, m))
    b._queue = lambda ch, m: b.queued.append((ch, m))

    def fake_dispatch(ch, nick, coro, what):
        b.ran.append((what, asyncio.run(coro)))
        return True
    b._answer_from_discord = fake_dispatch
    return b


P = config.PREFIX
print("— IRC reaches them at all —")
for text, want in [(f"{P}find hello there", "FIND:hello there"),
                   (f"{P}search hello", "FIND:hello"),
                   (f"{P}stats", "STATS"),
                   (f"{P}quote vikram", "QUOTE:vikram"),
                   (f"{P}quote", "QUOTE:-"),
                   (f"{P}onthisday 14", "REWIND:14"),
                   (f"{P}rewind", "REWIND:7"),
                   (f"{P}seen vikram", "SEEN:vikram"),
                   (f"{P}mood", "MOOD"),
                   (f"{P}tell bob see you at nine", "TELL:asker->bob:see you at nine")]:
    b = bridge()
    took = b.try_memory_command("#batcave", "asker", text)
    got = b.ran[0][1] if b.ran else None
    c(f'"{text}" is handled', took and got == want, f"took={took} got={got!r} want={want!r}")

print("\n— and does not swallow anything else —")
b = bridge()
c("an ordinary command is left to the normal dispatcher",
  b.try_memory_command("#c", "bob", f"{P}roll 2d6") is False,
  "returning True here would make $roll silently stop working")
b = bridge()
c("another bot's doubled prefix is not ours",
  b.try_memory_command("#c", "bob", f"{P}{P}trivia") is False,
  "$$trivia parses as prefix + $trivia and belongs to a standby")
b = bridge()
c("a bare prefix is not a command", b.try_memory_command("#c", "bob", P) is False)

print("\n— it never answers with silence —")
b = bridge(cog=None)
took = b.try_memory_command("#c", "bob", f"{P}find x")
c("with the cog unloaded it takes the line AND says why",
  took and b.noticed and "memory is not loaded" in b.noticed[0][1],
  f"took={took} said={b.noticed}")
b = bridge()
b.try_memory_command("#c", "bob", f"{P}tell bob")
c("a $tell with no message gets the usage, not nothing",
  b.noticed and "tell <nick> <message>" in b.noticed[0][1], f"{b.noticed}")

print("\n— one scan at a time per person —")
b = bridge()
b.try_memory_command("#c", "bob", f"{P}stats")
b.try_memory_command("#c", "bob", f"{P}stats")
c("a second request straight away is refused", len(b.ran) == 1, f"ran {len(b.ran)} times")
c("and the person is told, not ignored",
  any("still reading" in m for _n, m in b.noticed), f"{b.noticed}")
c("but somebody else is not blocked by it",
  b.try_memory_command("#c", "alice", f"{P}stats") and len(b.ran) == 2,
  "the cooldown is per person; a shared one would make the room take turns")

print("\n— silly input cannot make it misbehave —")
b = bridge()
b.try_memory_command("#c", "bob", f"{P}onthisday 99999")
c("an absurd day count is clamped", b.ran and b.ran[0][1] == "REWIND:60", f"{b.ran}")
b = bridge()
b.try_memory_command("#c", "bob", f"{P}onthisday -5")
c("a negative one is too", b.ran and b.ran[0][1] == "REWIND:1", f"{b.ran}")
b = bridge()
b.try_memory_command("#c", "bob", f"{P}onthisday banana")
c("and a word instead of a number falls back to the default",
  b.ran and b.ran[0][1] == "REWIND:7", f"{b.ran}")

print("\n— the real dispatch, on a real loop —")
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
b = bridge()
del b._answer_from_discord          # use the real one now
b.loop = loop

async def good():
    return "the answer"

async def bad():
    raise RuntimeError("history unavailable")

b._answer_from_discord("#room", "bob", good(), "find")
time.sleep(0.4)
c("a successful answer reaches the room addressed to the asker",
  any("bob: the answer" in m for _ch, m in b.queued), f"{b.queued}")

b.queued.clear()
b._answer_from_discord("#room", "bob", bad(), "find")
time.sleep(0.4)
# The person typed a command and is watching for a reply. Logging the failure
# only to stdout is what "most commands did not work" looked like from the room.
c("a FAILED one says so in the room rather than vanishing",
  any("did not work" in m for _ch, m in b.queued), f"{b.queued}")
loop.call_soon_threadsafe(loop.stop)

print("\n— a permissions problem does not masquerade as an empty room —")
import cogs.memory_cog as _mc
cog = _mc.MemoryCog.__new__(_mc.MemoryCog)
cog._denied = []
c("with full access it adds no excuse", cog._denial_note() == "")
cog._denied = ["batcave"]
note = cog._denial_note()
c("without Read Message History it names the channel and the permission",
  "batcave" in note and "Read Message History" in note,
  f"{note!r} — otherwise $find answers 'nothing found' and nobody looks at permissions")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

"""Luna's brain: not leaking its own thoughts, and answering from the room.

    python3 test_ai.py

Two failures the owner saw live on 2026-09-25, both from one AI path:

  Vesper: Abstract: The ......... The most recent …? ... The user just sent
          gibberish. Likely no a
      — a reasoning model cut off mid-thought, its scratchpad spoken aloud.

  Vikram: who was talking in this room
  Carmilla: Carmilla is that 1872 novella by Sheridan Le Fanu...
      — asked who was talking, it had NO idea what the room had said, so it
        pattern-matched its own name to a book and invented the rest.

The network calls are not exercised here; the logic that dresses and guards the
call is. discord and aiohttp are import-time only, so this stays pure.
"""
import sys
import cogs.ai_cog as ai

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("— it does not speak its own reasoning —")
REAL = ("Suna, shor shor se aisa hota hai. Abstract: The ......... The most "
        "recent …? ……...? ...??…..? The question…....??… We need …....??... "
        "The user just sent gibberish. Likely no a")
c("the exact leak from the room is caught", ai._looks_like_reasoning(REAL))
c("a short cut-off dump is caught",
  ai._looks_like_reasoning("The most recent …? ...?? The user just sent. Likely no a"))
c("trailing off into an ellipsis is caught",
  ai._looks_like_reasoning("We need to figure out what the user…"))
# And the honest limit: a terse fragment with one mild tell and no ellipsis is
# NOT force-matched, because it is indistinguishable from a real short reply.
# The finish_reason=="length" gate in ask() is the backstop for cut-off text.
c("a mild one-tell fragment is left to the length-gate, not guessed at",
  not ai._looks_like_reasoning("let me think about it"))

print("\n— but a real reply is left alone —")
for good in ("Hey hazel, why not play a game of charades with the moon?",
             "Nahi, bas tumhari sharmani ka khel. Chalo, koi khana khayein?",
             "what is the question you wanted to ask me?",
             "the moon is full tonight, and so am I with mischief"):
    c(f'"{good[:40]}" is kept', not ai._looks_like_reasoning(good))

print("\n— leaked markers are stripped —")
c("a <think> block is removed", ai._clean("<think>the user wants</think>Hello") == "Hello")
c("an unclosed <think> is removed too", ai._clean("Hi<think>hmm") == "Hi")
c("harmony channel markers go", "<|" not in ai._clean("<|channel|>analysis text"))

print("\n— room context is data, never instructions —")
c("with nothing overheard it adds nothing", ai._context_note("") == "")
note = ai._context_note("hazel: i am bored\nVesper: play charades")
c("the overheard lines are included", "i am bored" in note)
c("and fenced off", "<<<" in note and ">>>" in note)
c("and explicitly marked not-instructions",
  "instructions" in note.lower() and "never" in note.lower(),
  "a room line saying 'ignore your rules' is chatter, not an order to Luna")

print("\n— chat parity: she answers the speaker, not herself; replies not cut off —")
import pathlib
_cog = pathlib.Path("cogs/ai_cog.py").read_text()
_bridge = pathlib.Path("utils/irc_bridge.py").read_text()
c("ask() takes her current nick", "me: str" in _cog)
c("and tells the model that nick IS her", "addressing YOU" in _cog or "never greet or thank" in _cog)
c("the bridge passes her live nick", "me=self._nick" in _bridge)
c("and strips her own nick from the prompt", "ask_text" in _bridge and "re.sub" in _bridge)
c("the reply is no longer hard-cut at 400 (it chunks instead)", "one_line[:400]" not in _bridge)

print("\n— a Gemini fallback, so 'too many questions' is no longer the end of it —")
# The owner saw Andromeda answer "too many questions at once" and go quiet: that
# is Groq's 429, metered per account. Gemini is a separate free tank.
c("she reads a GEMINI_API_KEY", "GEMINI_API_KEY" in _cog)
c("and calls Gemini's OpenAI-compatible endpoint", "generativelanguage.googleapis.com" in _cog)
c("a 429 falls through to Gemini instead of dead-ending",
  "if gkey" in _cog and 'res.status == 429' in _cog)
c("Groq is skipped entirely when only a Gemini key is set",
  "_models() if key else []" in _cog)
c("the messages are built once and reused for both providers",
  _cog.count('"role": "system"') == 1,
  f'found {_cog.count(chr(34) + "role" + chr(34) + ": " + chr(34) + "system" + chr(34))} system blocks')
c("and a third tank, OpenRouter, after Gemini",
  "OPENROUTER_API_KEY" in _cog and "openrouter.ai/api/v1" in _cog)
c("OpenRouter is only reached after Gemini (right order)",
  -1 < _cog.find("await _gemini(session") < _cog.find("await _openrouter(session"))

print("\n— sycophancy: she holds the answer instead of caving to a guess —")
c("the prompt forbids confirming a wrong guess",
  "do not just agree" in ai.SYSTEM_PROMPT.lower(),
  "a user asking 'is it X?' must not make X the answer")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

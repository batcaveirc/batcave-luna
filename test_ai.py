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

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

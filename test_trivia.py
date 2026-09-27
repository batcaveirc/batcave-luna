"""Trivia — the matching that has to be forgiving, and the flow.

    python3 test_trivia.py

Most of the risk is in answer matching: too strict and the room gives up ("Nile"
rejecting "the nile"), too loose and "2" wins on "20 questions". Both directions
are pinned here.
"""
import sys

from utils.trivia import Trivia, matches, QUESTIONS

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("— answer matching is forgiving but not sloppy —")
c("exact match", matches("Tokyo", ["Tokyo"]))
c("case-insensitive", matches("tokyo", ["Tokyo"]))
c("ignores 'the' and punctuation", matches("the Nile!", ["Nile"]))
c("accepts the answer inside a sentence", matches("i think it's jupiter", ["Jupiter"]))
c("accepts a listed variant", matches("seven", ["7", "seven"]))
c("a wrong answer is rejected", not matches("Paris", ["Tokyo"]))
c("a short numeric answer does not match a longer number",
  not matches("20 questions later", ["2"]),
  "substring matching would wrongly accept this")
c("empty guess never matches", not matches("", ["Tokyo"]))

print("\n— every question in the bank is answerable —")
bad = [q for q, a in QUESTIONS if not a or not all(isinstance(x, str) and x for x in a)]
c("no question has an empty answer list", not bad, f"{bad[:2]}")
c("and its own canonical answer matches itself",
  all(matches(a[0], a) for _q, a in QUESTIONS))

print("\n— a running session tracks a winner and scores —")
t = Trivia()
q = t.start()
c("start returns a question and marks running", bool(q) and t.running)
# nobody's answered yet
c("a wrong line does not win", t.check("bob", "no idea") is None)
right = t.answer_text()
c("the first correct answer wins", t.check("alice", right) == "alice")
c("and it is scored", t.scores.get("alice") == 1)
c("a second correct answer does not double-win the same question",
  t.check("bob", right) is None, "the question is already answered")

print("\n— it moves on and can stop with a leaderboard —")
q2 = t.next_question()
c("next_question gives a fresh question", bool(q2))
c("and resets the answered flag", not t.answered)
t.check("alice", t.answer_text())
board = t.stop()
c("stop is not running and reports the top scorer",
  not t.running and "alice" in board, board)

print("\n— a one-off question needs no session —")
t2 = Trivia()
q = t2.one_off()
c("one_off returns a question without starting a session", bool(q) and not t2.running)
c("a one-off can be answered (winner returned)", t2.check("x", t2.answer_text()) == "x")
c("but a one-off keeps no leaderboard", t2.scores == {},
  "only a running session scores; a one-off just announces the winner")

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

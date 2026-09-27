"""Trivia — one question at a time, first correct answer wins.

Pure logic here (question bank, answer matching, session state and scoring); the
bridge drives the timers and the room I/O, so this stays testable without a
socket. Answer matching is deliberately forgiving — case, punctuation, "the"/"a"
and surrounding chatter are ignored — because a quiz that rejects "the nile"
when the answer is "Nile" just annoys the room.
"""
from __future__ import annotations

import random
import re

# Kept modest and general-knowledge. Each entry: question, then accepted answers
# (first is the canonical one shown on reveal).
QUESTIONS: list[tuple[str, list[str]]] = [
    ("Which planet is the largest in our solar system?", ["Jupiter"]),
    ("What is the capital of Japan?", ["Tokyo"]),
    ("How many continents are there on Earth?", ["7", "seven"]),
    ("What is the chemical symbol for gold?", ["Au"]),
    ("Who wrote the play 'Romeo and Juliet'?", ["Shakespeare", "William Shakespeare"]),
    ("What is the tallest mountain in the world?", ["Everest", "Mount Everest"]),
    ("What is the longest river in the world?", ["Nile", "Amazon"]),
    ("How many strings does a standard guitar have?", ["6", "six"]),
    ("What gas do plants absorb from the air?", ["carbon dioxide", "co2"]),
    ("What is the smallest prime number?", ["2", "two"]),
    ("In which country would you find the Taj Mahal?", ["India"]),
    ("What is the hardest natural substance on Earth?", ["diamond"]),
    ("How many colours are there in a rainbow?", ["7", "seven"]),
    ("What is the currency of the United Kingdom?", ["pound", "pound sterling", "gbp"]),
    ("Which ocean is the largest?", ["Pacific", "Pacific Ocean"]),
    ("What is the freezing point of water in Celsius?", ["0", "zero"]),
    ("Who painted the Mona Lisa?", ["Da Vinci", "Leonardo da Vinci", "Leonardo"]),
    ("What is the largest mammal in the world?", ["blue whale", "whale"]),
    ("How many minutes are there in a full day?", ["1440"]),
    ("What planet is known as the Red Planet?", ["Mars"]),
    ("What is the capital of France?", ["Paris"]),
    ("Which metal is liquid at room temperature?", ["mercury"]),
    ("How many players are on a football (soccer) team on the field?", ["11", "eleven"]),
    ("What is the largest desert in the world?", ["Antarctica", "Sahara"]),
    ("What language has the most native speakers?", ["Mandarin", "Chinese", "Mandarin Chinese"]),
    ("What is the square root of 144?", ["12", "twelve"]),
    ("Which festival is known as the festival of lights in India?", ["Diwali", "Deepavali"]),
    ("What organ pumps blood through the body?", ["heart"]),
    ("How many sides does a hexagon have?", ["6", "six"]),
    ("What is the capital of Italy?", ["Rome"]),
]

_STOP = {"the", "a", "an", "of", "is", "it", "its", "in", "at"}


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", (text or "").lower())
    words = [w for w in text.split() if w not in _STOP]
    return " ".join(words)


def matches(guess: str, answers: list[str]) -> bool:
    g = _norm(guess)
    if not g:
        return False
    for a in answers:
        na = _norm(a)
        if not na:
            continue
        # Exact normalized match, or the answer appears as a whole phrase inside
        # a longer guess ("i think it's the nile" -> "nile"). Substring only for
        # answers of a real length, so "2" doesn't match "20 questions".
        if g == na:
            return True
        if len(na) >= 3 and re.search(rf"(^| ){re.escape(na)}( |$)", g):
            return True
    return False


class Trivia:
    """One session's state: whether it's running, the current question, scores.

    The bridge owns the reveal/next timing; this just tracks what is being asked
    and who has answered.
    """

    def __init__(self):
        self.running = False
        self.idx = -1            # index into QUESTIONS of the current question
        self.answered = False    # has the current question been won/revealed?
        self.scores: dict[str, int] = {}
        self._order: list[int] = []
        self._pos = 0

    def start(self) -> str:
        self.running = True
        self.scores = {}
        # A shuffled run through the bank, so a session doesn't repeat until it
        # has to.
        self._order = list(range(len(QUESTIONS)))
        random.shuffle(self._order)
        self._pos = 0
        return self._advance()

    def stop(self) -> str:
        self.running = False
        if not self.scores:
            return "Trivia stopped."
        board = ", ".join(f"{n} ({s})" for n, s in
                          sorted(self.scores.items(), key=lambda kv: -kv[1])[:5])
        return f"Trivia stopped. Top: {board}"

    def _advance(self) -> str:
        if not self._order:
            self._order = list(range(len(QUESTIONS)))
            random.shuffle(self._order)
            self._pos = 0
        self.idx = self._order[self._pos % len(self._order)]
        self._pos += 1
        self.answered = False
        return QUESTIONS[self.idx][0]

    def next_question(self) -> str:
        return self._advance()

    def question(self) -> str:
        return QUESTIONS[self.idx][0] if self.idx >= 0 else ""

    def answer_text(self) -> str:
        return QUESTIONS[self.idx][1][0] if self.idx >= 0 else ""

    def check(self, nick: str, text: str):
        """A room line against the current question. Returns the winner's nick
        if it is the first correct answer, else None. Works for a one-off too;
        only a running SESSION keeps score."""
        if self.answered or self.idx < 0:
            return None
        if matches(text, QUESTIONS[self.idx][1]):
            self.answered = True
            if self.running:
                self.scores[nick] = self.scores.get(nick, 0) + 1
            return nick
        return None

    def one_off(self) -> str:
        """A single question outside a running session (the plain $trivia)."""
        self.idx = random.randrange(len(QUESTIONS))
        self.answered = False
        return QUESTIONS[self.idx][0]

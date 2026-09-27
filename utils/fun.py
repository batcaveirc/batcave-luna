"""Small canned fun — stateless, no API, no persistence, safe on an ephemeral host.

Ported in spirit from the Vampire bot's fun set (the parts worth having — not the
text-mangling ones the owner didn't want). Everything here is a pure function of
its input plus a random pick, so it needs no storage and cannot break on a
restart.
"""
from __future__ import annotations

import random

EIGHTBALL = [
    "It is certain.", "Without a doubt.", "Yes — definitely.", "You may rely on it.",
    "Most likely.", "Outlook good.", "Signs point to yes.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Don't count on it.",
    "My reply is no.", "Very doubtful.", "The moon says no, darling.",
]
DADJOKES = [
    "I'm reading a book on anti-gravity. It's impossible to put down.",
    "I only know 25 letters of the alphabet. I don't know y.",
    "What do you call a fish with no eyes? A fsh.",
    "I made a belt out of watches. It was a waist of time.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "I'm on a seafood diet. I see food and I eat it.",
    "What did the ocean say to the shore? Nothing, it just waved.",
]
FACTS = [
    "Octopuses have three hearts and blue blood.",
    "Honey never spoils — 3000-year-old honey is still edible.",
    "A day on Venus is longer than its year.",
    "Bananas are berries, but strawberries are not.",
    "Wombat poop is cube-shaped.",
    "There are more possible chess games than atoms in the observable universe.",
    "Sharks existed before trees did.",
]
ICEBREAKERS = [
    "If you could have dinner with anyone, living or dead, who?",
    "What's the last thing that made you laugh out loud?",
    "Tea or coffee — and defend it.",
    "What's a small thing that instantly improves your day?",
    "If you had to delete one app forever, which?",
    "What song is stuck in your head right now?",
]

# Action verbs: {a} does something to {b}. Each verb has a few flavours.
ACTIONS = {
    "hug":      ["{a} wraps {b} in a warm hug.", "{a} pulls {b} into a bear hug."],
    "pat":      ["{a} gently pats {b} on the head.", "{a} gives {b} an approving pat."],
    "slap":     ["{a} slaps {b} with a dramatic flourish.", "{a} delivers {b} a theatrical slap."],
    "bite":     ["{a} sinks fangs into {b} — playfully, mostly.", "{a} gives {b} a little vampire nibble."],
    "highfive": ["{a} leaves {b} hanging... then high-fives.", "{a} and {b} nail a perfect high-five."],
    "poke":     ["{a} pokes {b}. Twice.", "{a} keeps poking {b} until they notice."],
    "cheer":    ["{a} cheers {b} on wildly.", "{a} starts a chant for {b}."],
}


def eightball() -> str:
    return random.choice(EIGHTBALL)


def dadjoke() -> str:
    return random.choice(DADJOKES)


def fact() -> str:
    return random.choice(FACTS)


def icebreaker() -> str:
    return random.choice(ICEBREAKERS)


def action(verb: str, sender: str, target: str) -> str:
    """A roleplay action line. If no target, aim it at the room generally."""
    bank = ACTIONS.get((verb or "").lower())
    if not bank:
        return ""
    b = target.strip() if target and target.strip() else "the room"
    return random.choice(bank).format(a=sender, b=b)


ACTION_VERBS = tuple(ACTIONS.keys())

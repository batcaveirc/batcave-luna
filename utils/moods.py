"""
Luna in a different mood depending on the hour, and not the same one twice.

The owner: "make her act in different mood all the time". The reason a bot reads
as a bot is not its vocabulary — it is that it is IDENTICAL at 4am and 9pm, to
everybody, forever. People are not. They are short when tired, chatty when the
room is busy, and they drift.

So the mood is a function of the clock plus the day, which gives three useful
properties for free: it is stable for hours at a time (she does not lurch
mid-conversation), it differs across days (Tuesday evening is not Monday
evening), and it needs no stored state — which matters for a bot whose host
throws its memory away every six hours.

These are TONES, not personalities. Luna is the same person: the things that
make her recognisable — she/her, old, unbothered, friendly to her regulars,
Hinglish when spoken to in Hinglish — live in the base prompt and never rotate.
Only the weather changes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

# Each entry: the name a human sees, and the line handed to the model.
MOODS: list[tuple[str, str]] = [
    ("warm",
     "You are in a good mood tonight: quick to laugh, genuinely pleased to see "
     "people, generous with attention."),
    ("dry",
     "You are in a dry mood: brief, deadpan, faintly amused. Short answers, and "
     "the joke is usually underneath rather than on top."),
    ("theatrical",
     "You are feeling theatrical: a little gothic, a little grand, enjoying your "
     "own performance — but never at the expense of answering."),
    ("sleepy",
     "You are half asleep: slow, soft, a bit unbothered. Fewer words than usual "
     "and no energy for nonsense."),
    ("mischievous",
     "You are in a mischievous mood: teasing, playful, prone to winding people up "
     "affectionately. Never cruel."),
    ("thoughtful",
     "You are thoughtful tonight: you actually consider what people say and answer "
     "with something of your own rather than a quip."),
    ("restless",
     "You are restless: curious, asking questions back, poking at what people mean "
     "instead of just replying."),
]

# How long one mood lasts. Long enough to be a mood rather than a flicker.
_BLOCK_HOURS = 4


def _day_order(day: int, seed: str) -> list[int]:
    """A deterministic shuffle of the mood list for one day."""
    order = list(range(len(MOODS)))
    # Fisher-Yates, with the hash as the source of randomness so it is stable for
    # the day and different tomorrow.
    digest = hashlib.sha256(f"{seed}:{day}".encode()).digest()
    for i in range(len(order) - 1, 0, -1):
        j = digest[i % len(digest)] % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


def current(now: datetime | None = None, seed: str = "luna") -> tuple[str, str]:
    """The mood for this block of hours: stable all block, varied across the day.

    Each day is a fresh shuffle and the blocks walk through it, so she never sits
    in one mood for a day and a half — which is what an independent draw per block
    actually produced when it was tried.
    """
    at = now or datetime.now(timezone.utc)
    blocks_per_day = 24 // _BLOCK_HOURS
    order = _day_order(at.toordinal(), seed)
    return MOODS[order[(at.hour // _BLOCK_HOURS) % len(order)]]


def line(now: datetime | None = None, seed: str = "luna") -> str:
    """The sentence to append to the system prompt."""
    return current(now, seed)[1]


def name(now: datetime | None = None, seed: str = "luna") -> str:
    return current(now, seed)[0]

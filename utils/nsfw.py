"""Opt-in adult mode, with the guardrails that make it opt-in.

A clean reimplementation of the local Vampire bot's NSFW feature — not a copy;
that bot stays local-only. The model is the sound part of it, kept verbatim in
spirit:

    adults only · consent required · no coercion · no harassment · no minors ·
    stop the moment someone says no

Three things make this safe rather than a harassment tool:

1. A room is adult ONLY when an operator turns it on, and turning it on writes a
   disclosure into the channel TOPIC — so anyone entering sees "18+, adult
   content" before they say a word, and staying is their agreement to see it.
   The topic is also where the room's adult state LIVES, which means it survives
   restarts on its own (this bot's host is ephemeral) with no database.

2. A person must say they are 18+ and opt in for this session before the bot
   will involve them at all.

3. A directed line ("tempt <nick>") needs the TARGET to have opted in too — not
   just the sender. The Vampire bot gates only the sender; this does not, because
   aiming a sexual line at someone who never consented is the exact thing the
   rules line above forbids, toggle or no toggle.

Nothing here is explicit: the lines are suggestive, flirtatious, cinematic. This
module deliberately does NOT touch the AI: there is no "unfiltered, no-censorship"
generation, because removing a model's guardrails is how consented adult banter
turns into content nobody agreed to.
"""
from __future__ import annotations

import random

# The disclosure that goes into a room's topic when adult mode is turned on.
# The marker is what the bot looks for to know a room is adult, so it must be
# distinctive and stable.
TOPIC_MARK = "🔞"
TOPIC_NOTICE = (
    f"{TOPIC_MARK} Adult/NSFW room — by staying you confirm you are 18+ and "
    f"consent to adult content. Not for you? You are free to leave. "
    f"Rules: consent always, no harassment, no minors, stop when told."
)

RULES = ("Adults only, consent required, no coercion, no harassment, no minors, "
         "and stop the moment someone says no.")

# Suggestive, not explicit. Cinematic flirtation.
AFTERDARK = [
    "Low lights, good music, zero drama. That's the whole vibe.",
    "After-dark rule: charm first, respect always.",
    "A little tension, a lot of consent, and perfect timing.",
    "Confident, playful, and kind — that's how we do midnight here.",
]
SPICY = [
    "{a} leans in close to {b} and murmurs something dangerously charming.",
    "{a} gives {b} a slow look that says 'I noticed you'.",
    "{a} catches {b}'s eye across the room and holds it just long enough.",
    "{a} pulls {b} into a close, unhurried dance under the low lights.",
]
TEMPT = [
    "{a} gives {b} a playful 'come closer' look and smirks.",
    "{a} leans in and tells {b} they look incredible tonight.",
    "{a} circles {b} slowly with calm, unbothered confidence.",
    "{a} traces the rim of a glass, eyes never leaving {b}.",
]
FANTASY = [
    "{a} and {b} slip off to a velvet-lit corner and trade electric smiles.",
    "{a} slides {b} a note: 'midnight, the rooftop, no excuses.'",
    "{a} offers {b} a hand and says, 'trust me for one night.'",
    "{a} and {b} lock eyes across the room and everything else blurs.",
]
MIDNIGHT = [
    "{a} and {b} steal away to a moonlit balcony for a quiet moment.",
    "{a} meets {b} under neon where the night feels electric.",
    "{a} and {b} let a long, loaded pause say everything words won't.",
]
DESIRE = [
    "{a} admits, quietly, that {b} has been on their mind all night.",
    "{a} tells {b} that the room got warmer the moment they walked in.",
    "{a} confesses a slow-burning weakness for {b}'s smile.",
]

_BANKS = {
    "spicy": SPICY, "tempt": TEMPT, "fantasy": FANTASY,
    "midnight": MIDNIGHT, "desire": DESIRE,
}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


class Nsfw:
    """Room adult-state (via topic) and per-session consent (in memory)."""

    def __init__(self, topic_of=None):
        # topic_of(channel) -> current topic string, so a room's adult state can
        # be read from the live topic rather than a file that would not survive
        # a restart. Injected so the manager is testable without a socket.
        self._topic_of = topic_of or (lambda ch: "")
        # Consent is per session, on purpose: it resets on reconnect, so nobody
        # is opted in "forever" by something they typed weeks ago.
        self._age18: set[str] = set()
        self._consent: set[str] = set()

    # ── room state, read from the topic ──────────────────────────────────────
    def room_is_adult(self, channel: str) -> bool:
        return TOPIC_MARK in (self._topic_of(channel) or "")

    def topic_with_notice(self, existing: str) -> str:
        """The topic to SET when turning adult mode on: the disclosure, plus
        whatever the room's topic already said (once)."""
        base = (existing or "").strip()
        if TOPIC_MARK in base:
            return base                                  # already marked
        return f"{TOPIC_NOTICE}  |  {base}".strip(" |") if base else TOPIC_NOTICE

    def topic_without_notice(self, existing: str) -> str:
        """The topic to SET when turning it off: strip our disclosure, keep the
        rest."""
        base = (existing or "")
        if TOPIC_MARK not in base:
            return base.strip()
        # Remove our notice segment; keep anything the room added after it.
        parts = [p.strip() for p in base.split("|")]
        kept = [p for p in parts if TOPIC_MARK not in p]
        return "  |  ".join(kept).strip()

    # ── per-user opt-in ──────────────────────────────────────────────────────
    def set_age18(self, nick: str, yes: bool):
        (self._age18.add if yes else self._age18.discard)(_norm(nick))

    def set_consent(self, nick: str, yes: bool):
        (self._consent.add if yes else self._consent.discard)(_norm(nick))
        if not yes:
            self._age18.discard(_norm(nick))             # a hard opt-out clears both

    def opted_in(self, nick: str) -> bool:
        n = _norm(nick)
        return n in self._age18 and n in self._consent

    def status(self, nick: str) -> str:
        n = _norm(nick)
        return (f"18+: {'yes' if n in self._age18 else 'no'}, "
                f"consent: {'on' if n in self._consent else 'off'}")

    # ── producing a line, only when everyone involved agreed ─────────────────
    def line(self, kind: str, channel: str, sender: str, target: str = ""):
        """Return (text, refusal). Exactly one is non-empty.

        refusal is a short reason the caller can show; text is the roleplay
        line. The gate is the whole point, so it is here, not in the caller.
        """
        if not self.room_is_adult(channel):
            return "", "not here — this only works in a room an operator has set to adult mode."
        if not self.opted_in(sender):
            return "", "you have not opted in — say '$age18 yes' then '$consent on' first."
        kind = _norm(kind)
        if kind == "afterdark":
            return random.choice(AFTERDARK), ""
        bank = _BANKS.get(kind)
        if not bank:
            return "", f"I don't know that one. Try: afterdark, {', '.join(_BANKS)}."
        if not target:
            return "", "at whom? (give a nick)"
        if _norm(target) == _norm(sender):
            return random.choice(bank).format(a=sender, b=sender), ""
        # The guardrail Vampire lacks: the person on the receiving end must have
        # opted in too. No aiming a sexual line at someone who never agreed.
        if not self.opted_in(target):
            return "", f"{target} has not opted into adult mode, so I won't aim that at them."
        return random.choice(bank).format(a=sender, b=target), ""

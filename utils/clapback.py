"""Clapback — the room's answer when somebody turns on one of ours.

Same rules as Dracula's clapback.js, deliberately: two bots defending the same
regulars with two different standards of "too far" would be worse than neither.

    severity 1  mild     — a cutting line, no punishment
    severity 2  abuse    — the line, then the ordinary ladder (devoice first)
    severity 3  threat   — the line, then removal

Severity comes from what was said. "shut up" is not a threat, and a bot that
kicks over one is a bot the room turns off.

In an adult room the reply may swear, because the room asked for that register.
It never uses a slur: the bot moderates those, and a network that sees one in
OUR line does not read intent — it bans the client and takes the channel.
"""
from __future__ import annotations

import random
import re
import time
from typing import Callable, Iterable, Optional

MILD = re.compile(
    r"\b(?:idiot|stupid|dumb|moron|loser|clown|trash|ugly|fat|nobody|cringe|"
    r"beggar|peasant|worthless|pathetic|shut\s*up|stfu|gtfo|get\s*lost|"
    r"nobody\s+asked)\b", re.I)
ABUSE = re.compile(
    r"\b(?:bitch|whore|slut|bastard|asshole|arsehole|dickhead|motherfucker|"
    r"piece\s+of\s+shit|scum|vermin|randi|chutiya|chutiye|madarchod|"
    r"bhosdi\w*|haramkhor|kutta|kamina)\b", re.I)
THREAT = re.compile(
    r"\b(?:kill|murder|rape|acid|dox|doxx|stab|shoot\s+you|beat\s+you|"
    r"find\s+you|hunt\s+you|break\s+your|slit|hang\s+you)\b", re.I)
SEVERE = re.compile(r"\b(?:fuck\s+you|f+u+c+k+|fucking|fuckin|shit)\b", re.I)

POOLS = {
    1: {
        "clean": [
            "{who} is one of mine. Take the attitude somewhere that grades on effort.",
            "Say it to {who} again and the room will remember it longer than you will.",
            "Careful — {who} has friends in here. You are talking to all of us.",
            "That was a swing at {who}. It landed on the room's patience instead.",
        ],
        "nsfw": [
            "Talk shit about {who} somewhere I can't hear you. I hear everything.",
            "{who} is under this roof, {nick}. Pick a softer target or a shorter visit.",
            "That mouth is brave for somebody standing in front of {who}'s friends.",
        ],
    },
    2: {
        "clean": [
            "That is enough. {who} is not your punching bag, and I am not your audience.",
            "One more line like that at {who} and you can explain it to the door.",
            "You came in swinging at {who}. The room noticed. So did I.",
        ],
        "nsfw": [
            "Say that about {who} again and being muted will be the least of your problems.",
            "That was a pathetic swing at {who}. Try it once more and find out how fast I move.",
            "You are embarrassing yourself at {who}'s expense, and I am done being polite about it.",
        ],
    },
    3: {
        "clean": [
            "Threats are not banter. {who} stays, you go.",
            "You just threatened one of mine. That is the whole conversation.",
        ],
        "nsfw": [
            "Threaten {who} one more time and you will not finish the sentence.",
            "That was a threat at {who}. Enjoy the door — it is the only thing here that listens to you.",
        ],
    },
}

REASON = {
    1: "attacking a regular",
    2: "abusing a regular",
    3: "threatening a regular",
}

_TOKEN_SPLIT = re.compile(r"[\s,.:;!?()\[\]\"'<>|]+")


def tokens(text: str) -> list:
    """Every nick-looking token, prefixes stripped, lowercase."""
    out = []
    for t in _TOKEN_SPLIT.split(text or ""):
        t = t.lstrip("@+~&%").strip().lower()
        if len(t) >= 2:
            out.append(t)
    return out


def tier_of(text: str, severe: Optional[Iterable[str]] = None) -> int:
    """Severity of a line: 3 threat, 2 abuse, 1 mild, 0 none."""
    t = text or ""
    if not t:
        return 0
    if THREAT.search(t):
        return 3
    if ABUSE.search(t) or SEVERE.search(t):
        return 2
    if severe:
        words = re.findall(r"[a-z]+", t.lower())
        if any(w in set(severe) for w in words):
            return 2
    if MILD.search(t):
        return 1
    return 0


class Clapback:
    """Answers an attack on a protected person, in proportion."""

    def __init__(self, cooldown_secs: float = 60.0, rng=None, clock=None):
        self.cooldown = max(10.0, float(cooldown_secs))
        self._rng = rng or random.random
        self._clock = clock or time.time
        self._last: dict = {}

    def check(self, msg: str, speaker: str,
              is_protected: Optional[Callable[[str], bool]] = None,
              nsfw: bool = False,
              severe: Optional[Iterable[str]] = None) -> Optional[dict]:
        """The reply to an attack on a protected person, or None.

        Returns {tier, who, line, reason} — 'who' is the protected person the
        line was aimed at, which is what the caller needs for the log.
        """
        me = (speaker or "").strip().lower()
        if not me or not is_protected:
            return None
        # Never answer one of ours: regulars rib each other constantly, and a
        # bot policing that is a bot the room switches off.
        try:
            if is_protected(speaker):
                return None
        except Exception:                                   # noqa: BLE001
            return None
        tier = tier_of(msg, severe)
        if not tier:
            return None
        # It has to be aimed at somebody: an insult about nobody in particular
        # belongs to the ordinary ladder, not here.
        who = ""
        for tok in tokens(msg):
            if tok == me:
                continue
            try:
                if is_protected(tok):
                    who = tok
                    break
            except Exception:                               # noqa: BLE001
                continue
        if not who:
            return None
        now = self._clock()
        if now - self._last.get(me, 0.0) < self.cooldown:
            return None
        self._last[me] = now
        if len(self._last) > 500:
            for k, ts in list(self._last.items()):
                if now - ts > 3600:
                    self._last.pop(k, None)
        pool = POOLS[tier]["nsfw" if nsfw else "clean"]
        line = pool[min(len(pool) - 1, int(self._rng() * len(pool)))]
        return {
            "tier": tier,
            "who": who,
            "line": line.replace("{who}", who).replace("{nick}", speaker),
            "reason": REASON[tier],
        }

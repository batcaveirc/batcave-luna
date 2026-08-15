"""Moon phase calculator — pure Python, no external dependencies."""

import math
from datetime import datetime, timezone


PHASE_NAMES = [
    ("🌑", "New Moon",        "A time of beginnings. Plant seeds, set intentions, embrace the dark."),
    ("🌒", "Waxing Crescent", "Growth stirs. Your desires are taking shape in the shadows."),
    ("🌓", "First Quarter",   "Push forward. Obstacles are tests the moon sends to the worthy."),
    ("🌔", "Waxing Gibbous",  "Refinement. Almost full — patience, little star."),
    ("🌕", "Full Moon",       "Peak power. Emotions run wild. Magic is strongest tonight."),
    ("🌖", "Waning Gibbous",  "Gratitude and release. What no longer serves you, let it go."),
    ("🌗", "Last Quarter",    "Forgiveness. Break old patterns before the dark returns."),
    ("🌘", "Waning Crescent", "Rest and surrender. The moon asks you to trust the cycle."),
]


def get_moon_phase(date: datetime | None = None) -> dict:
    """Return current moon phase info as a dict."""
    if date is None:
        date = datetime.now(timezone.utc)

    # Known new moon reference: Jan 6, 2000 18:14 UTC
    known_new = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    synodic   = 29.53058867   # days

    delta  = (date - known_new).total_seconds() / 86400
    phase  = (delta % synodic) / synodic   # 0.0 → 1.0

    index = int(phase * 8) % 8
    emoji, name, meaning = PHASE_NAMES[index]

    illumination = int(
        50 * (1 - math.cos(2 * math.pi * phase))
    )

    return {
        "emoji":        emoji,
        "name":         name,
        "meaning":      meaning,
        "illumination": illumination,
        "phase_pct":    round(phase * 100, 1),
    }

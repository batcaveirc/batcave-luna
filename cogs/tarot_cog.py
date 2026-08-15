"""Tarot readings, moon phase, and horoscope commands."""

import random
import discord
from discord.ext import commands
from utils.moon import get_moon_phase
import config

# ── Full 78-card tarot deck ───────────────────────────────────────────────────

MAJOR_ARCANA = [
    ("0 - The Fool",         "New beginnings, spontaneity, a leap of faith into the unknown."),
    ("I - The Magician",     "Willpower, skill, manifestation. You have what you need — use it."),
    ("II - The High Priestess", "Intuition, mystery, the subconscious. Listen to what isn't said."),
    ("III - The Empress",    "Fertility, beauty, nature. Abundance flows toward you."),
    ("IV - The Emperor",     "Authority, structure, control. Build your empire with discipline."),
    ("V - The Hierophant",   "Tradition, spiritual guidance. Seek wisdom from those who came before."),
    ("VI - The Lovers",      "Love, alignment, a choice that will define you. Choose wisely."),
    ("VII - The Chariot",    "Determination, victory, control. Charge forward — nothing stops you."),
    ("VIII - Strength",      "Inner power, patience, compassion. The lion bows to a gentle hand."),
    ("IX - The Hermit",      "Solitude, introspection, inner guidance. The answer is within."),
    ("X - Wheel of Fortune", "Cycles, fate, turning points. The wheel spins — embrace change."),
    ("XI - Justice",         "Truth, fairness, cause and effect. What you put out returns to you."),
    ("XII - The Hanged Man", "Surrender, new perspectives, pause. Let go and see differently."),
    ("XIII - Death",         "Transformation, endings, transition. Something must die to be reborn."),
    ("XIV - Temperance",     "Balance, patience, moderation. Blend opposites into something perfect."),
    ("XV - The Devil",       "Shadow self, addiction, illusion. You are more free than you think."),
    ("XVI - The Tower",      "Sudden upheaval, revelation, chaos. Truth shatters what was built on lies."),
    ("XVII - The Star",      "Hope, renewal, faith. After the storm, the stars return."),
    ("XVIII - The Moon",     "Illusion, fear, the subconscious. Not everything is as it seems, darling."),
    ("XIX - The Sun",        "Joy, success, clarity. Let yourself shine without apology."),
    ("XX - Judgement",       "Reflection, reckoning, awakening. Answer the call of your higher self."),
    ("XXI - The World",      "Completion, integration, wholeness. You have arrived — celebrate."),
]

SUITS = {
    "Wands":    "passion, creativity, ambition, fire energy",
    "Cups":     "emotions, relationships, intuition, water energy",
    "Swords":   "intellect, conflict, truth, air energy",
    "Pentacles":"material world, work, money, earth energy",
}

COURT_CARDS = ["Page", "Knight", "Queen", "King"]
PIPS        = ["Ace","Two","Three","Four","Five","Six","Seven","Eight","Nine","Ten"]

MINOR_ARCANA = []
for suit, theme in SUITS.items():
    for pip in PIPS:
        MINOR_ARCANA.append((
            f"{pip} of {suit}",
            f"Speaks of {theme}. A message about the {pip.lower()} expression of {suit.lower()}."
        ))
    for court in COURT_CARDS:
        MINOR_ARCANA.append((
            f"{court} of {suit}",
            f"A {court.lower()} energy in the realm of {theme}."
        ))

ALL_CARDS = MAJOR_ARCANA + MINOR_ARCANA

SPREADS = {
    "single": ["Your Present Energy"],
    "three":  ["Past", "Present", "Future"],
    "cross":  ["Situation", "Challenge", "Advice", "Outcome"],
    "love":   ["Your Heart", "Their Heart", "What Connects You", "What to Watch"],
}

HOROSCOPE_SIGNS = {
    "aries":       "♈ Aries (Mar 21 – Apr 19)",
    "taurus":      "♉ Taurus (Apr 20 – May 20)",
    "gemini":      "♊ Gemini (May 21 – Jun 20)",
    "cancer":      "♋ Cancer (Jun 21 – Jul 22)",
    "leo":         "♌ Leo (Jul 23 – Aug 22)",
    "virgo":       "♍ Virgo (Aug 23 – Sep 22)",
    "libra":       "♎ Libra (Sep 23 – Oct 22)",
    "scorpio":     "♏ Scorpio (Oct 23 – Nov 21)",
    "sagittarius": "♐ Sagittarius (Nov 22 – Dec 21)",
    "capricorn":   "♑ Capricorn (Dec 22 – Jan 19)",
    "aquarius":    "♒ Aquarius (Jan 20 – Feb 18)",
    "pisces":      "♓ Pisces (Feb 19 – Mar 20)",
}

HOROSCOPE_TEMPLATES = [
    "The stars whisper of unexpected shifts — your intuition is your compass today.",
    "Mercury stirs things up, darling. Speak carefully; words carry more weight than you know.",
    "Venus blesses you with warmth. Someone is drawn to your energy whether they admit it or not.",
    "The moon is watching you closely. Rest when needed — power is built in silence.",
    "A cycle closes. Release what no longer feeds your soul and make room for what does.",
    "Your ruling planet aligns with ambition today. Strike while the cosmos is in your favour.",
    "Emotions run deep beneath the surface. Don't ignore what surfaces in dreams tonight.",
    "Fortune favours the bold — and you, little star, are bolder than you believe.",
    "Old wounds may resurface. Face them with grace; they are showing you where your strength grew.",
    "The universe conspires in your favour today, though it wears a mysterious disguise.",
]


class TarotCog(commands.Cog, name="Mystical"):
    """Tarot readings, moon phase, and horoscope."""

    def __init__(self, bot):
        self.bot = bot

    def _draw_cards(self, n: int) -> list[tuple]:
        deck = ALL_CARDS.copy()
        random.shuffle(deck)
        cards = []
        for card, meaning in deck[:n]:
            reversed_ = random.random() < 0.3
            if reversed_:
                cards.append((f"{card} (Reversed)", f"*Reversed* — {meaning} — but the energy is blocked or turned inward."))
            else:
                cards.append((card, meaning))
        return cards

    # ── ~tarot ────────────────────────────────────────────────────────────

    @commands.command(name="tarot", aliases=["t", "cards"])
    async def tarot(self, ctx, spread: str = "three"):
        """Pull tarot cards. Spreads: single | three | cross | love"""
        spread = spread.lower()
        if spread not in SPREADS:
            await ctx.send(
                f"*Luna tilts her head.* Choose a spread, darling: "
                f"`single`, `three`, `cross`, or `love`."
            )
            return

        positions = SPREADS[spread]
        cards     = self._draw_cards(len(positions))

        em = discord.Embed(
            title       = "🔮 Luna's Tarot Reading",
            description = f"*She lays the cards out slowly, eyes never leaving yours...*",
            color       = config.BOT_COLOR,
        )

        for pos, (card, meaning) in zip(positions, cards):
            em.add_field(name=f"**{pos}** — {card}", value=meaning, inline=False)

        em.set_footer(text="The cards reveal what you already know. 🌙")
        await ctx.send(embed=em)

    # ── ~moon ─────────────────────────────────────────────────────────────

    @commands.command(name="moon", aliases=["moonphase", "phase"])
    async def moon(self, ctx):
        """Current moon phase and its meaning."""
        info = get_moon_phase()
        em   = discord.Embed(
            title       = f"{info['emoji']} {info['name']}",
            description = info["meaning"],
            color       = config.BOT_COLOR,
        )
        em.add_field(name="Illumination", value=f"{info['illumination']}%", inline=True)
        em.set_footer(text="Luna feels the pull. Do you? 🌙")
        await ctx.send(embed=em)

    # ── ~horo ─────────────────────────────────────────────────────────────

    @commands.command(name="horo", aliases=["horoscope", "stars", "sign"])
    async def horoscope(self, ctx, *, sign: str = ""):
        """Daily horoscope. ~horo <sign>"""
        sign = sign.strip().lower()
        if sign not in HOROSCOPE_SIGNS:
            signs = " • ".join(HOROSCOPE_SIGNS.keys())
            await ctx.send(f"*Luna raises an eyebrow.* Give me a sign, darling: {signs}")
            return

        reading   = random.choice(HOROSCOPE_TEMPLATES)
        moon_info = get_moon_phase()
        lucky_num = random.randint(1, 99)
        lucky_col = random.choice(["crimson","violet","silver","obsidian","gold","indigo","midnight blue"])

        em = discord.Embed(
            title       = f"🔭 {HOROSCOPE_SIGNS[sign]}",
            description = f"*{reading}*",
            color       = config.BOT_COLOR,
        )
        em.add_field(name="Moon Influence", value=f"{moon_info['emoji']} {moon_info['name']}", inline=True)
        em.add_field(name="Lucky Number",   value=str(lucky_num),  inline=True)
        em.add_field(name="Lucky Colour",   value=lucky_col.title(), inline=True)
        em.set_footer(text="The stars speak only to those who listen. 🌙")
        await ctx.send(embed=em)

    # ── ~dream ────────────────────────────────────────────────────────────

    @commands.command(name="dream", aliases=["interpret"])
    async def dream(self, ctx, *, description: str = ""):
        """Luna interprets your dream. ~dream <what you dreamed>"""
        if not description:
            await ctx.send("*Luna waits.* Tell me your dream, darling. Every detail matters.")
            return

        symbols = {
            "water":   "Your emotions are speaking. Turbulent water = unresolved feelings.",
            "fire":    "Transformation and passion. Something is burning through old patterns.",
            "flying":  "Freedom — or the desperate need for it. What are you running from?",
            "falling": "Loss of control. Your subconscious craves stability right now.",
            "teeth":   "Anxiety about how others perceive you. Confidence, darling.",
            "snake":   "Wisdom, transformation, or a hidden threat. Trust your gut.",
            "moon":    "Intuition and the feminine. The moon is calling you deeper.",
            "death":   "Not literal — rebirth. Something in your life is ready to end and transform.",
            "house":   "Your inner self. Each room is a part of your psyche.",
            "running": "Avoidance. What do you keep escaping from?",
            "dark":    "The unknown — not evil, just unexplored. Time to go deeper.",
            "love":    "A longing, real or metaphorical. The heart always finds a way to speak.",
        }

        desc_lower = description.lower()
        found = [interp for word, interp in symbols.items() if word in desc_lower]
        base  = random.choice([
            f"Your dream carries the energy of **{get_moon_phase()['name']}**.",
            "The veil between worlds thins when we sleep, and yours spoke clearly.",
            "Dreams are letters from your subconscious. This one is urgent.",
        ])

        em = discord.Embed(
            title       = "💭 Dream Interpretation",
            description = f"*You dreamed of: {description[:200]}*\n\n{base}",
            color       = config.BOT_COLOR,
        )
        if found:
            em.add_field(
                name  = "Symbols Detected",
                value = "\n".join(f"• {i}" for i in found[:3]),
                inline=False,
            )
        else:
            em.add_field(
                name  = "Luna's Reading",
                value = "The symbols here are deeply personal. Sit with this dream — its meaning will surface when you stop chasing it.",
                inline=False,
            )
        em.set_footer(text="The subconscious never lies. 🌙")
        await ctx.send(embed=em)


async def setup(bot):
    await bot.add_cog(TarotCog(bot))

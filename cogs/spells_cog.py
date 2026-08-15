"""Spell, hex, bless, and charm system."""

import random
import time
import discord
from discord.ext import commands
import config

# ── Spell pools ───────────────────────────────────────────────────────────────

SPELLS = [
    ("Moonbind",       "Their words twist into riddles for the next hour. Good luck explaining yourself."),
    ("Silver Tongue",  "Every lie they tell tonight will taste like ash."),
    ("Veil of Shadow", "They become oddly invisible in conversations. Not ignored — just... overlooked."),
    ("Luna's Gaze",    "They feel watched. Because they are."),
    ("Star Drift",     "Their thoughts scatter like constellations. Focus eludes them pleasantly."),
    ("Night Bloom",    "A strange calm washes over them at the worst possible moment."),
]

HEXES = [
    ("The Crow's Eye",      "Bad luck follows them in small, irritating ways. Spilled drinks. Autocorrect disasters."),
    ("Vexing Fog",          "Every plan they make today unravels at the edges."),
    ("Thorn Whisper",       "Their exes will inexplicably cross their mind all day."),
    ("The Stumble Hex",     "Socially awkward energy radiates off them. They'll trip over their words constantly."),
    ("Midnight Itch",       "An unshakeable feeling they forgot something important. They didn't. Or did they?"),
    ("The Mirror Crack",    "Every flaw they try to hide will feel magnified to themselves alone."),
    ("Raven's Mockery",     "Every witty comeback they think of arrives three hours too late."),
]

BLESSINGS = [
    ("Moonlight Grace",  "A warm glow of confidence follows them today. People notice."),
    ("Silver Star",      "An unexpected piece of good news finds its way to them."),
    ("Charm of Ease",    "Conversations flow effortlessly. Doors open without being pushed."),
    ("Abundance Rune",   "Something small but delightful will appear in their day — a gift from the universe."),
    ("Heart's Shield",   "Emotional resilience wraps around them like armour today."),
    ("Luna's Favour",    "The moon watches over them tonight. Rest comes easily and dreams are kind."),
]

CHARMS = [
    ("Siren's Pull",     "They become inexplicably magnetic tonight. Eyes follow them everywhere."),
    ("Velvet Aura",      "Their voice takes on a warm, almost hypnotic quality in conversation."),
    ("Rose Thorn",       "Attractive but unapproachable energy — intriguing to exactly the right people."),
    ("Starfire",         "A bold, electric confidence that makes them impossible to ignore."),
    ("The Knowing Look", "They give off the energy of someone who knows far more than they're saying."),
]

CURSE_IMMUNITY = [
    "The hex bounces harmlessly off their suspicious aura.",
    "Their energy is too chaotic — even dark magic gets confused.",
    "Luna's spell fizzles. Some people are simply ungovernable.",
    "The stars intervene. Not today.",
]

# ── Cooldown tracking ─────────────────────────────────────────────────────────
_spell_cooldown: dict[int, float] = {}   # user_id → timestamp
SPELL_COOLDOWN = 60   # seconds


class SpellsCog(commands.Cog, name="Spells"):
    """Cast spells, hexes, blessings, and charms on users."""

    def __init__(self, bot):
        self.bot = bot

    def _check_cooldown(self, user_id: int) -> float:
        """Returns remaining cooldown seconds, or 0 if ready."""
        last = _spell_cooldown.get(user_id, 0)
        remaining = SPELL_COOLDOWN - (time.time() - last)
        return max(0.0, remaining)

    def _set_cooldown(self, user_id: int):
        _spell_cooldown[user_id] = time.time()

    async def _spell_embed(self, ctx, target: discord.Member, spell_type: str,
                           pool: list, color: int, emoji: str):
        remaining = self._check_cooldown(ctx.author.id)
        if remaining > 0:
            await ctx.send(
                f"*Luna raises a hand.* Patience, darling. "
                f"Your magic needs `{remaining:.0f}s` to recharge."
            )
            return

        # 10% immunity chance
        if random.random() < 0.10:
            await ctx.send(f"*Luna mutters darkly.* {random.choice(CURSE_IMMUNITY)}")
            return

        name, effect = random.choice(pool)
        self._set_cooldown(ctx.author.id)

        em = discord.Embed(
            title       = f"{emoji} {name}",
            description = (
                f"*{ctx.author.display_name} casts **{name}** upon **{target.display_name}**...*\n\n"
                f"{effect}"
            ),
            color=color,
        )
        em.set_footer(text=f"Cast by {ctx.author.display_name} • Luna's Spellbook 🌙")
        await ctx.send(embed=em)

    # ── Commands ──────────────────────────────────────────────────────────

    @commands.command(name="spell", aliases=["cast"])
    async def spell(self, ctx, target: discord.Member = None):
        """Cast a mystical spell on someone. ~spell @user"""
        if not target:
            await ctx.send(f"*Luna tilts her head.* Who are you casting on, darling? `{config.PREFIX}spell @user`")
            return
        await self._spell_embed(ctx, target, "spell", SPELLS, 0x9B59B6, "✨")

    @commands.command(name="hex", aliases=["curse"])
    async def hex(self, ctx, target: discord.Member = None):
        """Hex someone with bad luck. ~hex @user"""
        if not target:
            await ctx.send(f"*Luna smirks.* Name your target. `{config.PREFIX}hex @user`")
            return
        await self._spell_embed(ctx, target, "hex", HEXES, 0x2C3E50, "🖤")

    @commands.command(name="bless")
    async def bless(self, ctx, target: discord.Member = None):
        """Bless someone with good fortune. ~bless @user"""
        if not target:
            await ctx.send(f"*Luna softens.* Who deserves a blessing? `{config.PREFIX}bless @user`")
            return
        await self._spell_embed(ctx, target, "bless", BLESSINGS, 0xF1C40F, "🌟")

    @commands.command(name="charm")
    async def charm(self, ctx, target: discord.Member = None):
        """Cast an allure charm on someone. ~charm @user"""
        if not target:
            await ctx.send(f"*Luna raises an eyebrow.* Who needs more charm? `{config.PREFIX}charm @user`")
            return
        await self._spell_embed(ctx, target, "charm", CHARMS, 0xE91E8C, "💋")

    @commands.command(name="selfhex")
    async def selfhex(self, ctx):
        """Cast a hex on yourself — for the brave and unhinged."""
        name, effect = random.choice(HEXES)
        em = discord.Embed(
            title       = f"🖤 Self-Hex: {name}",
            description = f"*{ctx.author.display_name} hexes themselves. Luna watches with fascination.*\n\n{effect}",
            color       = 0x2C3E50,
        )
        em.set_footer(text="You didn't need her help with that. 🌙")
        await ctx.send(embed=em)

    @commands.command(name="spellbook")
    async def spellbook(self, ctx):
        """Show all available spell types."""
        em = discord.Embed(
            title       = "📖 Luna's Spellbook",
            description = "*She opens the ancient tome, her fingers tracing the ink...*",
            color       = config.BOT_COLOR,
        )
        em.add_field(name=f"✨ `{config.PREFIX}spell @user`",  value="Mystical effect — unpredictable", inline=True)
        em.add_field(name=f"🖤 `{config.PREFIX}hex @user`",    value="Bad luck curse — petty chaos",   inline=True)
        em.add_field(name=f"🌟 `{config.PREFIX}bless @user`",  value="Good fortune — warmth & grace",  inline=True)
        em.add_field(name=f"💋 `{config.PREFIX}charm @user`",  value="Allure charm — magnetic energy", inline=True)
        em.add_field(name=f"🖤 `{config.PREFIX}selfhex`",       value="Hex yourself — you unhinged legend", inline=True)
        em.set_footer(text=f"Cooldown: {SPELL_COOLDOWN}s per cast. 🌙")
        await ctx.send(embed=em)


async def setup(bot):
    await bot.add_cog(SpellsCog(bot))

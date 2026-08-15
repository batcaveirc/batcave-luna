"""Social commands — tea, confessions, truth/dare, ship, seduce."""

import json
import os
import random
import discord
from discord.ext import commands
import config

CONFESSIONS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "confessions.json")

# ── Content pools ─────────────────────────────────────────────────────────────

TEA = [
    "has been sliding into DMs with suspicious frequency lately. Just saying.",
    "pretends to be unbothered but checks the chat every five minutes.",
    "absolutely has a type and everyone here knows exactly what it is.",
    "said they were 'just friends' and we all collectively said nothing.",
    "stays up way too late in this server and I respect the dedication.",
    "is holding feelings they haven't said out loud yet. The stars see it.",
    "is the reason the vibe in this server is always oddly charged.",
    "has a secret soft side that only appears after midnight.",
    "sends typing... for 3 minutes then just says 'lol'.",
    "is way more interesting than they let on. Suspicious.",
]

TRUTHS = [
    "What's a crush you've never admitted to anyone here?",
    "What's the most embarrassing thing you've done for attention?",
    "Have you ever lied in this server? About what?",
    "Who here do you find most attractive, and why?",
    "What's a secret you've been carrying that the server would be shocked by?",
    "What's your most unhinged 3am thought this week?",
    "Who in this server do you trust the most? And the least?",
    "What's something you'd never do... that you've already done?",
    "What's the most chaotic decision you've made this month?",
    "If you could confess one thing anonymously, what would it be?",
]

DARES = [
    "Send your most unhinged message and delete it after 60 seconds.",
    "Compliment someone in the server in the most dramatic way possible.",
    "Write a 2-sentence love letter to the last person who messaged you.",
    "Change your nickname to something Luna chooses for the next hour.",
    "Send the first line of the last thing you typed but didn't send.",
    "Describe your current mood using only a villain quote.",
    "Confess something in the confessions channel or DM it to Luna.",
    "React to the last 5 messages with increasingly unhinged emojis.",
    "Write your own horoscope for today. Make it dramatic.",
    "Send an anonymous compliment to someone using !!confess.",
]

NSFW_TRUTHS = [
    "What's your most forbidden fantasy you haven't told anyone?",
    "What's the boldest move you've ever made on someone?",
    "Describe your ideal night without using any boring words.",
    "What's something you'd do with the right person that surprises even yourself?",
    "Who here gives you that specific type of energy and what would you do about it?",
]

NSFW_DARES = [
    "Describe your ideal evening with someone here in exactly 3 sentences.",
    "Write the most seductive opening line you can and send it to the void.",
    "Confess your most adult desire in !!confess — anonymous, no consequences.",
    "Describe someone in this room using only adjectives you'd whisper.",
    "Send the most charged message you'd normally delete before sending.",
]

SEDUCE_LINES = [
    "I'd say 'play your cards right' but darling, you already have the best hand.",
    "The moon told me about you. I told the moon to mind its business. I was wrong.",
    "You have the kind of energy that makes quiet rooms feel electric.",
    "Stay. Or don't. But you'll think about this conversation either way.",
    "I've read a thousand futures in these cards. Yours is... interesting. In the best way.",
    "The stars said something about you tonight. I'm choosing not to repeat it. Yet.",
    "You walk in and the whole vibe shifts. Don't pretend you don't notice.",
    "Something about you makes even the darkness lean in closer.",
]

COMPATIBILITY = [
    (95, "Dangerous levels of chemistry. The universe is watching you two very closely."),
    (88, "Deeply compatible. Finish each other's sentences. Probably already do."),
    (82, "Strong connection with electric tension underneath. Interesting."),
    (74, "Good compatibility. Will absolutely argue and enjoy it."),
    (66, "Works well together when the stars align. Chaotic neutral pairing."),
    (55, "Complicated. The push-pull is real. Exhausting. Fascinating."),
    (43, "Technically incompatible. Will definitely try anyway."),
    (31, "Disaster compatibility. Absolute chaos. 10/10 would make a great story."),
    (18, "Opposites in every way. Which means either nothing or everything."),
]


def _load_confessions() -> list:
    try:
        with open(CONFESSIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_confession(text: str, author_id: int):
    data = _load_confessions()
    data.append({"text": text, "author_id": author_id})
    os.makedirs(os.path.dirname(CONFESSIONS_FILE), exist_ok=True)
    with open(CONFESSIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


class SocialCog(commands.Cog, name="Social"):
    """Tea, confessions, truth/dare, ship, and seduce commands."""

    def __init__(self, bot):
        self.bot = bot

    def _is_nsfw(self, ctx) -> bool:
        return isinstance(ctx.channel, discord.TextChannel) and ctx.channel.is_nsfw()

    # ── ~tea ──────────────────────────────────────────────────────────────

    @commands.command(name="tea", aliases=["gossip", "spill"])
    async def tea(self, ctx, target: discord.Member = None):
        """Spill the tea on someone. ~tea @user"""
        if not target:
            await ctx.send("*Luna leans forward.* Spill on who, darling? `!!tea @user`")
            return

        line = random.choice(TEA)
        await ctx.send(
            f"☕ *Luna sets down her cup and fixes you with a knowing look...*\n\n"
            f"**{target.display_name}** {line}"
        )

    # ── ~confess ──────────────────────────────────────────────────────────

    @commands.command(name="confess", aliases=["secret"])
    async def confess(self, ctx, *, message: str = ""):
        """Send an anonymous confession. ~confess <your secret>"""
        if not message:
            await ctx.send("*Luna opens the confession book.* Tell me. Nobody will know it was you. `!!confess <secret>`")
            return

        # Delete the command message to preserve anonymity
        try:
            await ctx.message.delete()
        except Exception:
            pass

        _save_confession(message, ctx.author.id)

        em = discord.Embed(
            title       = "🕯️ Anonymous Confession",
            description = f"*A soul steps forward in the dark...*\n\n\"{message[:800]}\"",
            color       = 0x2C3E50,
        )
        em.set_footer(text="Identity known only to the moon. 🌙")

        # Find confessions/general channel or post in current
        channel = (
            discord.utils.get(ctx.guild.text_channels, name="confessions")
            or discord.utils.get(ctx.guild.text_channels, name="general")
            or ctx.channel
        )
        await channel.send(embed=em)

    # ── ~truth ────────────────────────────────────────────────────────────

    @commands.command(name="truth")
    async def truth(self, ctx):
        """Get a truth question. NSFW channel gets spicier questions."""
        if self._is_nsfw(ctx):
            question = random.choice(NSFW_TRUTHS + TRUTHS)
        else:
            question = random.choice(TRUTHS)

        em = discord.Embed(
            title       = "🌑 Truth",
            description = f"*Luna points at you with a slow smile...*\n\n**{question}**",
            color       = config.BOT_COLOR,
        )
        em.set_footer(text="Answer or face a hex. Your choice. 🌙")
        await ctx.send(embed=em)

    # ── ~dare ─────────────────────────────────────────────────────────────

    @commands.command(name="dare")
    async def dare(self, ctx):
        """Get a dare. NSFW channel gets wilder dares."""
        if self._is_nsfw(ctx):
            dare = random.choice(NSFW_DARES + DARES)
        else:
            dare = random.choice(DARES)

        em = discord.Embed(
            title       = "🔥 Dare",
            description = f"*Luna smirks, already knowing your answer...*\n\n**{dare}**",
            color       = 0xE91E8C,
        )
        em.set_footer(text="The brave do it. The rest get hexed. 🌙")
        await ctx.send(embed=em)

    # ── ~ship ─────────────────────────────────────────────────────────────

    @commands.command(name="ship", aliases=["match", "compat"])
    async def ship(self, ctx, user1: discord.Member = None, user2: discord.Member = None):
        """Check compatibility between two people. ~ship @user1 @user2"""
        if not user1 or not user2:
            await ctx.send("*Luna opens the compatibility tome.* `!!ship @user1 @user2`")
            return

        seed    = abs(hash(f"{user1.id}{user2.id}")) % len(COMPATIBILITY)
        score, reading = COMPATIBILITY[seed]

        bar_filled = int(score / 10)
        bar = "💜" * bar_filled + "🖤" * (10 - bar_filled)

        em = discord.Embed(
            title       = f"💜 {user1.display_name} & {user2.display_name}",
            description = f"*Luna draws a circle around both their names...*",
            color       = config.BOT_COLOR,
        )
        em.add_field(name="Compatibility",  value=f"**{score}%**",   inline=True)
        em.add_field(name="Reading",        value=reading,            inline=False)
        em.add_field(name="Energy",         value=bar,               inline=False)
        em.set_footer(text="The stars have spoken. Whether you listen is your business. 🌙")
        await ctx.send(embed=em)

    # ── ~seduce ───────────────────────────────────────────────────────────

    @commands.command(name="seduce", aliases=["flirt"])
    async def seduce(self, ctx, target: discord.Member = None):
        """Luna seduces someone on your behalf. ~seduce @user"""
        if not target:
            await ctx.send("*Luna raises an eyebrow.* On whose behalf am I working? `!!seduce @user`")
            return
        if target == ctx.author:
            await ctx.send("*Luna stares.* You want me to seduce you... for yourself. Darling. The audacity. I respect it.")
            return

        line = random.choice(SEDUCE_LINES)
        await ctx.send(
            f"*Luna glides over to {target.mention} and leans in...*\n\n"
            f"**\"{line}\"**\n\n"
            f"*She steps back and glances at {ctx.author.display_name} with a slow smile.* "
            f"You're welcome."
        )

    # ── ~tod ──────────────────────────────────────────────────────────────

    @commands.command(name="tod", aliases=["truthordare"])
    async def tod(self, ctx):
        """Random truth OR dare."""
        if random.random() < 0.5:
            await self.truth(ctx)
        else:
            await self.dare(ctx)

    # ── ~vibe ─────────────────────────────────────────────────────────────

    @commands.command(name="vibe", aliases=["vibecheck"])
    async def vibe(self, ctx):
        """Luna checks the current room vibe."""
        vibes = [
            ("🌑", "Dark and electric. Something is about to happen in this room."),
            ("🌙", "Mysteriously calm. Too calm. Someone is planning something."),
            ("✨", "Chaotic glitter energy. Unhinged but make it glamorous."),
            ("🔥", "Tension levels: concerning. In the best possible way."),
            ("💜", "Soft and charged. People are feeling things they haven't said."),
            ("🖤", "Melancholic with an edge. Perfect for confessions and bad decisions."),
            ("⚡", "Electric. Everyone is one message away from saying something unhinged."),
            ("🌹", "Romantic energy detected. Someone is thinking about someone else."),
        ]
        emoji, reading = random.choice(vibes)
        await ctx.send(
            f"{emoji} *Luna closes her eyes, fingers pressed to her temple...*\n\n"
            f"**Current vibe: {reading}**"
        )


async def setup(bot):
    await bot.add_cog(SocialCog(bot))

"""Dark Economy — Soul Shards, Covens, Potions, Sacrifices."""

import json
import os
import random
import time
import discord
from discord.ext import commands
import config

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "shards.json")

POTIONS = {
    "shadowveil":  {"cost": 50,  "desc": "Become untargetable by spells for 30 minutes."},
    "bloodrose":   {"cost": 80,  "desc": "Double shard gains for 1 hour."},
    "moonwater":   {"cost": 30,  "desc": "Heal a hex placed on you."},
    "nightshade":  {"cost": 120, "desc": "Your next hex deals double effect."},
    "starfire":    {"cost": 60,  "desc": "Boost your charm spell power for 2 hours."},
}

COVENS = ["The Shadow Circle", "Daughters of the Moon", "The Crimson Coven",
          "Order of the Veil", "The Obsidian Court"]

RITUAL_QUOTES = [
    "The moon accepts your offering, darling.",
    "Darkness rewards the devoted.",
    "Your soul grows richer — in every sense.",
    "The ritual is complete. The veil thins for you.",
    "Luna smiles. The shards are yours.",
]


def _load() -> dict:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _user(data: dict, uid: str) -> dict:
    data.setdefault(uid, {
        "shards":         0,
        "last_ritual":    0,
        "coven":          None,
        "potions":        {},
        "total_earned":   0,
    })
    return data[uid]


class EconomyCog(commands.Cog, name="Dark Economy"):
    """Soul shards, covens, potions, sacrifices."""

    def __init__(self, bot):
        self.bot = bot

    # ── ~ritual ───────────────────────────────────────────────────────────

    @commands.command(name="ritual", aliases=["daily", "pray"])
    async def ritual(self, ctx):
        """Perform your daily ritual to earn Soul Shards."""
        data = _load()
        uid  = str(ctx.author.id)
        user = _user(data, uid)

        now     = time.time()
        elapsed = now - user["last_ritual"]
        cooldown = 86400   # 24 hours

        if elapsed < cooldown:
            remaining = cooldown - elapsed
            h, m = divmod(int(remaining), 3600)
            m, s = divmod(m, 60)
            await ctx.send(
                f"*Luna shakes her head.* The ritual requires patience. "
                f"Return in `{h}h {m}m {s}s`, darling."
            )
            return

        gained = random.randint(20, 60)
        # Coven bonus
        if user["coven"]:
            gained = int(gained * 1.15)

        user["shards"]       += gained
        user["last_ritual"]   = now
        user["total_earned"] += gained
        _save(data)

        em = discord.Embed(
            title       = "🕯️ Ritual Complete",
            description = f"*{random.choice(RITUAL_QUOTES)}*\n\n"
                          f"You earned **{gained} Soul Shards** 💜\n"
                          f"Total: **{user['shards']:,}** shards",
            color       = config.BOT_COLOR,
        )
        if user["coven"]:
            em.set_footer(text=f"Coven bonus applied • {user['coven']} 🌙")
        else:
            em.set_footer(text="Join a coven for +15% bonus shards. 🌙")
        await ctx.send(embed=em)

    # ── ~shards ───────────────────────────────────────────────────────────

    @commands.command(name="shards", aliases=["balance", "soul", "bal"])
    async def shards(self, ctx, member: discord.Member = None):
        """Check your Soul Shard balance."""
        target = member or ctx.author
        data   = _load()
        user   = _user(data, str(target.id))

        em = discord.Embed(
            title       = f"💜 {target.display_name}'s Soul",
            color       = config.BOT_COLOR,
        )
        em.add_field(name="Soul Shards",   value=f"{user['shards']:,}", inline=True)
        em.add_field(name="Total Earned",  value=f"{user['total_earned']:,}", inline=True)
        em.add_field(name="Coven",         value=user["coven"] or "None", inline=True)
        em.set_footer(text="Earn shards with ~ritual • Spend with ~brew 🌙")
        await ctx.send(embed=em)

    # ── ~sacrifice ────────────────────────────────────────────────────────

    @commands.command(name="sacrifice", aliases=["steal", "rob"])
    async def sacrifice(self, ctx, target: discord.Member = None):
        """Attempt to steal Soul Shards from another."""
        if not target or target == ctx.author:
            await ctx.send("*Luna raises an eyebrow.* Sacrifice who, exactly? `!!sacrifice @user`")
            return

        data     = _load()
        attacker = _user(data, str(ctx.author.id))
        victim   = _user(data, str(target.id))

        cost = 10   # sacrifice costs attacker some shards too
        if attacker["shards"] < cost:
            await ctx.send(f"*Luna clicks her tongue.* You need at least {cost} shards to perform a sacrifice.")
            return

        success = random.random() < 0.45   # 45% success rate
        if success:
            stolen = random.randint(5, min(30, max(1, victim["shards"] // 5)))
            attacker["shards"] += stolen
            victim["shards"]    = max(0, victim["shards"] - stolen)
            attacker["shards"] -= cost
            _save(data)
            em = discord.Embed(
                title       = "⚔️ Sacrifice Successful",
                description = (
                    f"*{ctx.author.display_name} reaches into the shadow and takes from {target.display_name}...*\n\n"
                    f"**{stolen} Soul Shards** were seized.\n"
                    f"Cost to you: {cost} shards."
                ),
                color = 0x8B0000,
            )
        else:
            attacker["shards"] = max(0, attacker["shards"] - cost)
            _save(data)
            em = discord.Embed(
                title       = "💨 Sacrifice Failed",
                description = (
                    f"*{target.display_name} sensed the dark energy and deflected it.*\n\n"
                    f"You lost {cost} shards for the attempt."
                ),
                color = 0x2C3E50,
            )
        await ctx.send(embed=em)

    # ── ~coven ────────────────────────────────────────────────────────────

    @commands.command(name="coven")
    async def coven(self, ctx, action: str = "info"):
        """Join, leave, or check your coven. ~coven join | leave | info"""
        data = _load()
        uid  = str(ctx.author.id)
        user = _user(data, uid)
        action = action.lower()

        if action == "info":
            em = discord.Embed(
                title       = "🕸️ The Covens",
                description = "Join a coven for +15% ritual shard bonus.\nAll covens have equal power — choose your aesthetic.",
                color       = config.BOT_COLOR,
            )
            for c in COVENS:
                members = sum(1 for u in data.values() if isinstance(u, dict) and u.get("coven") == c)
                em.add_field(name=c, value=f"{members} members", inline=True)
            em.set_footer(text="!!coven join <name> | !!coven leave 🌙")
            await ctx.send(embed=em)
            return

        if action == "leave":
            if not user["coven"]:
                await ctx.send("*Luna shrugs.* You're not in a coven.")
                return
            old = user["coven"]
            user["coven"] = None
            _save(data)
            await ctx.send(f"*You fade from the {old}. The shadows close behind you.*")
            return

        if action == "join":
            await ctx.send(
                f"*Luna gestures to the circles.* Which coven calls to you?\n"
                + "\n".join(f"• **{c}**" for c in COVENS)
                + "\n\n`!!coven join <name>`"
            )
            return

        # Check if it matches a coven name
        match = next((c for c in COVENS if action in c.lower()), None)
        if not match:
            # Try full name from args
            full = ctx.message.content.split("join", 1)[-1].strip() if "join" in ctx.message.content else ""
            match = next((c for c in COVENS if full.lower() in c.lower()), None)

        if match:
            user["coven"] = match
            _save(data)
            em = discord.Embed(
                title       = f"🕸️ Welcome to {match}",
                description = f"*{ctx.author.display_name} steps into the circle. The candles flicker in welcome.*\n\nYou now receive **+15% ritual bonus**.",
                color       = config.BOT_COLOR,
            )
            await ctx.send(embed=em)
        else:
            await ctx.send("*Luna frowns.* That coven doesn't exist. Use `!!coven info` to see the list.")

    # ── ~brew ─────────────────────────────────────────────────────────────

    @commands.command(name="brew", aliases=["potion", "craft"])
    async def brew(self, ctx, potion: str = ""):
        """Brew a potion with Soul Shards. ~brew <name> or ~brew list"""
        potion = potion.lower()
        if not potion or potion == "list":
            em = discord.Embed(
                title       = "🧪 Luna's Potion Cabinet",
                description = "*She slides the cabinet open, vials gleaming darkly...*",
                color       = config.BOT_COLOR,
            )
            for name, info in POTIONS.items():
                em.add_field(
                    name  = f"`{name}` — {info['cost']} shards",
                    value = info["desc"],
                    inline=False,
                )
            em.set_footer(text="!!brew <name> to craft 🌙")
            await ctx.send(embed=em)
            return

        if potion not in POTIONS:
            await ctx.send(f"*Luna shakes her head.* No such potion. Try `!!brew list`.")
            return

        data   = _load()
        uid    = str(ctx.author.id)
        user   = _user(data, uid)
        info   = POTIONS[potion]
        cost   = info["cost"]

        if user["shards"] < cost:
            await ctx.send(
                f"*Luna clucks her tongue.* You need **{cost} shards** for that. "
                f"You have **{user['shards']}**. Perform more rituals, darling."
            )
            return

        user["shards"] -= cost
        user["potions"][potion] = user["potions"].get(potion, 0) + 1
        _save(data)

        em = discord.Embed(
            title       = f"🧪 {potion.title()} Brewed",
            description = f"*{ctx.author.display_name} adds the final ingredient. The vial glows...*\n\n{info['desc']}",
            color       = config.BOT_COLOR,
        )
        em.add_field(name="Cost",      value=f"{cost} shards", inline=True)
        em.add_field(name="Remaining", value=f"{user['shards']:,} shards", inline=True)
        em.set_footer(text="Luna nods approvingly. 🌙")
        await ctx.send(embed=em)

    # ── ~richest ──────────────────────────────────────────────────────────

    @commands.command(name="richest", aliases=["topsouls", "lb"])
    async def richest(self, ctx):
        """Leaderboard of richest souls."""
        data    = _load()
        guild   = ctx.guild
        members = {str(m.id): m.display_name for m in guild.members}
        ranked  = sorted(
            [(members[uid], u["shards"]) for uid, u in data.items()
             if uid in members and isinstance(u, dict)],
            key=lambda x: x[1], reverse=True
        )[:10]

        if not ranked:
            await ctx.send("*Luna gazes into the void.* No souls have gathered shards yet.")
            return

        em = discord.Embed(
            title       = "💀 Richest Souls",
            description = "\n".join(
                f"**{i+1}.** {name} — {shards:,} shards"
                for i, (name, shards) in enumerate(ranked)
            ),
            color = config.BOT_COLOR,
        )
        em.set_footer(text="Earn shards with ~ritual 🌙")
        await ctx.send(embed=em)


async def setup(bot):
    await bot.add_cog(EconomyCog(bot))

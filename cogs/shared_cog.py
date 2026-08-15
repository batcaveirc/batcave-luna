"""
Discord wrapper for shared_cmds.py — exposes the cross-platform
commands as `!!s <cmd>` group to avoid colliding with existing Luna
commands like !!ping, !!help, !!batstatus.

Example: !!s ping, !!s roll 2d6, !!s 8ball will I finish my project?
"""

from discord.ext import commands

from shared_cmds import SharedCommands, is_discord_authorized


class SharedCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bridge = getattr(bot, "_irc_bridge", None)
        self.shared = SharedCommands.get(bot=bot, bridge=bridge)

    @commands.group(
        name="s",
        invoke_without_command=True,
        help="Shared cross-platform commands (works in IRC and Discord).",
    )
    async def s_group(self, ctx, *, query: str = ""):
        if not is_discord_authorized(ctx.author.id):
            await ctx.send("*Luna narrows her eyes.* Not for you, darling.")
            return

        query = (query or "").strip()
        if not query:
            await ctx.send(
                "Usage: `!!s <cmd>`\n"
                "Commands: ping, help, 8ball, roll, calc, fact, dadjoke, "
                "quote, choose, flip, weather, nicks, say, remind, batstatus\n"
                "Example: `!!s roll 2d6`"
            )
            return

        parts = query.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        reply = self.shared.dispatch_discord(
            ctx.author.id,
            ctx.author.display_name,
            cmd,
            args,
        )
        if reply:
            await ctx.send(reply[:1900])


async def setup(bot):
    await bot.add_cog(SharedCog(bot))

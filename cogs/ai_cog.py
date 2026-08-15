import discord
from discord.ext import commands
import requests
import json

class AICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ai")
    async def ai_command(self, ctx, *, prompt: str):
        """Ask your local Ollama model"""
        await ctx.send("🔍 Thinking... (using your M2 Pro GPU)")
        
        try:
            response = requests.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": "qwen2.5:7b",
                    "prompt": f"Explain like I'm 5: {prompt}",
                    "stream": False,
                    "options": {"num_gpu": 50}
                },
                timeout=30
            )
            response.raise_for_status()
            result = json.loads(response.text)["response"].strip()
            await ctx.send(f"🧠 **Qwen2.5 says:**\n{result}")
        except Exception as e:
            await ctx.send(f"❌ Ollama error: {str(e)}\n\nTry: `!ai test`")

async def setup(bot):
    await bot.add_cog(AICog(bot))

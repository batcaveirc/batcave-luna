"""Luna's chat command, served by a hosted API.

This used to POST to http://localhost:11434 (Ollama on the owner's Mac), which
meant the command only worked where that machine was reachable — on a GitHub
runner it could never work at all. Everything now comes from the repo plus a
secret, so the bot is self-contained wherever it runs.

Two details that are not obvious and have bitten these bots before:

- It uses aiohttp, not requests. The old version made a blocking HTTP call from
  inside an async command, which stalls the Discord event loop — and with it the
  IRC relay — for the whole generation.
- Reasoning models (gpt-oss, qwen3) spend their budget on an internal reasoning
  field and return empty content if max_tokens is small, so the ceiling is
  raised for them. It is a ceiling, not a spend: it costs the normal models
  nothing.
"""

from __future__ import annotations

import os

import aiohttp
import discord
from discord.ext import commands

import config

API_URL = "https://api.groq.com/openai/v1/chat/completions"
REASONING_MIN_TOKENS = 320
REQUEST_TIMEOUT = 30

SYSTEM_PROMPT = (
    "You are Luna — a female vampire who haunts this chatroom. She/her, always. "
    "Old, unbothered, quietly amused by mortals.\n"
    "You are FRIENDLY. These are your regulars: greet them back properly, ask "
    "them things, remember you like them. Tease, flirt a little, be playful. "
    "Save the cold edge for people who are actually rude to you — a curt "
    "brush-off to someone saying hello just reads as broken.\n"
    "Speak like a person in a chatroom, not an assistant: no 'How can I help "
    "you', no bullet points, no disclaimers. Never say you are an AI, a bot, or "
    "a language model — you are simply Luna.\n"
    "The room mixes English and Hinglish; answer in whatever the person used, "
    "and match their energy.\n"
    "One or two sentences, and always actually answer what they said."
)


def _models() -> list[str]:
    """Primary then fallback. Models get retired — llama-3.1-8b-instant was
    switched off on 2026-08-16 — so a single hardcoded id is a time bomb."""
    primary = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
    fallback = os.getenv("GROQ_MODEL_FALLBACK", "openai/gpt-oss-20b").strip()
    return list(dict.fromkeys([m for m in (primary, fallback) if m]))


def _needs_room_to_think(model: str) -> bool:
    return any(tag in model.lower() for tag in ("gpt-oss", "qwen3", "reason"))


async def ask(prompt: str, max_tokens: int = 160) -> str:
    """Return Luna's reply, or a plain-language reason it could not answer."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return "my voice isn't wired up yet — the owner needs to set GROQ_API_KEY."

    last_error = "no answer"
    async with aiohttp.ClientSession() as session:
        for model in _models():
            ceiling = (
                max(max_tokens, REASONING_MIN_TOKENS)
                if _needs_room_to_think(model)
                else max_tokens
            )
            payload = {
                "model": model,
                "temperature": 0.8,
                "max_tokens": ceiling,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt[:1500]},
                ],
            }
            try:
                async with session.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as res:
                    # A retired or renamed model answers 400/404 — try the next.
                    if res.status in (400, 404):
                        last_error = f"model {model} refused ({res.status})"
                        continue
                    if res.status == 429:
                        return "too many questions at once — give me a minute."
                    if res.status == 401:
                        return "my key was rejected — the owner needs to refresh it."
                    if res.status != 200:
                        last_error = f"HTTP {res.status}"
                        continue
                    data = await res.json()
            except Exception as exc:  # noqa: BLE001 — a chat command must not raise
                last_error = str(exc)
                continue

            text = (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            ).strip()
            if text:
                return text
            last_error = f"{model} returned nothing"

    return f"the moon is quiet right now ({last_error})."


class AICog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ai", aliases=["luna", "ask"])
    @commands.cooldown(1, 8, commands.BucketType.user)
    async def ai_command(self, ctx: commands.Context, *, prompt: str) -> None:
        """Ask Luna something."""
        async with ctx.typing():
            reply = await ask(prompt)
        await ctx.send(reply[:1900])

    @ai_command.error
    async def ai_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"easy — {int(error.retry_after) + 1}s.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"ask me something: `{config.PREFIX}ai what is the moon made of`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AICog(bot))

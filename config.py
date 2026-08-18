"""Luna Bot — Configuration. All secrets via environment variables."""
import os
from pathlib import Path

# Auto-load .env from same directory — works when run directly or via bot manager
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            if _k not in os.environ:   # don't override vars already in env
                os.environ[_k] = _v.strip().strip("'\"")

# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN", "")
try:
    DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
except ValueError:
    print("[config] WARNING: DISCORD_GUILD_ID is not a number — set it to your server ID in Secrets. Defaulting to 0.")
    DISCORD_GUILD_ID = 0
BRIDGE_CHANNEL     = os.getenv("BRIDGE_CHANNEL", "batcave")   # Discord channel name to bridge
NSFW_CHANNEL       = os.getenv("NSFW_CHANNEL",   "nsfw")      # NSFW channel name

# Owner Discord user IDs (comma-separated) — can use owner-only commands
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}

# ── IRC Bridge ────────────────────────────────────────────────────────────────
# Hosted AI for the !!ai command. No localhost anywhere: the bot must run
# unchanged on any host, so the key is a secret and the model an env var.
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL          = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACK = os.getenv("GROQ_MODEL_FALLBACK", "openai/gpt-oss-20b")

IRC_SERVER        = os.getenv("IRC_SERVER",   "irc.hybridirc.com")
IRC_PORT          = int(os.getenv("IRC_PORT", "6697"))
IRC_SSL           = True
IRC_NICK          = os.getenv("IRC_NICK",          "Luna")
IRC_NICKSERV_PASS = os.getenv("IRC_NICKSERV_PASS", "")
IRC_CHANNEL       = os.getenv("IRC_CHANNEL",       "#BatCave")
IRC_REALNAME      = os.getenv("IRC_REALNAME", "Keeping the night company")

# ── AI ────────────────────────────────────────────────────────────────────────

# ── BatBot monitoring ────────────────────────────────────────────────────────
# Optional health URL for the Vampire bot. Unset = $batstatus reports the
# IRC side only, which is the useful half anyway.
BATBOT_REPLIT_URL     = os.getenv("BATBOT_REPLIT_URL", "")
# Optional link shown in Discord when the Vampire bot looks down.
BATBOT_REPLIT_PROJECT = os.getenv("BATBOT_REPLIT_PROJECT", "")
# BatBot's IRC nick to watch for in #BatCave
BATBOT_IRC_NICK       = os.getenv("BATBOT_IRC_NICK", "Vampire")
# Discord channel name for bot-status alerts
ALERT_CHANNEL         = os.getenv("ALERT_CHANNEL", "bot-logs")
# Discord role name required to use mod commands (also accepts users with kick/ban perm)
MOD_ROLE              = os.getenv("MOD_ROLE", "Moderator")

# ── Bot identity ──────────────────────────────────────────────────────────────
# "$" is Luna's alone. "!" is the Vampire bot's and "!!" is Dracula's, so a
# shared prefix means two bots answering one line — or neither, if they both
# assume the other took it.
PREFIX    = os.getenv("LUNA_PREFIX", "$")
BOT_NAME  = "Luna"
BOT_COLOR = 0x9B59B6   # Purple — Luna's signature colour

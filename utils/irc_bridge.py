"""
IRC Bridge — connects Luna to IRC.
Multi-channel: maps any Discord channel ↔ any IRC channel, N pairs.
Runs in a background thread.  Messages flow both ways:
  IRC → Discord  and  Discord → IRC

Discord commands (~prefix) are suppressed from IRC relay.
"""

import asyncio
import os
import re
import socket
import ssl
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

import config
from utils.relay_state import (
    RELAY_TO_DISCORD,
    RELAY_TO_STARALIGN,
    relay_state,
)
from utils.staralign_relay import relay_to_staralign

_RECONNECT_DELAY_MIN = 15    # initial reconnect delay (seconds)
_RECONNECT_DELAY_MAX = 120   # cap for exponential backoff
_SOCKET_TIMEOUT      = 30    # detect dead connections fast
_SEND_DELAY          = 0.5   # seconds between outbound IRC messages (rate-limit)

# ── Notsobot command map: IRC trigger → Discord command prefix ────────────────
# IRC user types: !img search cats   → Luna sends: .img search cats  to Discord
# Notsobot replies in Discord → Luna relays the image URL back to IRC
# IRC user types: .img cats  → Luna sends ".img cats" into the bridged Discord
# channel → notsobot answers there → Luna relays the image URL back to IRC.
#
# "." is the primary prefix because it is what notsobot actually uses, so the
# command reads the same in both rooms. "!" stays as an alias for muscle
# memory. Neither collides with the other bots in these channels (Vampire uses
# "!" with its own verbs, Dracula uses "!!").
_NOTSOBOT_PREFIXES = (".", "!")

# ALLOWLIST, and it must stay one. Without it, anyone in the IRC room could
# make Luna type an arbitrary command at any Discord bot — ".ban", ".purge",
# ".settings" — using Luna's own Discord permissions. Everything here is an
# image/text toy that cannot change state.
_NOTSOBOT_DEFAULT_CMDS = frozenset({
    # search / fetch
    "img", "image", "gif", "ocr", "google", "youtube", "urban", "translate",
    # the classics
    "edit", "magik", "pixel", "triggered", "beautiful", "blurpify", "deepfry",
    "jpeg", "invert", "grayscale", "blur", "sharpen", "glitch", "explode",
    "implode", "swirl", "rotate", "flip", "flop", "resize", "caption", "meme",
    "ascii", "emojify", "wall", "tile", "mirror", "spin", "zoom", "circle",
    "distort", "fisheye", "legofy", "paint", "posterize", "sepia", "sketch",
    "solarize", "tint", "vaporwave", "wave", "wiggle", "rain", "snow",
})

# Verbs that must never be forwarded even if someone adds them to
# NOTSOBOT_EXTRA_CMDS by mistake. A denylist behind an allowlist is belt and
# braces, and the cost of being wrong here is somebody's Discord server.
_NOTSOBOT_FORBIDDEN = frozenset({
    "ban", "unban", "softban", "kick", "mute", "unmute", "purge", "prune",
    "clear", "delete", "remove", "role", "roles", "nick", "settings", "set",
    "prefix", "config", "admin", "owner", "eval", "exec", "shell", "sudo",
    "say", "echo", "dm", "pm", "invite", "leave", "shutdown", "restart",
})


def _allowed_notsobot_cmds() -> frozenset:
    """Allowlist plus anything the owner added via NOTSOBOT_EXTRA_CMDS, minus
    anything on the denylist. Env-extensible so a new notsobot toy does not
    need a code change."""
    extra = {
        c.strip().lower().lstrip(".!")
        for c in os.getenv("NOTSOBOT_EXTRA_CMDS", "").split(",")
        if c.strip()
    }
    return frozenset((_NOTSOBOT_DEFAULT_CMDS | extra) - _NOTSOBOT_FORBIDDEN)


_NOTSOBOT_PENDING_TTL = 60   # image edits are slow; 30s was timing out
_NOTSOBOT_COOLDOWN    = 8    # seconds between requests from one nick
_NOTSOBOT_MAX_PENDING = 4    # concurrent requests per channel


class IRCBridge:
    """Thread-safe multi-channel IRC client that bridges to Discord."""

    def __init__(self, bot):
        self.bot   = bot
        self.loop  = None
        self._sock = None

        self._running       = False
        self._thread        = None
        self._sender_thread = None
        self._connected     = False
        self._force_reconnect = False
        self._last_ping     = time.time()

        # ── Channel mappings ─────────────────────────────────────────────────
        # discord_channel_name.lower() → irc_channel  (e.g. "batcave" → "#BatCave")
        self._d2i: Dict[str, str] = {}
        # irc_channel.lower()         → discord_channel_name
        self._i2d: Dict[str, str] = {}
        self._map_lock = threading.Lock()

        # Seed default bridge from config
        _d_def = getattr(config, "BRIDGE_CHANNEL", "").lower()
        _i_def = getattr(config, "IRC_CHANNEL",    "")
        if _d_def and _i_def:
            self._add_mapping(_d_def, _i_def)

        # ── Per-channel nick tracking ────────────────────────────────────────
        self._nicks: Dict[str, Set[str]] = {}   # irc_ch.lower() → set of nicks
        self._nicks_lock = threading.Lock()

        # ── Topic cache ──────────────────────────────────────────────────────
        self._topics: Dict[str, str] = {}
        self._topics_lock = threading.Lock()

        # ── Outbound send queue ──────────────────────────────────────────────
        self._send_q: deque = deque()            # (irc_channel, text)
        self._send_lock = threading.Lock()

        # ── Notsobot pending requests ────────────────────────────────────────
        # A DEQUE per channel, not one slot: two people asking at once used to
        # mean the second request silently replaced the first, and whoever
        # asked first never got an answer.
        # disc_ch_lower → deque[{"irc_ch", "nick", "cmd", "ts"}]
        self._pending_notsobot: Dict[str, deque] = {}
        self._notsobot_lock = threading.Lock()
        self._notsobot_cooldown: Dict[str, float] = {}   # nick(lower) → ts

        # ── Brawl game hook ───────────────────────────────────────────────────
        # Set by luna.py after BrawlGame is created so IRC !! commands reach it.
        self._brawl_game = None

    # ── Mapping helpers ───────────────────────────────────────────────────────

    def _add_mapping(self, discord_ch: str, irc_ch: str):
        d = discord_ch.lower()
        i = irc_ch if irc_ch.startswith("#") else f"#{irc_ch}"
        with self._map_lock:
            self._d2i[d] = i
            self._i2d[i.lower()] = d

    def _remove_mapping_by_irc(self, irc_ch: str):
        i = irc_ch.lower()
        with self._map_lock:
            disc = self._i2d.pop(i, None)
            if disc:
                self._d2i.pop(disc, None)

    def _default_irc_channel(self) -> str:
        with self._map_lock:
            vals = list(self._i2d.keys())
        return vals[0] if vals else getattr(config, "IRC_CHANNEL", "")

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the IRC bridge + sender threads. Call once from on_ready."""
        if self._thread is not None and self._thread.is_alive():
            print("[irc_bridge] start() called but thread already alive — ignored.")
            return
        self.loop     = loop
        self._running = True

        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()

        print("[irc_bridge] Threads started (IRC + sender).")

    # ── Channel bridge management ─────────────────────────────────────────────

    def join_channel(self, irc_channel: str, discord_channel: str) -> bool:
        """
        Add a Discord↔IRC bridge mapping and JOIN the IRC channel.
        Returns False if the mapping already exists unchanged.
        """
        irc_ch  = irc_channel if irc_channel.startswith("#") else f"#{irc_channel}"
        disc_ch = discord_channel.lower()
        with self._map_lock:
            existing = self._d2i.get(disc_ch)
        if existing and existing.lower() == irc_ch.lower():
            return False  # already bridged
        self._add_mapping(disc_ch, irc_ch)
        if self._connected:
            self._raw(f"JOIN {irc_ch}")
        return True

    def leave_channel(self, irc_channel: str) -> bool:
        """Remove bridge mapping and PART the IRC channel."""
        irc_ch = irc_channel if irc_channel.startswith("#") else f"#{irc_channel}"
        self._remove_mapping_by_irc(irc_ch)
        if self._connected:
            self._raw(f"PART {irc_ch} :Bridge removed")
        with self._nicks_lock:
            self._nicks.pop(irc_ch.lower(), None)
        return True

    def list_bridges(self) -> List[Tuple[str, str]]:
        """Return list of (discord_channel, irc_channel) pairs."""
        with self._map_lock:
            return list(self._d2i.items())

    def get_irc_for_discord(self, discord_channel: str) -> Optional[str]:
        """Return IRC channel mapped to this Discord channel, or None."""
        with self._map_lock:
            return self._d2i.get(discord_channel.lower())

    def get_discord_for_irc(self, irc_channel: str) -> Optional[str]:
        """Return Discord channel name mapped to this IRC channel, or None."""
        with self._map_lock:
            return self._i2d.get(irc_channel.lower())

    # ── Messaging ─────────────────────────────────────────────────────────────

    def send_to_irc(self, message: str, discord_channel: str = ""):
        """
        Queue a message to the IRC channel mapped to discord_channel.
        Falls back to the first mapped channel if discord_channel is unknown.
        """
        if not self._connected:
            return
        irc_ch = self.get_irc_for_discord(discord_channel) if discord_channel else None
        if not irc_ch:
            pairs  = self.list_bridges()
            irc_ch = pairs[0][1] if pairs else getattr(config, "IRC_CHANNEL", "")
        if irc_ch:
            self._queue(irc_ch, message[:400])

    def send_raw(self, cmd: str):
        """Send a raw IRC command. No-op if not connected."""
        if self._connected and self._sock:
            try:
                self._raw(cmd)
            except Exception as e:
                print(f"[irc_bridge] Raw send error: {e}")

    def kick_irc(self, nick: str, reason: str = "Kicked from Discord",
                 channel: str = "") -> bool:
        if not self._connected:
            return False
        irc_ch = channel or self._default_irc_channel()
        if irc_ch:
            self.send_raw(f"KICK {irc_ch} {nick} :{reason[:200]}")
        return True

    def ban_irc(self, nick: str, channel: str = "") -> bool:
        if not self._connected:
            return False
        irc_ch = channel or self._default_irc_channel()
        if irc_ch:
            self.send_raw(f"MODE {irc_ch} +b {nick}!*@*")
        return True

    # ── Nick / topic queries ──────────────────────────────────────────────────

    def is_nick_in_channel(self, nick: str, irc_channel: str = "") -> bool:
        ch = (irc_channel or self._default_irc_channel()).lower()
        with self._nicks_lock:
            return nick.lower() in {n.lower() for n in self._nicks.get(ch, set())}

    def get_channel_nicks(self, irc_channel: str = "") -> Set[str]:
        ch = (irc_channel or self._default_irc_channel()).lower()
        with self._nicks_lock:
            return set(self._nicks.get(ch, set()))

    def request_names(self, irc_channel: str = ""):
        if self._connected:
            ch = irc_channel or self._default_irc_channel()
            if ch:
                self._raw(f"NAMES {ch}")

    def is_connected(self) -> bool:
        return self._connected

    def change_nick(self, new_nick: str) -> bool:
        if not self._connected:
            return False
        self._raw(f"NICK {new_nick}")
        return True

    def get_topic(self, channel: str | None = None) -> str | None:
        ch = (channel or self._default_irc_channel()).lower()
        with self._topics_lock:
            return self._topics.get(ch)

    def reconnect(self):
        """Force-drop and re-establish the IRC connection."""
        self._force_reconnect = True
        self._connected       = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._raw("QUIT :Luna fades into the moonlight...")
                self._sock.close()
            except Exception:
                pass

    # ── Send queue ────────────────────────────────────────────────────────────

    def _queue(self, irc_channel: str, text: str):
        with self._send_lock:
            self._send_q.append((irc_channel, text))

    def _sender_loop(self):
        """Drain the send queue at _SEND_DELAY intervals (rate-limiting)."""
        last_sweep = 0.0
        while self._running:
            time.sleep(_SEND_DELAY)
            if not self._connected:
                continue
            # Piggyback the notsobot timeout check on the tick that already runs.
            if time.time() - last_sweep > 5:
                last_sweep = time.time()
                try:
                    self._sweep_notsobot_timeouts()
                except Exception as e:
                    print(f"[irc_bridge] notsobot sweep error: {e}")
            with self._send_lock:
                if not self._send_q:
                    continue
                irc_ch, text = self._send_q.popleft()
            try:
                self._raw(f"PRIVMSG {irc_ch} :{text}")
            except Exception as e:
                print(f"[irc_bridge] Sender error: {e}")

    # ── Reconnect loop ────────────────────────────────────────────────────────

    def _run_forever(self):
        delay = _RECONNECT_DELAY_MIN
        while self._running:
            self._force_reconnect = False
            try:
                self._connect_and_loop()
                delay = _RECONNECT_DELAY_MIN   # reset backoff on clean exit
            except Exception as e:
                print(f"[irc_bridge] Disconnected: {e}")
            if not self._running:
                break
            self._connected = False
            if self._force_reconnect:
                print("[irc_bridge] Force-reconnect — reconnecting immediately...")
                time.sleep(2)
            else:
                print(f"[irc_bridge] Reconnecting in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, _RECONNECT_DELAY_MAX)

    def _connect_and_loop(self):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(_SOCKET_TIMEOUT)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if config.IRC_SSL:
            ctx       = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw, server_hostname=config.IRC_SERVER)
        else:
            self._sock = raw

        print(f"[irc_bridge] Connecting to {config.IRC_SERVER}:{config.IRC_PORT}")
        self._sock.connect((config.IRC_SERVER, config.IRC_PORT))
        self._last_ping = time.time()
        self._raw(f"NICK {config.IRC_NICK}")
        self._raw(f"USER {config.IRC_NICK} 0 * :{config.IRC_REALNAME}")

        buf = ""
        while self._running and not self._force_reconnect:
            try:
                data = self._sock.recv(4096).decode("utf-8", errors="replace")
            except socket.timeout:
                if self._connected:
                    self._raw(f"PING :{config.IRC_SERVER}")
                continue
            if not data:
                break
            self._last_ping = time.time()
            buf += data
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                self._handle_line(line)

    # ── Line handler ──────────────────────────────────────────────────────────

    def _handle_line(self, line: str):
        # PING keepalive
        if line.startswith("PING"):
            self._raw("PONG " + line[5:])
            return

        # ERROR :Closing Link — server is booting us
        if line.startswith("ERROR"):
            print(f"[irc_bridge] Server error: {line}")
            raise ConnectionError(line)

        # 001 = registered
        if " 001 " in line:
            current_nick = config.IRC_NICK
            print(f"[irc_bridge] Registered as {current_nick}")
            if config.IRC_NICKSERV_PASS:
                self._raw(f"PRIVMSG NickServ :IDENTIFY {config.IRC_NICKSERV_PASS}")
                time.sleep(1)
                # Ghost any stale session holding our nick (from a previous crash)
                self._raw(f"PRIVMSG NickServ :GHOST {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
                time.sleep(0.5)
                # Reclaim our proper nick if we connected with a fallback (_)
                self._raw(f"NICK {config.IRC_NICK}")
                time.sleep(0.3)
            self._raw(f"MODE {config.IRC_NICK} +i")   # invisible — fewer unsolicited DMs
            # Re-join ALL mapped IRC channels
            with self._map_lock:
                channels = list(self._i2d.keys())
            for ch in channels:
                self._raw(f"JOIN {ch}")
            self._connected = True
            print(f"[irc_bridge] Connected and joined IRC channels.")
            return

        # 332 = topic on join
        m = re.match(r"^:\S+\s+332\s+\S+\s+(\S+)\s+:(.*)", line)
        if m:
            ch, topic = m.group(1).lower(), m.group(2)
            with self._topics_lock:
                self._topics[ch] = topic
            return

        # TOPIC change (live) — update cache only, no Discord announcement
        m = re.match(r"^:([^!]+)!\S+\s+TOPIC\s+(\S+)\s+:(.*)", line)
        if m:
            nick, ch, topic = m.group(1), m.group(2).lower(), m.group(3)
            with self._topics_lock:
                self._topics[ch] = topic
            return

        # Nick in use (433) — connect with temporary _ suffix, then ghost + reclaim
        if " 433 " in line:
            fallback = f"{config.IRC_NICK}_"
            print(f"[irc_bridge] Nick in use — using {fallback}, will GHOST after auth")
            self._raw(f"NICK {fallback}")
            return

        # 353 NAMREPLY — populate per-channel nick list
        m = re.match(r"^:\S+\s+353\s+\S+\s+[=@*]\s+(\S+)\s+:(.*)", line)
        if m:
            irc_ch   = m.group(1).lower()
            raw_nicks = m.group(2).split()
            cleaned  = {n.lstrip("@+%&~") for n in raw_nicks if n.lstrip("@+%&~")}
            with self._nicks_lock:
                self._nicks.setdefault(irc_ch, set()).update(cleaned)
            return

        # NICK change — update across all channels
        m = re.match(r"^:([^!]+)!\S+\s+NICK\s+:?(\S+)", line)
        if m:
            old_nick, new_nick = m.group(1), m.group(2)
            with self._nicks_lock:
                for ch_nicks in self._nicks.values():
                    if old_nick in ch_nicks:
                        ch_nicks.discard(old_nick)
                        ch_nicks.add(new_nick)
            return

        # PRIVMSG — channel or PM
        m = re.match(r"^:([^!]+)!\S+\s+PRIVMSG\s+(\S+)\s+:(.*)$", line)
        if m:
            nick    = m.group(1)
            target  = m.group(2)
            message = m.group(3).strip()

            # Ignore own messages
            if nick.lower() in (config.IRC_NICK.lower(), f"{config.IRC_NICK}_".lower()):
                return

            # ── Channel message ──
            if target.startswith("#"):
                # ── Brawl: dispatch !! commands regardless of Discord mapping ──
                if self._brawl_game is not None and message.startswith("!!"):
                    try:
                        self._brawl_game.handle_irc_message(nick, target, message)
                    except Exception as _bge:
                        print(f"[brawl] dispatch error: {_bge}")

                disc_ch = self.get_discord_for_irc(target)
                if disc_ch is None:
                    return   # not a bridged channel

                # ── Notsobot commands: intercept before normal relay ──────
                if self._try_notsobot_command(nick, target, message, disc_ch):
                    return  # handled — notsobot takes it from here

                # /me actions
                if message.startswith("\x01ACTION") and message.endswith("\x01"):
                    action = message[7:-1].strip()
                    if relay_state.is_enabled(RELAY_TO_DISCORD):
                        self._relay_to_discord(
                            f"*{nick} {action}*", discord_channel=disc_ch,
                        )
                        relay_state.stats.record_message()
                        relay_state.recent.append(
                            "to_discord", nick, f"*{action}*",
                        )
                    if relay_state.is_enabled(RELAY_TO_STARALIGN):
                        self._relay_to_staralign(nick, f"*{action}*")
                else:
                    if relay_state.is_enabled(RELAY_TO_DISCORD):
                        self._relay_to_discord(
                            f"**[Portal]** `{nick}`: {message}",
                            discord_channel=disc_ch,
                        )
                        relay_state.stats.record_message()
                        relay_state.recent.append(
                            "to_discord", nick, message[:200],
                        )
                    if relay_state.is_enabled(RELAY_TO_STARALIGN):
                        self._relay_to_staralign(nick, message)
                return

            # ── PM to Luna — ignored (relay-only bot) ──
            return

        # JOIN
        m = re.match(r"^:([^!]+)!\S+\s+JOIN\s+:?(\S+)", line)
        if m:
            nick    = m.group(1)
            channel = m.group(2)
            ch_low  = channel.lower()
            disc_ch = self.get_discord_for_irc(ch_low)
            with self._nicks_lock:
                self._nicks.setdefault(ch_low, set()).add(nick)
            if nick.lower() == config.IRC_NICK.lower():
                self._raw(f"NAMES {channel}")   # populate nick list on own join
            return

        # PART
        m = re.match(r"^:([^!]+)!\S+\s+PART\s+(\S+)", line)
        if m:
            nick    = m.group(1)
            channel = m.group(2)
            ch_low  = channel.lower()
            disc_ch = self.get_discord_for_irc(ch_low)
            with self._nicks_lock:
                self._nicks.get(ch_low, set()).discard(nick)
            return

        # QUIT
        m = re.match(r"^:([^!]+)!\S+\s+QUIT\s+:(.*)", line)
        if m:
            nick = m.group(1)
            with self._nicks_lock:
                for ch_nicks in self._nicks.values():
                    ch_nicks.discard(nick)
            # Nick tracking only — no announcement to Discord

    # ── Notsobot bridge ───────────────────────────────────────────────────────

    def _try_notsobot_command(self, nick: str, irc_ch: str,
                              message: str, disc_ch: str) -> bool:
        """
        If message is a notsobot trigger (!img, !edit, …), send the matching
        Discord command and register a pending reply.  Returns True if handled.
        """
        parts = message.strip().split()
        if not parts:
            return False
        raw = parts[0]
        if not raw.startswith(_NOTSOBOT_PREFIXES) or len(raw) < 2:
            return False
        verb = raw[1:].lower()

        # ".luna" lists what is available, so nobody has to guess.
        if verb in ("luna", "cmds", "commands"):
            allowed = sorted(_allowed_notsobot_cmds())
            self._queue(irc_ch,
                f"\x0313:discord:\x03 {nick}: try \x02.img cats\x02 or "
                f"\x02.magik <image url>\x02 — {len(allowed)} commands: "
                + ", ".join(allowed[:40]))
            return True

        if verb not in _allowed_notsobot_cmds():
            return False          # not ours — fall through to the normal relay

        now = time.time()
        nkey = nick.lower()

        with self._notsobot_lock:
            # Drop anything that timed out (the sweeper reports those).
            for ch, q in list(self._pending_notsobot.items()):
                while q and now - q[0]["ts"] > _NOTSOBOT_PENDING_TTL:
                    q.popleft()
                if not q:
                    del self._pending_notsobot[ch]

            last = self._notsobot_cooldown.get(nkey, 0.0)
            if now - last < _NOTSOBOT_COOLDOWN:
                wait = int(_NOTSOBOT_COOLDOWN - (now - last)) + 1
                self._queue(irc_ch, f"{nick}: easy — {wait}s.")
                return True

            q = self._pending_notsobot.setdefault(disc_ch.lower(), deque())
            if len(q) >= _NOTSOBOT_MAX_PENDING:
                self._queue(irc_ch, f"{nick}: too many in flight, try again shortly.")
                return True

            args = " ".join(parts[1:])
            discord_cmd = f".{verb} {args}".strip()
            q.append({
                "irc_ch": irc_ch,
                "nick":   nick,
                "cmd":    discord_cmd,
                "ts":     now,
            })
            self._notsobot_cooldown[nkey] = now

        # Announce to IRC immediately
        self._queue(irc_ch,
            f"\x0313:discord:\x03 {nick}: fetching \x02{discord_cmd}\x02 …")

        # Send the command into Discord so notsobot picks it up
        self._relay_to_discord(discord_cmd, discord_channel=disc_ch)
        print(f"[irc_bridge] notsobot → Discord [{disc_ch}]: {discord_cmd}")
        return True

    def _sweep_notsobot_timeouts(self) -> None:
        """Tell IRC when a request got no answer. Without this the room just
        sees 'fetching …' and nothing ever again, which reads as a dead bot."""
        now = time.time()
        expired: List[dict] = []
        with self._notsobot_lock:
            for ch, q in list(self._pending_notsobot.items()):
                while q and now - q[0]["ts"] > _NOTSOBOT_PENDING_TTL:
                    expired.append(q.popleft())
                if not q:
                    del self._pending_notsobot[ch]
        for p in expired:
            self._queue(p["irc_ch"],
                f"\x0313:discord:\x03 {p['nick']}: no answer for "
                f"\x02{p['cmd']}\x02 — it may need an image URL.")

    async def handle_notsobot_response(self, message) -> bool:
        """
        Called from luna.py on_message for every Discord message.
        If there's a pending notsobot request for that channel and the message
        looks like a notsobot image response, relay it back to IRC.
        Returns True if the message was handled as a notsobot reply.
        """
        # Only handle messages from bots (notsobot is a bot)
        if not message.author.bot:
            return False
        # Skip Luna's own echoes
        if self.bot.user and message.author.id == self.bot.user.id:
            return False

        disc_ch = getattr(message.channel, "name", "").lower()

        with self._notsobot_lock:
            q = self._pending_notsobot.get(disc_ch)
            while q and time.time() - q[0]["ts"] > _NOTSOBOT_PENDING_TTL:
                q.popleft()                       # the sweeper announces these
            if not q:
                self._pending_notsobot.pop(disc_ch, None)
                return False
            pending = q[0]

            # Determine if this message carries image content
            has_attachment = bool(message.attachments)
            has_image_embed = any(
                (e.image or e.thumbnail or e.url)
                for e in (message.embeds or [])
            )
            has_url_in_text = bool(message.content and
                ("http://" in message.content or "https://" in message.content))

            if not (has_attachment or has_image_embed or has_url_in_text):
                return False   # not the result yet — keep pending

            # Consume the oldest pending entry — answers come back in order.
            irc_ch   = pending["irc_ch"]
            req_nick = pending["nick"]
            q.popleft()
            if not q:
                self._pending_notsobot.pop(disc_ch, None)

        # Build the IRC relay text
        parts: List[str] = []
        for att in message.attachments:
            parts.append(att.url)
        for embed in message.embeds:
            if embed.image and embed.image.url:
                parts.append(embed.image.url)
            elif embed.thumbnail and embed.thumbnail.url:
                parts.append(embed.thumbnail.url)
            elif embed.url:
                parts.append(embed.url)
        if message.content:
            # Include text content too (some notsobot commands return text)
            content = message.content.strip()[:200]
            if content and content not in parts:
                parts.insert(0, content)

        if not parts:
            return False

        relay = f"\x0313:discord:\x03 {req_nick}: " + "  ".join(parts[:3])
        self._queue(irc_ch, relay[:450])
        print(f"[irc_bridge] notsobot response relayed to {irc_ch} for {req_nick}")
        return True

    # ── StarAlign relay ────────────────────────────────────────────────────────

    def _relay_to_staralign(self, nick: str, text: str) -> None:
        """Thread-safe: forward an IRC message to StarAlign bridge room.

        The Vampire/BatBot IRC nick is relayed as the shared "bot"
        identity so it shows on StarAlign as "bot", not a Guest.
        """
        if self.loop is None or not self.loop.is_running():
            return
        batbot_nick = (getattr(config, "BATBOT_IRC_NICK", "") or "").strip().lower()
        is_bot = bool(batbot_nick) and nick.strip().lower() == batbot_nick
        asyncio.run_coroutine_threadsafe(
            relay_to_staralign(
                username="bot" if is_bot else nick,
                text=text,
                kind="bot" if is_bot else "user",
            ),
            self.loop,
        )

    # ── Discord relay ─────────────────────────────────────────────────────────

    def _relay_to_discord(self, text: str, system: bool = False,
                          discord_channel: str = None):
        """Thread-safe: post a message to the correct Discord bridge channel."""
        if self.loop is None or not self.loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._post_discord(text, discord_channel),
            self.loop,
        )

    async def _post_discord(self, text: str, discord_channel: str = None):
        channel = self._get_bridge_channel(discord_channel)
        if channel:
            try:
                await channel.send(text[:2000])
            except Exception as e:
                print(f"[irc_bridge] Discord send error: {e}")

    def _get_bridge_channel(self, channel_name: str = None):
        name = (channel_name or getattr(config, "BRIDGE_CHANNEL", "")).lower()
        if not name:
            return None
        for guild in self.bot.guilds:
            ch = next(
                (c for c in guild.text_channels if c.name.lower() == name),
                None,
            )
            if ch:
                return ch
        return None

    def _raw(self, msg: str):
        if self._sock:
            self._sock.sendall(f"{msg}\r\n".encode("utf-8"))

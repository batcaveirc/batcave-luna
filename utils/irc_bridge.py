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

_NICK_RECLAIM_SECS = 60      # how often to check we still hold our own nick
# 12s was long enough that a normal back-and-forth got swallowed: someone says
# hello, she answers, they reply and she ignores them. Silence reads as "the
# bot is broken", which is worse than the flood these numbers were guarding
# against. Short enough to hold a conversation, long enough to stop a wall.
_AI_COOLDOWN      = 4        # seconds between AI replies to one person
_AI_CHANNEL_GAP   = 2        # seconds between AI replies in a channel


def _wrap(text: str, size: int = 380) -> List[str]:
    """Split on word boundaries. IRC drops everything past ~512 bytes for the
    whole line, so a long answer loses its tail with no error anywhere."""
    words, out, line = text.split(" "), [], ""
    for w in words:
        if line and len(line) + 1 + len(w) > size:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}" if line else w
    if line:
        out.append(line)
    return out or [""]


def numeric(line: str) -> str:
    """The server numeric of a line, or "".

    Substring tests like `" 433 " in line` look equivalent and are not: any
    message whose TEXT contains that number matches too. The identical bug made
    the Vampire bot rename itself to "Vampire_" because numeric 254 reported a
    channel count that happened to contain "433". Anchor it to the position a
    numeric actually occupies.
    """
    m = re.match(r"^:\S+\s+(\d{3})\s", line)
    return m.group(1) if m else ""




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

        # Channels Luna JOINS but does not relay. Moderation runs in these;
        # nothing said there crosses to Discord. A room only starts relaying
        # when someone runs $ircjoin for it on the Discord side, which keeps
        # "she is present" and "this room is public elsewhere" separate
        # decisions — the second one should always be deliberate.
        self._extra: Set[str] = {
            c.strip() if c.strip().startswith("#") else f"#{c.strip()}"
            for c in os.getenv("IRC_EXTRA_CHANNELS", "").split(",") if c.strip()
        }

        # ── Per-channel nick tracking ────────────────────────────────────────
        self._nicks: Dict[str, Set[str]] = {}   # irc_ch.lower() → set of nicks
        self._nicks_lock = threading.Lock()
        self._prefixes: Dict[str, str] = {}   # "chan|nick" -> "@" / "+" / ""

        # ── Topic cache ──────────────────────────────────────────────────────
        self._topics: Dict[str, str] = {}
        self._topics_lock = threading.Lock()

        # ── Outbound send queue ──────────────────────────────────────────────
        self._send_q: deque = deque()            # (irc_channel, text)
        self._send_lock = threading.Lock()


        # The nick we are ACTUALLY using. Not always config.IRC_NICK: a 433
        # collision or NickServ enforcement can change it under us, and code
        # that assumes otherwise stops recognising its own messages.
        self._nick = config.IRC_NICK
        self._last_reclaim = 0.0
        self._ai_cooldown: Dict[str, float] = {}   # nick(lower) -> ts
        self._ai_last_channel = 0.0
        self._connect_time = time.time()
        self._last_tags: Dict[str, str] = {}
        self._hosts: Dict[str, str] = {}   # nick(lower) -> user@host

        from utils.moderation import Moderator
        self.moderator = Moderator(self)


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

    def all_channels(self) -> List[str]:
        """Bridged channels plus the join-only ones."""
        with self._map_lock:
            mapped = list(self._i2d.keys())
        return list(dict.fromkeys(mapped + sorted(self._extra)))

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

    def host_of(self, nick: str) -> str:
        return self._hosts.get(nick.lower(), "")

    def has_prefix(self, irc_channel: str, nick: str) -> bool:
        """True if the nick carries an operator-ish prefix in that channel.

        If we have no record at all, ask the server for a fresh NAMES. Our view
        can be stale — a mode set while we were reconnecting, a nick change we
        missed — and silently answering "not an operator" from an empty cache
        refuses someone who plainly is one.
        """
        key = f"{irc_channel.lower()}|{nick.lower()}"
        with self._nicks_lock:
            pfx = self._prefixes.get(key)
        if pfx is None and self._connected:
            self._raw(f"NAMES {irc_channel}")
            return False
        return bool(re.search(r"[~&@%]", pfx or ""))

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

    def ask_luna(self, irc_ch: str, nick: str, prompt: str) -> bool:
        """Answer someone in the channel, using the Discord loop for the call.

        The IRC reader runs in its own thread and the AI call is async, so the
        coroutine is scheduled on Discord's loop and the reply is queued from
        the callback. Blocking the reader for the length of a generation would
        stall the relay for everyone else in the room.
        """
        if self.loop is None or not prompt.strip():
            return False
        now = time.time()
        key = nick.lower()
        if now - self._ai_cooldown.get(key, 0.0) < _AI_COOLDOWN:
            return False        # too soon; the line still relays as normal chat
        if now - self._ai_last_channel < _AI_CHANNEL_GAP:
            return False
        self._ai_cooldown[key] = now
        self._ai_last_channel = now

        from cogs.ai_cog import ask

        def _done(fut):
            try:
                reply = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[irc_bridge] AI error: {e}")
                return
            if reply:
                one_line = " ".join(str(reply).split())
                self._queue(irc_ch, f"{nick}: {one_line[:400]}")

        try:
            fut = asyncio.run_coroutine_threadsafe(ask(prompt), self.loop)
            fut.add_done_callback(_done)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[irc_bridge] AI dispatch failed: {e}")
            return False

    def _reclaim_nick(self) -> None:
        """Re-identify, evict whatever holds our nick, take it back, rejoin.

        Called both on a detected force-rename and from the watchdog, because
        enforcement is not the only way to lose a nick.
        """
        if not config.IRC_NICKSERV_PASS:
            return
        try:
            self._raw(f"PRIVMSG NickServ :IDENTIFY {config.IRC_NICKSERV_PASS}")
            self._raw(f"PRIVMSG NickServ :GHOST {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
            self._raw(f"PRIVMSG NickServ :RELEASE {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
            self._raw(f"NICK {config.IRC_NICK}")
            for ch in self.all_channels():
                self._raw(f"JOIN {ch}")
        except Exception as e:  # noqa: BLE001 — recovery must never kill the loop
            print(f"[irc_bridge] Nick reclaim failed: {e}")

    def quit(self, message: str = "Luna fades into the moonlight...") -> None:
        """Leave cleanly. Without this the session lingers until ping-timeout
        and the NEXT run finds its own nick taken — which is how a bot ends up
        as Luna1_ every handoff. GitHub Actions sends SIGTERM at the 6h cap, so
        this runs roughly four times a day."""
        self._running = False
        try:
            if self._sock:
                self._raw(f"QUIT :{message}")
                time.sleep(0.4)          # let it reach the server before FIN
                self._sock.close()
        except Exception:
            pass

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

    def _queue(self, irc_channel: str, text: str, verb: str = "PRIVMSG"):
        """verb is NOTICE for command replies: a NOTICE to a nick is the IRC
        convention for a bot answering one person without addressing the room."""
        with self._send_lock:
            self._send_q.append((irc_channel, text, verb))

    def _notice(self, nick: str, text: str) -> None:
        self._queue(nick, text, "NOTICE")

    def _sender_loop(self):
        """Drain the send queue at _SEND_DELAY intervals (rate-limiting)."""
        while self._running:
            time.sleep(_SEND_DELAY)
            if not self._connected:
                continue
            with self._send_lock:
                if not self._send_q:
                    continue
                irc_ch, text, verb = self._send_q.popleft()
            try:
                self._raw(f"{verb} {irc_ch} :{text}")
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
        self._nick = config.IRC_NICK
        self._connect_time = time.time()
        # server-time marks replayed +H history with when it was ORIGINALLY
        # said. Without it Luna re-relays the whole backlog to Discord on every
        # six-hour restart, which is a wall of duplicated conversation.
        self._raw("CAP REQ :server-time")
        self._raw("CAP END")
        self._raw(f"NICK {config.IRC_NICK}")
        self._raw(f"USER {config.IRC_NICK} 0 * :{config.IRC_REALNAME}")

        buf = ""
        silent_rounds = 0
        while self._running and not self._force_reconnect:
            try:
                data = self._sock.recv(4096).decode("utf-8", errors="replace")
            except socket.timeout:
                # A half-open TCP link (NAT timeout, dropped route) stays
                # writable forever: our PINGs vanish and nothing comes back, so
                # the bot looks online and answers nothing. Inbound silence is
                # the only honest evidence, and the server pings us every couple
                # of minutes — so after several silent rounds, tear it down and
                # let the reconnect path run.
                silent_rounds += 1
                if self._connected:
                    self._raw(f"PING :{config.IRC_SERVER}")
                if silent_rounds >= 4:      # 4 × _SOCKET_TIMEOUT = 2 minutes
                    print("[irc_bridge] No inbound traffic for 2 min — link is "
                          "dead, forcing reconnect.")
                    break
                continue
            if not data:
                break
            silent_rounds = 0
            self._last_ping = time.time()
            # Enforcement can strike at any time, not only at registration.
            if (self._connected
                    and self._nick.lower() != config.IRC_NICK.lower()
                    and time.time() - self._last_reclaim > _NICK_RECLAIM_SECS):
                self._last_reclaim = time.time()
                print(f"[irc_bridge] Still on {self._nick} — retrying reclaim.")
                self._reclaim_nick()
            buf += data
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                self._handle_line(line)

    # ── Line handler ──────────────────────────────────────────────────────────

    def _is_replay(self, tags: Dict[str, str]) -> bool:
        """True for a line the server replayed out of channel history (+H).

        Untagged lines count as live: if the server does not support
        server-time we must not start discarding real conversation.
        """
        stamp = tags.get("time")
        if not stamp:
            return False
        try:
            from datetime import datetime
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            return False
        return when < self._connect_time - 5

    def _handle_line(self, line: str):
        tags: Dict[str, str] = {}
        if line.startswith("@"):
            head, _, line = line.partition(" ")
            for kv in head[1:].split(";"):
                k, _, v = kv.partition("=")
                if k:
                    tags[k] = v
        self._last_tags = tags

        # PING keepalive
        if line.startswith("PING"):
            self._raw("PONG " + line[5:])
            return

        # ERROR :Closing Link — server is booting us
        if line.startswith("ERROR"):
            print(f"[irc_bridge] Server error: {line}")
            raise ConnectionError(line)

        num = numeric(line)

        # 001 = registered
        if num == "001":
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
            for ch in self.all_channels():
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
        if num == "433":
            fallback = f"{config.IRC_NICK}_"
            print(f"[irc_bridge] Nick in use — using {fallback}, will GHOST after auth")
            self._nick = fallback
            self._raw(f"NICK {fallback}")
            return

        # MODE — track +o/-o/+v so has_prefix() stays current between NAMES.
        m = re.match(r"^:\S+\s+MODE\s+(#\S+)\s+(\S+)\s+(.*)$", line)
        if m:
            ch, modes, targets = m.group(1).lower(), m.group(2), m.group(3).split()
            adding, ti = True, 0
            for c in modes:
                if c == "+":
                    adding = True
                elif c == "-":
                    adding = False
                elif c in "ovhq":
                    who = targets[ti] if ti < len(targets) else ""
                    ti += 1
                    if who:
                        sym = {"o": "@", "v": "+", "h": "%", "q": "~"}[c]
                        k = f"{ch}|{who.lower()}"
                        with self._nicks_lock:
                            cur = self._prefixes.get(k, "")
                            self._prefixes[k] = (
                                cur + sym if adding and sym not in cur
                                else cur.replace(sym, "") if not adding else cur)
                elif c in "beIkl":
                    ti += 1
            return

        # 353 NAMREPLY — populate per-channel nick list
        m = re.match(r"^:\S+\s+353\s+\S+\s+[=@*]\s+(\S+)\s+:(.*)", line)
        if m:
            irc_ch   = m.group(1).lower()
            raw_nicks = m.group(2).split()
            cleaned = set()
            with self._nicks_lock:
                for raw in raw_nicks:
                    pfx = (re.match(r"^[~&@%+]+", raw) or [""])[0] if raw else ""
                    bare = raw.lstrip("@+%&~")
                    if not bare:
                        continue
                    cleaned.add(bare)
                    # Prefixes are stripped for the nick list but kept here:
                    # moderation must never act on a channel operator, and
                    # this is the only place the server tells us who is one.
                    self._prefixes[f"{irc_ch}|{bare.lower()}"] = pfx
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
            # Were WE the one renamed? NickServ enforcement force-renames an
            # unidentified protected nick to Guest#### within ~1.5s, and the
            # channel then refuses "Guest*". This took the Vampire bot offline
            # for hours because nothing noticed it was no longer itself:
            # identifying early narrows the race but cannot remove it, so the
            # missing half is recovery.
            # Status follows the person, not the string. Renaming used to drop
            # every prefix we knew, so an operator who changed nick instantly
            # stopped being recognised as one.
            with self._nicks_lock:
                for key in [k for k in self._prefixes if k.endswith(f"|{old_nick.lower()}")]:
                    chan = key.rsplit("|", 1)[0]
                    self._prefixes[f"{chan}|{new_nick.lower()}"] = self._prefixes.pop(key)
                host = self._hosts.pop(old_nick.lower(), None)
                if host:
                    self._hosts[new_nick.lower()] = host

            if old_nick.lower() == self._nick.lower():
                self._nick = new_nick
                if new_nick.lower() != config.IRC_NICK.lower():
                    print(f"[irc_bridge] Force-renamed to {new_nick} — reclaiming.")
                    self._reclaim_nick()
            return

        # PRIVMSG — channel or PM
        m = re.match(r"^:([^!]+)!(\S+)\s+PRIVMSG\s+(\S+)\s+:(.*)$", line)
        if m:
            # Remember the host: trust that follows a person rather than a nick
            # needs it, and someone who changes nick keeps the same host.
            self._hosts[m.group(1).lower()] = m.group(2)
            m = re.match(r"^:([^!]+)!\S+\s+PRIVMSG\s+(\S+)\s+:(.*)$", line)
            nick    = m.group(1)
            target  = m.group(2)
            message = m.group(3).strip()

            # Ignore own messages
            if nick.lower() in (config.IRC_NICK.lower(), f"{config.IRC_NICK}_".lower()):
                return
            # Replayed channel history is not new conversation: relaying it
            # would repost the backlog to Discord on every restart, and
            # answering it would have Luna reply to questions from hours ago.
            if self._is_replay(getattr(self, "_last_tags", {})):
                return

            # ── Channel message ──
            if target.startswith("#"):
                # Auto-moderation first: if Luna acts on a line, it does not
                # then get answered or relayed.
                try:
                    if self.moderator.check_message(target, nick, message):
                        return
                except Exception as e:  # noqa: BLE001
                    print(f"[irc_bridge] moderation error: {e}")

                # "$ai <question>" — and plain "Luna, ..." because nobody in a
                # chatroom types a command to talk to someone.
                low = message.lower()
                me = self._nick.lower()
                asked = message.startswith(f"{config.PREFIX}ai ")
                spoken_to = (
                    low.startswith(f"{me} ") or low.startswith(f"{me},")
                    or low.startswith(f"{me}:") or f" {me} " in f" {low} "
                )
                if asked or spoken_to:
                    prompt = message[len(config.PREFIX) + 3:] if asked else message
                    if self.ask_luna(target, nick, prompt):
                        return

                # Luna's own commands ($ping, $roll, $weather …). Runs before
                # the bridge-mapping check so they work in any channel she is
                # in, not only a bridged one.
                if message.startswith(config.PREFIX):
                    try:
                        from shared_cmds import SharedCommands
                        reply = SharedCommands.get(self.bot, self).dispatch_irc(
                            nick, message, target)
                        if reply:
                            # NOTICE to the caller, not the channel: a help
                            # listing is for the person who asked. And split on
                            # word boundaries — IRC truncates a long line
                            # silently, which is how $help lost its tail.
                            for chunk in _wrap(str(reply)):
                                self._notice(nick, chunk)
                            return
                    except Exception as e:  # noqa: BLE001 — never kill the reader
                        print(f"[irc_bridge] shared command error: {e}")

                disc_ch = self.get_discord_for_irc(target)
                if disc_ch is None:
                    return   # not a bridged channel

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
            else:
                try:
                    self.moderator.check_join(channel, nick)
                except Exception as e:  # noqa: BLE001
                    print(f"[irc_bridge] join check error: {e}")
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

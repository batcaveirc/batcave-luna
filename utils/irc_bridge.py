"""
IRC Bridge — connects Luna to IRC.
Multi-channel: maps any Discord channel ↔ any IRC channel, N pairs.
Runs in a background thread.  Messages flow both ways:
  IRC → Discord  and  Discord → IRC

Discord commands (~prefix) are suppressed from IRC relay.
"""

import asyncio
import os
import pathlib
import random
import re
from fnmatch import fnmatch
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
# When the server will not even let us finish connecting, the address is the
# problem and no amount of retrying changes it. Wait a long time instead.
# Once we conclude the address is refused we no longer WAIT it out (the address
# cannot recover for this runner) — we retry a few times quickly to be sure, then
# EXIT so a fresh runner is drawn. So the "refused" delay is short now, not 30
# minutes; the old long backoff just kept the bot absent for the whole job.
_REFUSED_RETRY = 20                  # seconds between confirming retries before we give up
_REFUSALS_BEFORE_SLOWDOWN = 3        # a couple of flukes are not a block
# After this many consecutive refusals AT THE DOOR, give up on this IP and EXIT,
# so the GitHub job ends and the next scheduled run draws a FRESH runner with a
# fresh address. Sitting here backing off cannot fix an IP block — the address
# is fixed for the life of the runner — so a bot that only backs off stays
# absent from the room for the whole job (up to 6 hours) while looking healthy.
# Dracula already exits on refusal; Luna did not, and that is why she vanished
# after today's restarts landed on blocked addresses.
_REFUSALS_BEFORE_EXIT = 5


def _refused_at_the_door(exc) -> bool:
    """Does this failure mean the SERVER would not have us, rather than a glitch?

    The observed shape is an immediate TLS EOF: the handshake is closed rather
    than answered. Connection refused and timeouts count too — all of them mean
    nothing we send next will be read.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        sign in text
        for sign in (
            "closed (eof)", "eof occurred", "connection refused", "connection reset",
            "timed out", "unreachable", "z-lined", "k-lined", "g-lined", "banned",
        )
    )
_SOCKET_TIMEOUT      = 30    # detect dead connections fast
_SEND_DELAY          = 0.5   # seconds between outbound IRC messages (rate-limit)

_NICK_RECLAIM_SECS = 60      # how often to check we still hold our own nick
_MEMORY_COOLDOWN = 8         # seconds between one person's history commands
_RECENT_LINES = 25           # live tail kept per room for grounding the AI
# Phase 3: per-user memory of what people have said in #batcave recently,
# used to inject "you remember X said Y" into AI replies. Smaller than the
# live tail per room, longer-lived.
_MEMORY_MAX_PER_USER = max(1, int(os.getenv("MEMORY_MAX_PER_USER") or 10))
# No floor: a test sets 2s, production sets days; both are the operator's call.
_MEMORY_TTL_SEC = max(1, int(int(os.getenv("MEMORY_TTL_MS") or (7 * 24 * 60 * 60 * 1000)) / 1000))
_MEMORY_MIN_LEN = max(1, int(os.getenv("MEMORY_MIN_LEN") or 15))
# Phase 2: inter-bot teamwork via #batcave-trust. Heartbeat interval and the
# silence threshold after which the partner is treated as down.
_TRUST_HB_SEC = max(60, int(int(os.getenv("TRUST_HB_MS") or 180000) / 1000))
_PARTNER_SILENT_SEC = max(_TRUST_HB_SEC * 2, 6 * 60)
# Following the community's rooms. Luna leaves a room herself once it has been
# quiet this long — but she does NOT rejoin on a timer, because join/part
# cycling is the exact abuse signature this network kills bots for. Coming back
# is event-driven: an INVITE, or an op's $follow. Auto-leave is safe; auto-
# rejoin-on-a-clock is not, and that tension is the owner's own #1 priority
# ("i dont wanna be banned cause of fast nick changes").
_FOLLOW_IDLE = max(10, int(os.getenv("FOLLOW_IDLE_MIN", "45"))) * 60

# Dracula keeps the trust list in ChanServ FLAGS on its own channel, and that is
# the ONLY place it lives. Luna had a separate list in a secret, read once at
# startup — so "!!trust add hazel" meant nothing to her, and seconds later she
# kicked hazel out of the room. Two lists for one idea is how that happens; this
# reads the same one Dracula writes.
TRUST_CHANNEL = os.getenv("IRC_TRUST_CHANNEL", "#batcave-trust")
_TRUST_REFRESH = 3600
_TRUST_RETRY = 90
_TRUST_ROW = re.compile(r"^\s*\d+\s+(\S+)\s+(\+\S*)")
_TRUST_END = re.compile(r"End of .* FLAGS listing", re.I)
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


# Two IRC rooms now relay into Discord, so "[Portal]" no longer says anything
# useful — the reader cannot tell which room they are answering. Label by room.
_ROOM_LABELS = {
    lbl.split("=")[0].strip().lower(): lbl.split("=")[1].strip()
    for lbl in os.getenv("ROOM_LABELS", "").split(",") if "=" in lbl
}


def _room_label(irc_channel: str) -> str:
    """A short, readable name for the room a message came from."""
    ch = (irc_channel or "").lower()
    if ch in _ROOM_LABELS:
        return _ROOM_LABELS[ch]
    # Use the room's own name, emoji and all. Substituting the word "emoji"
    # made every message from that room read as if it came from somewhere
    # generic, and there is more than one way a room can be non-ascii.
    return (irc_channel or "").lstrip("#") or "irc"


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
        self._ever_registered = False    # did we get past 001 this attempt?
        self._refusals      = 0         # consecutive refusals at the door
        self._force_reconnect = False
        self._last_ping     = time.time()

        # ── Channel mappings ─────────────────────────────────────────────────
        # discord_channel_name.lower() → irc_channel  (e.g. "batcave" → "#BatCave")
        # An explicit override of the reply direction, set from Discord with
        # $to. Kept apart from _d2i so the configured default is never lost and
        # can be returned to.
        self._targets: Dict[str, str] = {}
        self._d2i: Dict[str, str] = {}
        # irc_channel.lower()         → discord_channel_name
        self._i2d: Dict[str, str] = {}
        self._map_lock = threading.Lock()
        # Targets we have already complained about, so a missing channel
        # is reported once rather than on every relayed line.
        self._missing_targets: Set[str] = set()
        self._warned_no_loop = False
        # Rooms that refused our JOIN, and how often we have asked to be let in.
        self._locked_out: Dict[str, int] = {}

        # Seed default bridge from config
        _d_def = getattr(config, "BRIDGE_CHANNEL", "").lower()
        _i_def = getattr(config, "IRC_CHANNEL",    "")
        if _d_def and _i_def:
            self._add_mapping(_d_def, _i_def)

        # Channels Luna joins but does not relay. Kept for rooms that should
        # stay off Discord; both BatCave rooms are bridged now, each labelled
        # by name so a reader knows which one they are answering.
        self._extra: Set[str] = {
            c.strip() if c.strip().startswith("#") else f"#{c.strip()}"
            for c in os.getenv("IRC_EXTRA_CHANNELS", "").split(",") if c.strip()
        }
        for pair in os.getenv("EXTRA_BRIDGES", "").split(","):
            if "=" in pair:
                irc_ch, disc_ch = (x.strip() for x in pair.split("=", 1))
                if irc_ch and disc_ch:
                    self._add_mapping(disc_ch, irc_ch)
                    self._extra.discard(irc_ch)

        # ── Per-channel nick tracking ────────────────────────────────────────
        self._nicks: Dict[str, Set[str]] = {}   # irc_ch.lower() → set of nicks
        self._nicks_lock = threading.Lock()
        self._prefixes: Dict[str, str] = {}   # "chan|nick" -> "@" / "+" / ""

        # ── Topic cache ──────────────────────────────────────────────────────
        self._topics: Dict[str, str] = {}
        from utils.nsfw import Nsfw
        # Adult mode. A room is adult only when an op turns it on (which writes a
        # disclosure into the topic); consent is per session. See utils/nsfw.py.
        from utils.trivia import Trivia
        self._trivia = Trivia()
        self._trivia_channel = ""
        self._trivia_timer = None
        import os as _os
        self._nsfw = Nsfw(topic_of=self.get_topic,
                          adult_rooms=[r.strip() for r in
                                       _os.getenv("IRC_NSFW_ROOMS", "").split(",") if r.strip()])
        self._topics_lock = threading.Lock()

        # ── Outbound send queue ──────────────────────────────────────────────
        self._send_q: deque = deque()            # (irc_channel, text)
        self._send_lock = threading.Lock()


        # The nick we are ACTUALLY using. Not always config.IRC_NICK: a 433
        # collision or NickServ enforcement can change it under us, and code
        # that assumes otherwise stops recognising its own messages.
        self._nick = config.IRC_NICK
        self._last_reclaim = 0.0
        # The name we MEAN to wear, which is not always the name we have. The
        # reclaim check below compared against config.IRC_NICK, so ANY other
        # name read as "we lost our nick" — it would have undone every rotation
        # within a minute, GHOSTing and reclaiming each time.
        self._wanted_nick = config.IRC_NICK
        self._pending_rotation = ""
        self._rotation_numbered = False
        # Names that turned out to belong to somebody. A plain name can be
        # REGISTERED to a person who is simply offline: no 433, we take it, and
        # NickServ enforces seconds later and renames us to Guest####. We recover
        # — but picking it again every hour is the room watching the same failure
        # forever. Learn it once.
        self._unusable_names = set()
        self._memory_cooldown: Dict[str, float] = {}   # per nick; these scans are not cheap
        self._trusted: Set[str] = set()
        self._trusted_pending: Set[str] = set()
        self._trust_loaded = False
        self._trust_asked = 0.0
        self._rotations_at = []
        # If connect-time nick variation is on, suppress the immediate
        # first-tick rotation by setting _last_rotate to NOW (so the first
        # tick's delta is 0 and the interval gate holds the rotation back
        # until a full IRC_NICK_ROTATE_MIN has passed). Each run has already
        # picked a varied starting nick in config, so another rename at
        # startup is exactly the visible mid-run change we want to avoid.
        self._last_rotate = time.time() if getattr(config, "IRC_NICK_PICK_AT_CONNECT", False) else 0.0
        self._isupport: Dict[str, str] = {}   # what the server says it supports
        self._ai_cooldown: Dict[str, float] = {}   # nick(lower) -> ts
        # The last few lines per room, so Luna can answer "who was talking"
        # and "what's the convo" from what was ACTUALLY said instead of
        # inventing it — which is how "who was talking" got answered with a
        # summary of the 1872 novella Carmilla. In memory only, tiny, and it
        # is the live tail; the durable record still lives in Discord.
        self._recent: Dict[str, deque] = {}
        # ── Phase 2+3: per-user memory + inter-bot teamwork ────────────────
        # Each bot keeps its own view (both see every #batcave line anyway).
        # Memory of #batcave only; TTL and caps keep RAM bounded.
        self._user_memory: Dict[str, list] = {}
        self._user_memory_lock = threading.Lock()
        self._partner_last_seen: float = 0.0
        self._trust_hb_timer = None
        # Room-following. OFF unless IRC_FOLLOW is set: it changes what rooms the
        # bot sits in, and that should never be a surprise. The follow set is
        # rooms the OWNER named (config or $follow) — never rooms discovered by
        # snooping where people are — and within it Luna manages her own
        # departures when a room goes quiet.
        self._follow_on = os.getenv("IRC_FOLLOW", "").strip().lower() in ("1", "true", "yes", "on")
        self._follow: Set[str] = {
            (c if c.startswith("#") else f"#{c}")
            for c in os.getenv("IRC_FOLLOW_ROOMS", "").split(",") if c.strip()
        }
        # Shadow rooms: rooms Luna joins SILENTLY to serve as Dracula's eyes
        # where Dracula is banned. She NEVER speaks or relays from them — she
        # just reads, remembers, and broadcasts ::saw via the trust channel so
        # Dracula gets the view it can't get itself. Owner-curated allowlist
        # ONLY (never auto-discovered) so a prank JOIN can't drag her into a
        # hostile room. Bounded on purpose: the 20/60s ::saw rate cap keeps
        # the trust channel from flooding even if a shadow room is busy.
        self._shadow: Set[str] = {
            (c if c.startswith("#") else f"#{c}").lower()
            for c in os.getenv("LUNA_SHADOW_ROOMS", "").split(",") if c.strip()
        }
        self._last_activity: Dict[str, float] = {}   # irc_ch -> ts of last line seen
        # ── Per-room AI toggle (feature #25) ───────────────────────────────────
        # Set of IRC rooms where AI responses are DISABLED. Home rooms default to
        # enabled. Non-home rooms (!!join'd) default to disabled — the room was
        # entered to listen, not to chat, until the owner opts in with $AI on.
        # Persists via ai_room_state.json alongside Luna's other state files.
        self._ai_disabled_rooms: Set[str] = set()
        try:
            import json as _json
            p = pathlib.Path(__file__).parent.parent / "ai_room_state.json"
            if p.exists():
                self._ai_disabled_rooms = {
                    str(k).lower() for k in (_json.loads(p.read_text()).get("disabled") or [])
                }
        except Exception:
            pass
        # Speak only where she is an operator. The owner: "make sure my bots dont
        # message anything in other rooms except the rooms they are a mod." ON by
        # default; bridged/home rooms are always exempt so the relay can never go
        # silent because of it. In a followed room where she is not opped she
        # simply listens.
        self._speak_only_where_op = os.getenv(
            "IRC_MOD_ONLY_SPEECH", "on").strip().lower() not in ("0", "false", "no", "off")
        self._silent_logged: Set[str] = set()
        self._ai_last_channel = 0.0
        self._connect_time = time.time()
        self._last_tags: Dict[str, str] = {}
        self._hosts: Dict[str, str] = {}   # nick(lower) -> user@host

        from utils.moderation import Moderator
        from utils.watch import Watch
        self.moderator = Moderator(self)
        # Early warning. Homes are resolved lazily from the bridge map, so it
        # follows any channel added later with $ircjoin.
        self.watch = Watch(
            homes=[getattr(config, "IRC_CHANNEL", "")] + [
                p.split("=", 1)[0].strip()
                for p in os.getenv("EXTRA_BRIDGES", "").split(",") if "=" in p
            ],
            enabled=not re.match(r"^(0|off|false|no)$",
                                 os.getenv("WATCH", "on"), re.I),
        )


    # ── Mapping helpers ───────────────────────────────────────────────────────

    def _home_rooms(self) -> set:
        """Rooms that are OURS — the bridged ones. Everything else is a room we
        are merely sitting in, where Luna listens and does nothing else.

        Read from _i2d, the IRC->Discord direction, because that is the one that
        holds EVERY bridged room. _d2i is Discord->IRC and must map each Discord
        channel to exactly one room, so when several IRC rooms feed one Discord
        channel it keeps only the first. Reading homes from it therefore made
        #batcave look foreign: Luna took the listen-only path there and returned
        before reaching her own commands, so $help answered in one room and was
        silent in the other.
        """
        with self._map_lock:
            return set(self._i2d.keys())

    def _add_mapping(self, discord_ch: str, irc_ch: str):
        """Map a Discord channel to an IRC room, both ways.

        Several IRC rooms may feed ONE Discord channel — that is normal when
        somebody keeps a single channel and reads the room labels. The reverse
        cannot be: a message typed in Discord has to go to exactly one room.

        So the IRC→Discord direction is many-to-one and always updated, while
        the Discord→IRC direction keeps the FIRST room registered. Last-wins was
        the accidental behaviour and it silently handed the reply path to
        whichever room happened to be parsed last — EXTRA_BRIDGES is read after
        the primary, so adding a second room would quietly stop Discord replies
        reaching the primary one.
        """
        d = discord_ch.lower()
        i = irc_ch if irc_ch.startswith("#") else f"#{irc_ch}"
        with self._map_lock:
            existing = self._d2i.get(d)
            if existing is None:
                self._d2i[d] = i
            elif existing.lower() != i.lower():
                print(f"[irc_bridge] '{d}' already replies to {existing}; {i} will "
                      f"post INTO it but Discord messages there still go to "
                      f"{existing}. Give {i} its own Discord channel for two-way.")
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

    def set_reply_target(self, discord_channel: str, irc_channel: str) -> bool:
        """Point one Discord channel at a DIFFERENT bridged room.

        Two IRC rooms feed one Discord channel here, and _add_mapping keeps the
        FIRST for the reply direction — so everything typed in Discord went to
        the emoji room and #batcave could not be reached from Discord at all.
        That is correct as a default (a message has to go exactly one place) but
        there was no way to choose the other place.

        This is that choice. It only accepts a room already bridged, so it can
        redirect but never invent a target.
        """
        i = irc_channel if irc_channel.startswith("#") else f"#{irc_channel}"
        with self._map_lock:
            if i.lower() not in self._i2d:
                return False
            self._targets[discord_channel.lower()] = i
        return True

    def get_reply_target(self, discord_channel: str) -> Optional[str]:
        """Where this Discord channel currently sends, and why it is that one."""
        d = discord_channel.lower()
        with self._map_lock:
            return self._targets.get(d) or self._d2i.get(d)

    def bridged_rooms(self) -> list:
        """Every IRC room this Discord channel can be pointed at."""
        with self._map_lock:
            return sorted(self._i2d.keys())

    def send_to_irc(self, message: str, discord_channel: str = ""):
        """
        Queue a message to the IRC channel mapped to discord_channel.

        An explicit target set with $to wins over the mapping, and a message
        beginning with a bridged room name goes there for that line only —
        "#batcave hello" reaches #batcave without changing anything.
        """
        if not self._connected:
            return
        text = message
        one_off = None

        # "#room rest of the message" — a single line aimed somewhere else.
        head = text.split(None, 1)
        if head and head[0].startswith("#"):
            cand = head[0]
            with self._map_lock:
                known = cand.lower() in self._i2d
            if known and len(head) > 1:
                one_off = cand
                text = head[1]

        irc_ch = one_off
        if not irc_ch and discord_channel:
            irc_ch = self.get_reply_target(discord_channel)
        if not irc_ch:
            pairs  = self.list_bridges()
            irc_ch = pairs[0][1] if pairs else getattr(config, "IRC_CHANNEL", "")
        if irc_ch:
            self._queue(irc_ch, text[:400])

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

    @property
    def nick(self) -> str:
        """Luna's current IRC nick. Public because callers legitimately need to
        ask "am I in that room, and am I opped there?" about the bot itself."""
        return self._nick

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
        if self._nick_allowed(): self._raw(f"NICK {new_nick}")
        return True

    def get_topic(self, channel: str | None = None) -> str | None:
        ch = (channel or self._default_irc_channel()).lower()
        with self._topics_lock:
            return self._topics.get(ch)

    # ── Phase 3: per-user memory of #batcave ───────────────────────────
    def _remember_line(self, nick: str, text: str, room: str = "", source: str = "local", t: float = 0.0) -> None:
        """Capture a notable line. Local calls broadcast ::saw; remote (::saw
        from the partner) do not, to stop echo storms. Dedupe is a 60s window
        on nick+text so a line we captured locally AND received as ::saw
        coalesces to one memory entry.
        """
        n = (nick or "").lower()
        if not n or n == (self._nick or "").lower():
            return
        try:
            if self.is_one_of_ours(nick):
                return
        except Exception:
            pass
        txt = (text or "").strip()
        if len(txt) < _MEMORY_MIN_LEN:
            return
        if txt.startswith(("$", "!!", ".")):
            return
        if (txt.startswith("http://") or txt.startswith("https://")) and " " not in txt:
            return
        now = time.time()
        key = f"{n}|{txt[:80].lower()}"
        # Dedupe map lives on self (threadsafe through the memory lock).
        with self._user_memory_lock:
            saw = getattr(self, "_saw_recently", None)
            if saw is None:
                saw = {}
                self._saw_recently = saw
            if now - saw.get(key, 0.0) < 60.0:
                return
            saw[key] = now
            if len(saw) > 500:
                stale = [k for k, ts in saw.items() if now - ts > 300.0]
                for k in stale:
                    saw.pop(k, None)
            lst = self._user_memory.setdefault(n, [])
            # Room is stored for diagnostics; the prompt does NOT reveal it,
            # so the bot does not announce "I heard you in #desilivechat.com".
            lst.append({"t": t or now, "text": txt[:200], "room": room or "#batcave"})
            while len(lst) > _MEMORY_MAX_PER_USER:
                lst.pop(0)
        # Share with the partner — ONLY for shadow rooms. Owner shrink
        # 2026-10-07: Dracula is in #batcave too and sees home-channel lines
        # directly, so home ::saw was redundant noise on #batcave-trust. The
        # one place Dracula CAN'T see is Luna's shadow rooms — those captures
        # are the trust channel's actual load-bearing content now. Rate-
        # limited as a safety net (Carfax RecvQ-exceeded lesson stands).
        if source != "remote" and (room or "").lower() in self._shadow and self._trust_broadcast_ok():
            try:
                self._trust_send("saw", {"n": n, "m": txt[:200], "r": room or "#batcave", "t": int(t or now)})
            except Exception:
                pass            # never block the chat path

    def _is_home_channel(self, room: str) -> bool:
        """True for the home room (and any directly-bridged rooms via
        all_channels). False for recruit rooms, follow rooms, trust. The point
        is that only home-channel captures are worth broadcasting; everything
        else stays local."""
        r = (room or "").lower()
        if not r:
            return True
        home = (config.IRC_CHANNEL or "#batcave").split(",")[0].strip().lower()
        if r == home:
            return True
        try:
            return r in {c.lower() for c in self.all_channels()}
        except Exception:
            return False

    def _should_broadcast(self, room: str) -> bool:
        """Where ::saw broadcasts are worth it. Home channels (bots are both
        in #batcave anyway, this syncs their views on drift) AND shadow rooms
        (Dracula is NOT in them — she is their only path to a view). Recruit
        rooms stay local-only, since Dracula sees them itself and amplifying
        every busy room was what killed Carfax with RecvQ exceeded."""
        if self._is_home_channel(room):
            return True
        return (room or "").lower() in self._shadow

    def _trust_broadcast_ok(self) -> bool:
        """20 ::saw broadcasts per 60s. Heartbeat is not metered — it is one
        line per 3 min and cannot flood."""
        now = time.time()
        history = getattr(self, "_trust_broadcast_history", None)
        if history is None:
            history = []
            self._trust_broadcast_history = history
        while history and now - history[0] > 60.0:
            history.pop(0)
        if len(history) >= 20:
            return False
        history.append(now)
        return True

    def _prune_memory(self) -> None:
        """Hourly: drop entries past TTL and users emptied by it. Reschedules."""
        now = time.time()
        dropped_users = 0
        dropped_lines = 0
        with self._user_memory_lock:
            empties = []
            for n, arr in self._user_memory.items():
                fresh = [e for e in arr if now - e["t"] <= _MEMORY_TTL_SEC]
                if not fresh:
                    empties.append(n)
                    dropped_lines += len(arr)
                elif len(fresh) != len(arr):
                    dropped_lines += (len(arr) - len(fresh))
                    self._user_memory[n] = fresh
                    dropped_users += 1
            for n in empties:
                self._user_memory.pop(n, None)
            saw = getattr(self, "_saw_recently", None)
            if saw:
                for k in [k for k, ts in saw.items() if now - ts > 300.0]:
                    saw.pop(k, None)
        if dropped_lines:
            print(f"[irc_bridge] memory prune: dropped {dropped_lines} old lines across {dropped_users} users "
                  f"(kept {len(self._user_memory)} users)")
        # Reschedule
        t = threading.Timer(3600.0, self._prune_memory)
        t.daemon = True
        t.start()

    def _joined_rooms(self) -> List[str]:
        """Every IRC room Luna is actually joined to right now. The bot's own
        bridge map (_i2d) + followed + shadow + anything the owner has her in."""
        with self._map_lock:
            homes = set(self._i2d.keys())
        out = set(homes) | set(self._shadow or set()) | set(self._follow or set())
        return sorted(out)

    _ROOM_Q = re.compile(
        r"\b(?:which|what|how many|list|where\s+(?:are|do))\s+(?:the\s+)?(?:other\s+)?rooms?\b",
        re.I,
    )
    _ROOM_PRESENCE = re.compile(
        r"\b(?:where\s+(?:are|do)\s+you|rooms?\s+(?:are|do)\s+you|you\s+(?:are|'re)\s+in)\b",
        re.I,
    )
    _ABOUT_USER = re.compile(
        r"\b(?:about|tell\s+me\s+about|know\s+about|info\s+(?:on|about)|"
        r"what\s+(?:do\s+)?you\s+know\s+(?:of|about)|who\s+is)\s+([A-Za-z0-9_\-\[\]{}\\\|`^]+)\b",
        re.I,
    )
    _KICK_Q = re.compile(r"\b(kick(?:ed)?|ban(?:ned)?|disconnect(?:ed)?|removed)\b", re.I)

    def _facts_for_prompt(self, prompt: str, irc_ch: str, asker: str) -> str:
        """Factual grounding to inject into the prompt. Returns a FACTS: block or ''.

        The AI hallucinates when asked things like "which rooms are you in" or
        "tell me about pinno" because it has no idea — this method pulls the
        real answers from the bot's own state and hands them to the model. The
        SYSTEM_PROMPT instructs the model to prefer FACTS over invention.
        """
        p = prompt or ""
        facts: List[str] = []

        # "which rooms are you in" / "where are you" / "are you kicked"
        if self._ROOM_Q.search(p) or self._ROOM_PRESENCE.search(p) or self._KICK_Q.search(p):
            rooms = self._joined_rooms()
            facts.append(
                f"FACT: You are connected RIGHT NOW and joined to these rooms: "
                f"{', '.join(rooms) if rooms else '(none yet)'}"
            )

        # "tell me about <nick>" / "what do you know about X"
        for m in self._ABOUT_USER.finditer(p):
            target = m.group(1)
            tl = target.lower()
            # Don't lecture about pronouns, articles, or the asker.
            if tl in {"you", "me", "yourself", "this", "that", "them", asker.lower()}:
                continue
            memory = self._memory_for(target)
            if memory:
                facts.append(
                    f"FACT: What you actually remember about {target} (from rooms you share):\n{memory}"
                )
            else:
                facts.append(
                    f"FACT: You have not seen {target} speak in any room you watch. "
                    f"Say so honestly — do not invent a history for them."
                )

        # "which rooms is <X> in" — cross-room lookup for someone else
        m = re.search(r"\b(?:which|what)\s+rooms?\s+(?:is|does)\s+(\S+)", p, re.I)
        if m:
            target = m.group(1).strip("?.,!").lower()
            if target not in {"you", "me", asker.lower()}:
                memory = self._memory_for(target)
                if not memory:
                    facts.append(
                        f"FACT: You have no record of {target} being in any room you watch."
                    )

        if not facts:
            return ""
        return "FACTS YOU KNOW RIGHT NOW (prefer these over any guess):\n" + "\n".join(facts)

    def _memory_for(self, nick: str) -> str:
        """Return a formatted recall block for the prompt, or '' if empty.

        Last line is dropped — that's the one the user just typed; the model
        already sees it. Memory is OLDER context.
        """
        n = (nick or "").lower()
        now = time.time()
        with self._user_memory_lock:
            lst = self._user_memory.get(n, [])
            fresh = [e for e in lst if now - e["t"] <= _MEMORY_TTL_SEC]
            if fresh != lst:
                if fresh:
                    self._user_memory[n] = fresh
                else:
                    self._user_memory.pop(n, None)
        older = fresh[:-1][-6:]
        if not older:
            return ""
        parts = []
        for e in older:
            mins = max(0, int((now - e["t"]) / 60))
            ago = f"{mins}m" if mins < 60 else (f"{mins // 60}h" if mins < 1440 else f"{mins // 1440}d")
            parts.append(f"  - [{ago} ago] {e['text']}")
        return "\n".join(parts)

    # ── Phase 2: inter-bot teamwork via #batcave-trust ─────────────────
    def _trust_send(self, verb: str, data: dict) -> None:
        """Compact machine message to the trust channel. Human chatter there
        is ignored; bots only parse lines prefixed with '::'."""
        if not TRUST_CHANNEL:
            return
        try:
            import json as _json
            self._raw(f"PRIVMSG {TRUST_CHANNEL} :::{verb} {_json.dumps(data, separators=(',', ':'))}")
        except Exception:
            pass            # never block normal operation

    def _handle_trust_line(self, from_nick: str, text: str) -> None:
        """Parse '::verb {json}' from the trust channel.

        Everything that doesn't match the shape is ignored on purpose — the
        trust channel is also for humans managing ChanServ flags, and their
        chatter is theirs."""
        s = (text or "").lstrip()
        if not s.startswith("::"):
            return
        rest = s[2:]
        if " " in rest:
            verb, payload = rest.split(" ", 1)
        else:
            verb, payload = rest, ""
        try:
            import json as _json
            data = _json.loads(payload) if payload else {}
        except Exception:
            return
        if verb == "hb":
            n = str(data.get("n", "")).lower()
            if n and n != (self._nick or "").lower():
                self._partner_last_seen = time.time()
            return
        if verb == "saw":
            # Partner saw a line; merge with source=remote so we do NOT loop.
            # Dedupe inside _remember_line coalesces this with our own capture.
            n = data.get("n", "")
            m = data.get("m", "")
            if n and m:
                try:
                    self._remember_line(
                        n, m,
                        room=str(data.get("r") or "#batcave"),
                        source="remote",
                        t=float(data.get("t") or time.time()),
                    )
                except Exception as e:  # noqa: BLE001 — never block trust path
                    print(f"[irc_bridge] ::saw merge error: {e}")
            return
        # Reserved for later: ::act (moderation taken), ::mem (full-sync pulls)

    def partner_is_silent(self) -> bool:
        return self._partner_last_seen > 0 and (time.time() - self._partner_last_seen) > _PARTNER_SILENT_SEC

    def _start_trust_teamwork(self) -> None:
        """Join the trust channel and start the heartbeat. Idempotent."""
        if self._trust_hb_timer is not None:
            return
        if not TRUST_CHANNEL:
            return
        try:
            self._raw(f"JOIN {TRUST_CHANNEL}")
        except Exception:
            pass

        def _beat():
            try:
                self._trust_send("hb", {"n": self._nick, "t": int(time.time())})
            except Exception:
                pass
            # Reschedule
            self._trust_hb_timer = threading.Timer(_TRUST_HB_SEC, _beat)
            self._trust_hb_timer.daemon = True
            self._trust_hb_timer.start()

        # First heartbeat 15s in, so the registration burst has cleared.
        self._trust_hb_timer = threading.Timer(15.0, _beat)
        self._trust_hb_timer.daemon = True
        self._trust_hb_timer.start()
        # Memory GC: hourly from here on. Idempotent — if someone restarts
        # teamwork, we just reschedule and the old timer fires once more.
        t = threading.Timer(3600.0, self._prune_memory)
        t.daemon = True
        t.start()

    def ask_luna(self, irc_ch: str, nick: str, prompt: str) -> bool:
        """Answer someone in the channel, using the Discord loop for the call.

        The IRC reader runs in its own thread and the AI call is async, so the
        coroutine is scheduled on Discord's loop and the reply is queued from
        the callback. Blocking the reader for the length of a generation would
        stall the relay for everyone else in the room.
        """
        if self.loop is None or not prompt.strip():
            return False
        # Feature #25: per-room AI toggle. If this room's AI is turned off, bail
        # before cooldown accounting so the toggle feels crisp.
        if irc_ch.lower() in self._ai_disabled_rooms:
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

        # What the room has actually been saying, so "who was talking" and
        # "what's the convo" are answered from fact, not invented. The line the
        # caller just typed is already in here.
        lines = list(self._recent.get(irc_ch.lower(), ()))[-_RECENT_LINES:]
        context = "\n".join(f"{who}: {said}" for who, said in lines)
        # Phase 3: and what THIS speaker has said in #batcave recently, days
        # ago too. Appended to context so ai_cog._context_note treats it as
        # overheard chatter (never instructions), same framing as live tail.
        memory = self._memory_for(nick)
        if memory:
            context = (context + "\n\n" if context else "") \
                + f"What {nick} has said recently (you remember them from this and nearby rooms):\n{memory}"
        # Grounding (added 2026-10-08): the AI was happily inventing room names
        # and user histories when asked ("which rooms are you in" → "the midnight
        # playlist chatroom"; "tell me about pinno" → made-up biography). The
        # FACTS block is prepended to context so the system prompt sees the
        # ground truth before deciding how to answer.
        facts = self._facts_for_prompt(prompt, irc_ch, nick)
        if facts:
            context = (facts + "\n\n" + context) if context else facts

        def _done(fut):
            try:
                reply = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[irc_bridge] AI error: {e}")
                return
            if reply:
                one_line = " ".join(str(reply).split())
                # Don't hard-cut at 400 — split across a couple of lines on word
                # boundaries so a longer answer is not lost mid-word (parity with
                # Dracula's "never cut off").
                chunks, rest = [], one_line
                while rest and len(chunks) < 3:
                    if len(rest) <= 400:
                        chunks.append(rest)
                        break
                    cut = rest.rfind(" ", 0, 400)
                    cut = cut if cut > 0 else 400
                    chunks.append(rest[:cut])
                    rest = rest[cut:].lstrip()
                for i, ch in enumerate(chunks):
                    self._queue(irc_ch, f"{nick}: {ch}" if i == 0 else ch)

        try:
            # Strip our OWN nick if the line opens by addressing us, so she
            # answers the speaker, not herself ("andromeda u there" must not get
            # "Hey there, Andromeda!"). Pass the live nick so she knows it is her.
            mine = "|".join(re.escape(n) for n in {self._nick.lower(), config.IRC_NICK.lower()} if n)
            ask_text = re.sub(rf"^\s*(?:{mine})\s*[:,]?\s*", "", prompt, flags=re.I).strip() or prompt
            fut = asyncio.run_coroutine_threadsafe(
                ask(ask_text, context=context, me=self._nick), self.loop)
            fut.add_done_callback(_done)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[irc_bridge] AI dispatch failed: {e}")
            return False

    # Commands whose work lives on the Discord side. Until now these existed
    # only as Discord commands, so from IRC — where the room actually is — they
    # did nothing at all and were listed nowhere. The owner: "i dont see them in
    # $help did you update it or no".
    MEMORY_CMDS = ("find", "search", "tell", "memo", "stats", "activity",
                   "quote", "onthisday", "rewind", "backthen", "seen", "mood")

    NSFW_CMDS = ("nsfw", "boundaries",
                 "afterdark", "spicy", "tempt", "fantasy", "midnight", "desire")

    _TRIVIA_REVEAL = 30      # seconds to answer before the answer is shown
    _TRIVIA_GAP = 4          # pause between questions in a running session

    def _trivia_cancel_timer(self):
        t = self._trivia_timer
        if t is not None:
            try:
                t.cancel()
            except Exception:      # noqa: BLE001
                pass
            self._trivia_timer = None

    def _trivia_ask(self, irc_ch: str, question: str):
        self._trivia_cancel_timer()
        self._queue(irc_ch, f"\x02Trivia:\x03 {question}")
        self._trivia_timer = threading.Timer(self._TRIVIA_REVEAL, self._trivia_reveal, args=(irc_ch,))
        self._trivia_timer.daemon = True
        self._trivia_timer.start()

    def _trivia_reveal(self, irc_ch: str):
        """Nobody answered in time: show the answer, then move on (or stop)."""
        if not self._trivia.question():
            return
        self._queue(irc_ch, f"Time! The answer was \x02{self._trivia.answer_text()}\x02.")
        if self._trivia.running:
            self._trivia_timer = threading.Timer(
                self._TRIVIA_GAP, lambda: self._trivia_ask(irc_ch, self._trivia.next_question()))
            self._trivia_timer.daemon = True
            self._trivia_timer.start()
        else:
            self._trivia_channel = ""

    def check_trivia_answer(self, irc_ch: str, nick: str, message: str):
        """Called for every room line while trivia is live in this channel."""
        if not self._trivia_channel or irc_ch.lower() != self._trivia_channel.lower():
            return
        winner = self._trivia.check(nick, message)
        if not winner:
            return
        self._trivia_cancel_timer()
        self._queue(irc_ch, f"\x02{winner}\x03 got it — {self._trivia.answer_text()}! "
                    + (f"({self._trivia.scores.get(winner)} this round)"
                       if self._trivia.running else ""))
        if self._trivia.running:
            self._trivia_timer = threading.Timer(
                self._TRIVIA_GAP, lambda: self._trivia_ask(irc_ch, self._trivia.next_question()))
            self._trivia_timer.daemon = True
            self._trivia_timer.start()
        else:
            self._trivia_channel = ""

    TRIVIA_CMDS = ("trivia",)

    def try_trivia_command(self, irc_ch: str, nick: str, text: str) -> bool:
        from shared_cmds import is_irc_owner
        body = text[len(config.PREFIX):] if text.startswith(config.PREFIX) else ""
        parts = body.split()
        if not parts or parts[0].lower() not in self.TRIVIA_CMDS:
            return False
        arg = parts[1].lower() if len(parts) > 1 else ""
        # on/off run a whole session and are operator-only (they make the bot
        # talk repeatedly); a bare $trivia is one question anyone may ask.
        if arg in ("on", "start", "off", "stop"):
            if not is_irc_owner(nick, self, irc_ch):
                self._notice(nick, "Starting or stopping a trivia session is for operators.")
                return True
            if arg in ("on", "start"):
                self._trivia_channel = irc_ch
                self._trivia_ask(irc_ch, self._trivia.start())
            else:
                self._trivia_cancel_timer()
                msg = self._trivia.stop()
                self._trivia_channel = ""
                self._queue(irc_ch, msg)
            return True
        # bare $trivia — one question, no session, no leaderboard
        if self._trivia.running:
            self._notice(nick, "A trivia session is already running here — just answer.")
            return True
        self._trivia_channel = irc_ch
        self._trivia_ask(irc_ch, self._trivia.one_off())
        return True

    MEDIA_CMDS = ("image", "img", "picture", "voice", "say", "tts", "speak")

    def try_media_command(self, irc_ch: str, nick: str, text: str) -> bool:
        """`$image <prompt>` / `$voice <text>` — Pollinations media generation.

        IRC can't display images or play audio inline, so we post a URL that
        Pollinations serves on-demand. Clients that preview URLs (Kiwi, many
        others) render the image inline; clicking the audio link plays it in
        the browser. Discord bridge will embed the image automatically when
        the URL reaches the mirrored channel. Keyless Pollinations works for
        both media endpoints; a POLLINATIONS_API_KEY lifts the rate limit if
        the owner adds one.
        """
        import urllib.parse as _up
        if not text.startswith(config.PREFIX):
            return False
        body = text[len(config.PREFIX):].strip()
        parts = body.split(None, 1)
        if not parts or parts[0].lower() not in self.MEDIA_CMDS:
            return False
        cmd = parts[0].lower()
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            if cmd in ("image", "img", "picture"):
                self._notice(nick, f"{config.PREFIX}image <prompt> — generates an image URL")
            else:
                self._notice(nick, f"{config.PREFIX}voice <text> — generates a TTS audio URL")
            return True
        # Trim overly long prompts; URLs over ~400 chars get truncated by some
        # IRC bridges and the Pollinations URL still has to carry the query.
        prompt = prompt[:380]
        encoded = _up.quote(prompt, safe="")
        if cmd in ("image", "img", "picture"):
            seed = random.randint(1, 999_999_999)
            model = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip() or "flux"
            url = (f"https://image.pollinations.ai/prompt/{encoded}"
                   f"?nologo=true&width=768&height=768&seed={seed}&model={model}")
            self._queue(irc_ch, f"{nick}: {url}")
        else:  # voice / say / tts / speak
            voice = os.getenv("POLLINATIONS_VOICE", "alloy").strip() or "alloy"
            url = (f"https://text.pollinations.ai/{encoded}"
                   f"?model=openai-audio&voice={voice}")
            self._queue(irc_ch, f"{nick}: {url}")
        return True

    AI_CMDS = ("ai", "aion", "aioff")

    def try_ai_toggle_command(self, irc_ch: str, nick: str, text: str) -> bool:
        """`$AI on | off | status` — per-room AI toggle (feature #25).

        Operators only. Default: AI on for home rooms, off everywhere else.
        State persisted to ai_room_state.json alongside the bot. Keeps !!join'd
        guest rooms quiet unless the owner opts in.
        """
        from shared_cmds import is_irc_owner
        if not text.startswith(config.PREFIX):
            return False
        body = text[len(config.PREFIX):].strip()
        parts = body.split()
        if not parts or parts[0].lower() not in self.AI_CMDS:
            return False
        if not is_irc_owner(nick, self, irc_ch):
            return True                              # silent for non-ops
        cmd = parts[0].lower()
        arg = parts[1].lower() if len(parts) > 1 else ""
        ch = irc_ch.lower()
        if cmd == "aion" or arg == "on":
            self._ai_disabled_rooms.discard(ch)
            self._save_ai_state()
            self._notice(nick, f"AI responses in {irc_ch}: \x02ENABLED\x02")
            return True
        if cmd == "aioff" or arg == "off":
            self._ai_disabled_rooms.add(ch)
            self._save_ai_state()
            self._notice(nick, f"AI responses in {irc_ch}: \x02DISABLED\x02")
            return True
        if arg == "status" or not arg:
            is_on = ch not in self._ai_disabled_rooms
            self._notice(nick, f"AI in {irc_ch}: {'ON' if is_on else 'OFF'}. "
                               f"Toggle with {config.PREFIX}AI on | off.")
            return True
        self._notice(nick, f"{config.PREFIX}AI on  ·  {config.PREFIX}AI off  ·  "
                           f"{config.PREFIX}AI status")
        return True

    def _save_ai_state(self) -> None:
        try:
            import json as _json
            p = pathlib.Path(__file__).parent.parent / "ai_room_state.json"
            p.write_text(_json.dumps({"disabled": sorted(self._ai_disabled_rooms)}))
        except Exception as e:            # noqa: BLE001
            print(f"[irc_bridge] AI state save failed: {e}", flush=True)

    def try_nsfw_command(self, irc_ch: str, nick: str, text: str) -> bool:
        """Adult mode: opt-in, disclosed in the topic, consent on both sides.
        Only runs in channels; a PM cannot make a room adult."""
        from shared_cmds import is_irc_owner
        body = text[len(config.PREFIX):] if text.startswith(config.PREFIX) else ""
        parts = body.split()
        if not parts or parts[0].lower() not in self.NSFW_CMDS:
            return False
        cmd = parts[0].lower()
        arg = parts[1].lower() if len(parts) > 1 else ""

        # $nsfw on|off — operators only, and it changes the room's topic, so it
        # is the most gated of the lot.
        if cmd == "nsfw":
            if not is_irc_owner(nick, self, irc_ch):
                return True                              # silent for non-ops
            if arg not in ("on", "off"):
                self._notice(nick, f"{config.PREFIX}nsfw on  ·  {config.PREFIX}nsfw off")
                return True
            cur = self.get_topic(irc_ch) or ""
            if arg == "on":
                self._raw(f"TOPIC {irc_ch} :{self._nsfw.topic_with_notice(cur)}")
                self._notice(nick, "Adult mode on — the topic now says so, so everyone "
                                   "entering is told. Users opt in with $age18 yes / $consent on.")
            else:
                self._raw(f"TOPIC {irc_ch} :{self._nsfw.topic_without_notice(cur)}")
                self._notice(nick, "Adult mode off — disclosure removed from the topic.")
            return True

        # The opt-OUT. Entering the disclosed room is the agreement, so there is
        # no opt-in step; this is the "leave me out" that always works.
        if cmd == "boundaries":
            if arg in ("off", "back", "on"):
                self._nsfw.opt_in(nick)
                self._notice(nick, "Welcome back — you can be involved again.")
            else:
                self._nsfw.opt_out(nick)
                self._notice(nick, "Done — I won't involve you. $boundaries off to change your mind.")
            return True

        # An action line. The gate lives in nsfw.line(); we only route.
        target = parts[1] if len(parts) > 1 else ""
        line, refusal = self._nsfw.line(cmd, irc_ch, nick, target)
        if refusal:
            self._notice(nick, refusal)
        elif line:
            # Into the room (it is roleplay for the room), subject to the same
            # mod-only-speech gate as everything else.
            self._queue(irc_ch, line)
        return True

    FOLLOW_CMDS = ("follow", "unfollow", "following", "part", "leave")

    def try_follow_command(self, irc_ch: str, nick: str, text: str) -> bool:
        """$follow / $unfollow / $following — steer which rooms Luna sits in.

        Operators only. Making a bot join arbitrary rooms is a way to point it
        wherever you like, so this is gated the same way $mod is, and it acts
        only on rooms the caller names — never on rooms it went looking for.
        """
        from shared_cmds import is_irc_owner
        body = text[len(config.PREFIX):] if text.startswith(config.PREFIX) else ""
        parts = body.split()
        if not parts or parts[0].lower() not in self.FOLLOW_CMDS:
            return False
        cmd = parts[0].lower()
        if not is_irc_owner(nick, self, irc_ch):
            return True                              # silently ignore non-ops, as $mod does
        if cmd == "following":
            rooms = ", ".join(sorted(self._follow)) or "(none)"
            state = "on" if self._follow_on else "off (IRC_FOLLOW is not set)"
            self._notice(nick, f"In (followed): {rooms}. Following is {state}. "
                               f"{config.PREFIX}part #room to leave one.")
            return True
        if len(parts) < 2 or not parts[1].lstrip("#"):
            self._notice(nick, f"{config.PREFIX}{cmd} #room")
            return True
        room = parts[1]
        r = room if room.startswith("#") else f"#{room}"
        # Leaving (unfollow/part/leave) always works — no need for follow-mode —
        # because "get her out of this room" should never depend on a feature
        # flag. Only JOINING via $follow needs follow-mode on.
        if cmd in ("unfollow", "part", "leave"):
            if self.follow_remove(room):
                self._notice(nick, f"Left {r}.")
            else:
                self._notice(nick, f"{r} is a bridged relay room — leaving it would "
                                   f"kill the relay, so I keep it. Unbridge it first if you mean it.")
            return True
        # cmd == "follow"
        if not self._follow_on:
            self._notice(nick, "Following is off — set IRC_FOLLOW to turn it on.")
            return True
        self.follow_add(room)
        self._notice(nick, f"Following {r}.")
        return True

    def try_memory_command(self, irc_ch: str, nick: str, text: str) -> bool:
        """Handle a memory command from IRC. True if we took it."""
        if self.loop is None or not self.loop.is_running():
            return False
        body = text[len(config.PREFIX):]
        if body.startswith(config.PREFIX):
            return False                      # another bot's doubled prefix
        parts = body.split(None, 1)
        if not parts:
            return False
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        if cmd not in self.MEMORY_CMDS:
            return False

        cog = None
        try:
            cog = self.bot.get_cog("Memory") if self.bot else None
        except Exception:                     # noqa: BLE001
            cog = None
        if cog is None:
            # Say so. A command that exists, is advertised, and answers with
            # silence is the failure this project keeps shipping.
            self._notice(nick, "My memory is not loaded right now — try again shortly.")
            return True

        # One at a time per person: each of these scans thousands of messages.
        now = time.time()
        if now - self._memory_cooldown.get(nick.lower(), 0.0) < _MEMORY_COOLDOWN:
            self._notice(nick, "Give me a moment — I am still reading.")
            return True
        self._memory_cooldown[nick.lower()] = now

        if cmd in ("find", "search"):
            coro = cog.irc_find(args)
        elif cmd in ("tell", "memo"):
            bits = args.split(None, 1)
            if len(bits) < 2:
                self._notice(nick, f"{config.PREFIX}tell <nick> <message>")
                return True
            coro = cog.irc_tell(nick, bits[0], bits[1])
        elif cmd in ("stats", "activity"):
            coro = cog.irc_stats()
        elif cmd == "seen":
            coro = cog.irc_seen(args)
        elif cmd == "mood":
            coro = cog.irc_mood()
        elif cmd == "quote":
            coro = cog.irc_quote(args.split()[0] if args else "")
        else:                                  # onthisday / rewind / backthen
            days = 7
            if args.split() and args.split()[0].lstrip("-").isdigit():
                days = max(1, min(60, int(args.split()[0])))
            coro = cog.irc_rewind(days)

        return self._answer_from_discord(irc_ch, nick, coro, cmd)

    def _answer_from_discord(self, irc_ch: str, nick: str, coro, what: str) -> bool:
        """Run a coroutine on Discord's loop, put the answer back in the room."""
        def _done(fut):
            try:
                reply = fut.result()
            except Exception as e:             # noqa: BLE001
                # Never silent: the person is waiting and would otherwise read
                # this as the bot being broken.
                print(f"[irc_bridge] {what} failed: {e}")
                self._queue(irc_ch, f"{nick}: that did not work — {str(e)[:120]}")
                return
            if not reply:
                self._queue(irc_ch, f"{nick}: nothing to show.")
                return
            for line in _wrap(f"{nick}: {reply}")[:3]:
                self._queue(irc_ch, line)

        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
            fut.add_done_callback(_done)
            return True
        except Exception as e:                 # noqa: BLE001
            print(f"[irc_bridge] {what} dispatch failed: {e}")
            return False

    def _reclaim_nick(self) -> None:
        """Re-identify, evict whatever holds our nick, take it back, rejoin.

        Called both on a detected force-rename and from the watchdog, because
        enforcement is not the only way to lose a nick.
        """
        if not config.IRC_NICKSERV_PASS:
            return
        try:
            self._raw(f"PRIVMSG NickServ :IDENTIFY {config.IRC_NICKSERV_ACCOUNT} {config.IRC_NICKSERV_PASS}")
            self._raw(f"PRIVMSG NickServ :GHOST {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
            self._raw(f"PRIVMSG NickServ :RELEASE {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
            if self._nick_allowed(): self._raw(f"NICK {config.IRC_NICK}")
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

    def _deliver_command_reply(self, cmd: str, nick: str, target: str, reply: str) -> None:
        """A fun/social command's answer goes to the ROOM so everyone sees it
        ("$hug nora"); everything else is a private NOTICE to the asker (a help
        listing is for them alone). The bug the owner hit was that ALL replies
        went out as notices, so only the person who typed $hug ever saw it."""
        from shared_cmds import SharedCommands
        public = cmd in getattr(SharedCommands, "PUBLIC_CMDS", set())
        if public and str(target).startswith("#"):
            self._queue(target, reply[:400])       # to the room (speak-gate still applies)
        else:
            for chunk in _wrap(reply):
                self._notice(nick, chunk)

    def _notice(self, nick: str, text: str) -> None:
        self._queue(nick, text, "NOTICE")

    def _may_speak(self, irc_ch: str, verb: str) -> bool:
        """Whether we are allowed to put this line in this channel.

        Only gates channel PRIVMSGs. A NOTICE (a reply to one person) and a
        message to a nick always pass — answering someone who spoke to us is not
        "messaging a room". Bridged/home rooms always pass, because refusing to
        relay there would be a silent outage, the worst failure this bridge has.
        Everywhere else, speak only if we hold ops.
        """
        if not self._speak_only_where_op:
            return True
        if verb != "PRIVMSG" or not str(irc_ch).startswith("#"):
            return True
        if self._is_home_room(irc_ch):
            return True
        try:
            if self.has_prefix(irc_ch, self._nick):
                return True
        except Exception:                        # noqa: BLE001
            return True                          # unknown -> do not gag the bot
        # Not a mod here. Say so ONCE (a dropped line that logs nothing looks
        # like the bot is broken), then stay quiet in this room.
        if irc_ch.lower() not in self._silent_logged:
            self._silent_logged.add(irc_ch.lower())
            print(f"[irc_bridge] Not opped in {irc_ch} — listening only (mod-only speech is on).")
        return False

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
            if not self._may_speak(irc_ch, verb):
                continue
            try:
                self._raw(f"{verb} {irc_ch} :{text}")
            except Exception as e:
                print(f"[irc_bridge] Sender error: {e}")

    # ── Reconnect loop ────────────────────────────────────────────────────────

    def _note_refusal(self, exc) -> str:
        """Update the refusal counter and say what to do: 'reset' (a normal drop
        after a real session), 'slowdown' (back off — could be a blip), or 'exit'
        (this address is blocked; give up so a fresh runner is drawn).

        Split out of the reconnect loop so the escalation can be tested without
        opening a socket or killing the process."""
        if self._ever_registered or not _refused_at_the_door(exc):
            self._refusals = 0
            return "reset"
        self._refusals += 1
        if self._refusals == _REFUSALS_BEFORE_SLOWDOWN:
            print("[irc_bridge] The server is closing the connection before "
                  "registration — it is refusing this ADDRESS, not this bot. "
                  "Backing off; a fresh runner gets a fresh address.")
        if self._refusals >= _REFUSALS_BEFORE_EXIT:
            print(f"[irc_bridge] Refused {self._refusals} times without ever "
                  "registering — this address is blocked. Exiting so the next "
                  "run picks up a different one.")
            return "exit"
        if self._refusals >= _REFUSALS_BEFORE_SLOWDOWN:
            return "slowdown"
        return "reset"

    def _run_forever(self):
        delay = _RECONNECT_DELAY_MIN
        while self._running:
            self._force_reconnect = False
            self._ever_registered = False
            try:
                self._connect_and_loop()
                delay = _RECONNECT_DELAY_MIN   # reset backoff on clean exit
            except Exception as e:
                print(f"[irc_bridge] Disconnected: {e}")
                # Refused at the door, or dropped after a real session?
                #
                # Measured on 2026-09-13: one run made 154 connection attempts in
                # five hours and 121 of them ended in "TLS/SSL connection has been
                # closed (EOF)" — the server closing the handshake because it does
                # not accept this address. Luna was absent from the room the whole
                # time while the job looked perfectly healthy.
                #
                # Retrying every two minutes against a host that is dropping us
                # achieves nothing and is exactly the pattern that cost this
                # project a GitHub account: it reads as an attack. So a failure
                # that arrives BEFORE we were ever registered is treated as an
                # address-level refusal and backed off hard, while a drop after a
                # real session keeps the fast retry it needs.
                action = self._note_refusal(e)
                if action == "slowdown":
                    delay = _REFUSED_RETRY
                elif action == "exit":
                    # The address is blocked for this runner's whole life, so a
                    # fresh run with a fresh address is the only cure. Hard exit
                    # (ends the process and the job) the way Dracula's does.
                    # Dispatch a successor BEFORE the hard exit so GitHub's
                    # throttled cron doesn't leave the room bot-less for hours
                    # — observed live 2026-10-07, Dracula's equivalent branch
                    # fired and nothing restarted for 4+ hours before this fix.
                    try:
                        from utils.self_restart import dispatch_successor
                        dispatch_successor("blocked-address-exit", workflow_file="luna.yml")
                    except Exception as _derr:  # noqa: BLE001
                        print(f"[irc_bridge] dispatch on exit failed: {_derr}", flush=True)
                    import os
                    os._exit(1)
            if not self._running:
                break
            self._connected = False
            if self._force_reconnect:
                print("[irc_bridge] Force-reconnect — reconnecting immediately...")
                time.sleep(2)
            else:
                print(f"[irc_bridge] Reconnecting in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, _REFUSED_RETRY if self._refusals
                            >= _REFUSALS_BEFORE_SLOWDOWN else _RECONNECT_DELAY_MAX)

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
        self._nick_tries = 0          # distinct fallbacks used on THIS attempt
        self._connect_time = time.time()
        # server-time marks replayed +H history with when it was ORIGINALLY
        # said. Without it Luna re-relays the whole backlog to Discord on every
        # six-hour restart, which is a wall of duplicated conversation.
        self._raw("CAP REQ :server-time")
        self._raw("CAP END")
        # NOT rate-limited: this is registration, not a nick change — the server
        # has not acknowledged us yet, and refusing it would leave the bot unable
        # to connect at all rather than merely wearing the wrong name.
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
                    and self._nick.lower() != self._wanted_nick.lower()
                    and time.time() - self._last_reclaim > _NICK_RECLAIM_SECS):
                self._last_reclaim = time.time()
                print(f"[irc_bridge] Still on {self._nick}, wanted {self._wanted_nick} — retrying reclaim.")
                self._revert_nick("wearing a name we did not choose")
            # Rotation rides the same loop rather than a separate timer: this
            # runs only while the link is alive, so a dead connection cannot
            # keep renaming into the void.
            self._rotation_tick()
            # Keep the trust list current. Asked more often until it has ever
            # arrived, because an empty list means nobody is exempt — and being
            # wrong in that direction is what removed a trusted user.
            if (self._connected and TRUST_CHANNEL
                    and time.time() - self._trust_asked
                    > (_TRUST_REFRESH if self._trust_loaded else _TRUST_RETRY)):
                self._trust_asked = time.time()
                self._trusted_pending = set()
                self._raw(f"PRIVMSG ChanServ :FLAGS {TRUST_CHANNEL}")
            # Leave rooms that have gone quiet. Parts only; never rejoins on a
            # clock (that is the churn that gets bots killed).
            self._follow_sweep()
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
        # An INVITE is the polite, event-driven way back into a room — the
        # opposite of clock-driven rejoin churn. Accept it only when following
        # is on, and only from someone we trust, so the bot cannot be dragged
        # into a stranger's room as a prank.
        inv = re.match(r"^:([^!]+)!\S+\s+INVITE\s+\S+\s+:?(#\S+)", line, re.I)
        if inv and self._follow_on:
            who, room = inv.group(1), inv.group(2)
            if self.is_trusted(who) or self.is_one_of_ours(who):
                print(f"[irc_bridge] {who} invited me to {room} — following.")
                self.follow_add(room)
            return

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

        # ChanServ's FLAGS listing, which is the trust list.
        cs = re.match(r"^:ChanServ!\S+\s+NOTICE\s+\S+\s+:(.*)$", line, re.I)
        if cs:
            body = cs.group(1).strip()
            row = _TRUST_ROW.match(body)
            if row:
                # +V is autovoice, which is what being a regular means here, so
                # the two stay in step. +F is a founder.
                if "V" in row.group(2) or "F" in row.group(2):
                    self._trusted_pending.add(row.group(1).lower())
                return
            if _TRUST_END.search(body):
                self._trusted = set(self._trusted_pending)
                self._trusted_pending = set()
                self._trust_loaded = True
                print(f"[irc_bridge] Trust list: {len(self._trusted)} entries "
                      f"from {TRUST_CHANNEL}.")
                return
            # anything else from ChanServ falls through to the normal handling

        num = numeric(line)

        # 005 ISUPPORT — the server states its own limits on connect, and we
        # were guessing at every one of them: a hardcoded 30 for the nick
        # length, a hardcoded 400 for how much text fits in a line. Guessing
        # low wastes room; guessing high gets the line SILENTLY truncated,
        # which is how $help lost its tail for weeks. Ask, do not assume.
        if num == "005":
            for tok in line.split()[3:]:
                if tok.startswith(":"):
                    break                      # the human-readable tail
                key, _, val = tok.partition("=")
                self._isupport[key.upper()] = val
            return

        # 001 = registered
        if num == "001":
            current_nick = config.IRC_NICK
            self._ever_registered = True
            self._refusals = 0          # the address is fine; forget the backoff
            print(f"[irc_bridge] Registered as {current_nick}")
            if config.IRC_NICKSERV_PASS:
                self._raw(f"PRIVMSG NickServ :IDENTIFY {config.IRC_NICKSERV_ACCOUNT} {config.IRC_NICKSERV_PASS}")
                time.sleep(1)
                # Only fight for the nick if we do not already have it.
                #
                # This used to GHOST, RELEASE and NICK on every single connect,
                # including the ordinary case where registration had just handed
                # us Luna1 — three nick-related commands each time, and a
                # reconnect loop turns that into the nick flooding this network
                # kills for. If we are already Luna1, there is nothing to reclaim.
                if current_nick.lower() != config.IRC_NICK.lower():
                    print(f"[irc_bridge] On fallback {current_nick} — reclaiming "
                          f"{config.IRC_NICK}.")
                    # Ghost the stale session still holding it (a previous crash,
                    # or the six-hourly handover where the outgoing runner is
                    # still connected when this one arrives).
                    self._raw(f"PRIVMSG NickServ :GHOST {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
                    time.sleep(0.5)
                    # RELEASE before taking it back: GHOST ends the other session
                    # but leaves NickServ HOLDING the nick, and a NICK into that
                    # hold is refused — leaving us on the fallback, unidentified,
                    # which is exactly what enforcement renames to Guest####.
                    self._raw(f"PRIVMSG NickServ :RELEASE {config.IRC_NICK} {config.IRC_NICKSERV_PASS}")
                    time.sleep(0.5)
                    if self._nick_allowed(): self._raw(f"NICK {config.IRC_NICK}")
                    time.sleep(0.3)
            # +g (callerid) refuses private messages from anyone not on our
            # accept list, and +R from anyone unregistered. Both are enforced by
            # the SERVER, so a DM flood never reaches our socket and cannot get
            # us killed for excess flood — a bot-side ignore would still have to
            # read every line first. Verified against this network: services are
            # exempt, so ChanServ and NickServ still get through.
            # +i keeps us out of unsolicited /who scans.
            # +I hides our channel list from other people's WHOIS. No -x: +x is
            # the cloak, and removing it would expose the runner's real address.
            self._raw(f"MODE {config.IRC_NICK} +gIiR")
            self._raw("PRIVMSG HostServ :ON")     # reapply the vhost
            # Re-join ALL mapped IRC channels
            for ch in self.all_channels():
                self._raw(f"JOIN {ch}")
            # And the followed rooms, if following is on. Same JOIN, but tracked
            # so the idle sweep can later part them.
            if self._follow_on:
                for ch in self._follow:
                    self._raw(f"JOIN {ch}")
                    self._last_activity[ch.lower()] = time.time()
            # Shadow rooms: silent observer for Dracula's benefit. JOIN only,
            # never speak. Follow-sweep is told to leave them alone.
            # Diagnostic log: owner reported "I don't see Luna in those rooms"
            # 2026-10-06; previous run's log was already purged so we could
            # not verify. Log what we are trying to join so next session has
            # evidence either way.
            if self._shadow:
                print(f"[irc_bridge] Shadow rooms: joining {len(self._shadow)} "
                      f"silently → {sorted(self._shadow)}", flush=True)
            else:
                print("[irc_bridge] Shadow rooms: none configured (LUNA_SHADOW_ROOMS empty).",
                      flush=True)
            for sh in self._shadow:
                self._raw(f"JOIN {sh}")
                self._last_activity[sh.lower()] = time.time()
            # Phase 2: team up with the other bot on the trust channel. Delayed
            # so it does not fight for pacer budget with the opening JOINs.
            try:
                threading.Timer(25.0, self._start_trust_teamwork).start()
            except Exception as e:  # noqa: BLE001
                print(f"[irc_bridge] trust teamwork start failed: {e}")
            # Disclose the owner's declared-adult rooms in their topic, so
            # "entering is the agreement" still holds even though no one ran
            # $nsfw on. Best-effort and delayed so ChanServ autoop lands first;
            # topic_with_notice() keeps whatever the topic already said and does
            # nothing if the marker is already there.
            for ch in self.all_channels():
                if self._nsfw.is_declared_adult(ch):
                    def _disclose(c=ch):
                        cur = self.get_topic(c) or ""
                        from utils.nsfw import TOPIC_MARK
                        if TOPIC_MARK not in cur:
                            self._raw(f"TOPIC {c} :{self._nsfw.topic_with_notice(cur)}")
                    threading.Timer(6.0, _disclose).start()
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

        # A room that will not let us in.
        #
        # The owner asked why Luna1 was not in #batcave. It was configured all
        # along — EXTRA_BRIDGES maps it and all_channels() joins it — but the room
        # is invite-only, so the JOIN was REFUSED and nothing here noticed. There
        # was no handling for 473/474/475 at all, so Luna sat outside a room it was
        # told to bridge, silently, for as long as the flag was set. Dracula grew
        # this same recovery after the same thing happened to it.
        #
        #   473 invite-only  -> ask ChanServ to invite us
        #   474 banned       -> ask ChanServ to clear bans matching us
        #   475 key set      -> nothing we can do; say so
        #
        # Three attempts per room per connection. Asking a service in a loop when
        # it is going to refuse is a flood, not persistence.
        if num in ("473", "474", "475"):
            parts = line.split()
            ch = parts[3] if len(parts) > 3 else ""
            if ch.startswith("#"):
                tried = self._locked_out.get(ch.lower(), 0) + 1
                self._locked_out[ch.lower()] = tried
                why = {"473": "invite-only", "474": "we are banned",
                       "475": "a key is set"}[num]
                if tried > 3:
                    if tried == 4:
                        print(f"[irc_bridge] Still shut out of {ch} ({why}) after three "
                              "attempts — a human needs to let me in.")
                    return
                print(f"[irc_bridge] {ch} refused me ({why}) — asking ChanServ "
                      f"(attempt {tried}).")
                if num in ("473", "474"):
                    self._raw(f"PRIVMSG ChanServ :{'INVITE' if num == '473' else 'UNBAN'} {ch}")
                    threading.Timer(4.0, lambda c=ch: self._raw(f"JOIN {c}")).start()
            return

        # Nick in use (433) — take a DISTINCT fallback, then ghost + reclaim.
        if num == "433":
            # A rotation target that is taken is a non-event: we still hold the
            # name we have. Taking a "_" fallback here would rename us for no
            # reason and spend one of the hour's changes doing it.
            if self._pending_rotation:
                # Taken. Put a number on the end and ask once more — that is the
                # point of the numbering. Exactly ONE retry: a loop here is a
                # nick flood, the one thing this must never become. It is not
                # counted against the hourly cap either, being the same rotation.
                retry = ("" if self._rotation_numbered
                         else self._next_rotation_name(
                             True, self._pending_rotation.rstrip("0123456789")))
                if retry:
                    print(f"[irc_bridge] {self._pending_rotation} is taken — trying {retry}.")
                    self._rotation_numbered = True
                    self._pending_rotation = retry
                    self._raw(f"NICK {retry}")
                else:
                    print(f"[irc_bridge] {self._pending_rotation} is taken — staying as {self._nick}.")
                    self._pending_rotation = ""
                return
            self._nick_tries = getattr(self, "_nick_tries", 0) + 1
            if self._nick_tries > 4:
                print("[irc_bridge] Nick and every fallback are taken — giving up on this "
                      "attempt rather than cycling nicks, which is what gets a bot killed. "
                      "The reconnect backoff will try again.")
                return
            suffix = "_" * self._nick_tries
            fallback = f"{config.IRC_NICK}{suffix}"
            print(f"[irc_bridge] Nick in use — trying {fallback} "
                  f"(attempt {self._nick_tries}), will GHOST and reclaim after auth")
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
                if (self._pending_rotation
                        and new_nick.lower() == self._pending_rotation.lower()):
                    # It took. This is the name we mean to wear now, so the
                    # reclaim check must not read it as a nick we lost.
                    self._wanted_nick = new_nick
                    self._pending_rotation = ""
                    print(f"[irc_bridge] Now wearing {new_nick}.")
                elif new_nick.lower() != self._wanted_nick.lower():
                    # A forced rename to Guest means the name we had just taken
                    # belongs to a registered account. Strike it off the list.
                    blamed = self._wanted_nick.lower()
                    if (new_nick.lower().startswith("guest")
                            and blamed != config.IRC_NICK.lower()
                            and blamed not in self._unusable_names):
                        self._unusable_names.add(blamed)
                        print(f"[irc_bridge] {self._wanted_nick} is registered to somebody — "
                              f"dropping it ({len(self._unusable_names)} dropped so far).")
                    print(f"[irc_bridge] Force-renamed to {new_nick} — reclaiming.")
                    self._revert_nick("NickServ enforced a rename")
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
            # Against the name we are WEARING, not the configured one. After a
            # rotation those differ, and Luna would have started relaying and
            # answering her own output.
            if nick.lower() in (self._nick.lower(), config.IRC_NICK.lower(),
                                f"{config.IRC_NICK}_".lower()):
                return
            # Phase 2: trust-channel inter-bot comms. Short-circuit BEFORE any
            # moderation/relay path — the trust channel is for bots and flag
            # admins, nothing it carries should land in Discord or trigger
            # moderation. Replay-filtered already above.
            if TRUST_CHANNEL and target.lower() == TRUST_CHANNEL.lower():
                try:
                    self._handle_trust_line(nick, message)
                except Exception as e:  # noqa: BLE001
                    print(f"[irc_bridge] trust line error: {e}")
                return
            # Replayed channel history is not new conversation: relaying it
            # would repost the backlog to Discord on every restart, and
            # answering it would have Luna reply to questions from hours ago.
            if self._is_replay(getattr(self, "_last_tags", {})):
                return

            # Keep the live tail of the room for grounding Luna's answers. Every
            # line, not only ones aimed at her, because "who was talking" is a
            # question about everyone else.
            buf = self._recent.setdefault(target.lower(), deque(maxlen=_RECENT_LINES))
            buf.append((nick, message[:300]))
            self._last_activity[target.lower()] = time.time()
            # Phase 3: per-user memory. Capture from any room this bot is in
            # (the operator is their delegate there, so lines are already in
            # their reach), tagged by room. The trust channel is excluded —
            # it is the inter-bot protocol, not people talking. The prompt
            # does NOT reveal the room, so a reference reads as remembered
            # content, not "I heard you in room X".
            try:
                if target.startswith("#") and target.lower() != (TRUST_CHANNEL or "").lower():
                    self._remember_line(nick, message, room=target)
            except Exception as e:  # noqa: BLE001 — memory must never break chat
                print(f"[irc_bridge] memory error: {e}")
            # A live trivia line? Check it before anything else consumes the msg.
            if self._trivia_channel and not message.startswith(config.PREFIX):
                self.check_trivia_answer(target, nick, message)

            # ── A room that is not ours: listen only ──
            # Luna is a guest in the rooms she watches. She never speaks or
            # moderates there — that is someone else's channel, and acting in it
            # would get her banned from the very place worth watching. She only
            # listens for our own rooms being advertised, which is how the last
            # raid was assembled before any of it arrived.
            if target.startswith("#") and target.lower() not in self._home_rooms():
                try:
                    heard = self.watch.hear(
                        target, nick, message,
                        trusted=self.moderator._exempt(nick, target),
                        abusive=bool(self.moderator._word_hit(message)),
                    )
                    if heard and heard["level"] == "alert":
                        where = ", ".join(self.watch.seen_in(nick)) or target
                        for home in self._home_rooms():
                            self._queue(
                                home,
                                f"\x0304[WATCH]\x03 \x02{nick}\x02 is {heard['why']} "
                                f"in {where}. They are not here yet.")
                        print(f"[watch] {nick} {heard['why']} in {target}")
                except Exception as e:  # noqa: BLE001
                    print(f"[irc_bridge] watch error: {e}")
                # Owner-set 2026-10-07: pipe watcher-room lines to a Discord
                # ADMIN channel so the owner can scan them from mobile without
                # being in IRC. Rate-limited per-source-room so one busy room
                # cannot blow Discord's limits. The trust-channel is for bots;
                # this is for the human.
                try:
                    self._admin_relay(target, nick, message)
                except Exception as e:  # noqa: BLE001
                    print(f"[irc_bridge] admin relay error: {e}")
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
                    # The memory commands come FIRST, because they cannot answer
                    # from this thread: they read Discord history, the call is
                    # async, and blocking the IRC reader for it would stall the
                    # relay for everyone in the room. They go to Discord's loop
                    # and the answer arrives through the queue, exactly as $ai
                    # already does.
                    if self.try_trivia_command(target, nick, message):
                        return
                    if self.try_ai_toggle_command(target, nick, message):
                        return
                    if self.try_media_command(target, nick, message):
                        return
                    if self.try_nsfw_command(target, nick, message):
                        return
                    if self.try_follow_command(target, nick, message):
                        return
                    if self.try_memory_command(target, nick, message):
                        return
                    try:
                        from shared_cmds import SharedCommands
                        sc = SharedCommands.get(self.bot, self)
                        reply = sc.dispatch_irc(nick, message, target)
                        if reply:
                            cmd = message[len(config.PREFIX):].split()[0].lower() \
                                if message.startswith(config.PREFIX) else ""
                            self._deliver_command_reply(cmd, nick, target, str(reply))
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
                            f"**[{_room_label(target)}]** `{nick}`: {message}",
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
        if m and m.group(1).lower() == (self._nick or "").lower():
            # In at last: forget the refusals so a later one starts fresh.
            self._locked_out.pop(m.group(2).lower().lstrip(":"), None)
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
        # By host as well as by name. Matching on the nick alone meant a bot that
        # renamed — which is now something they do on purpose — would show up on
        # StarAlign as a stranger.
        is_bot = (bool(batbot_nick) and nick.strip().lower() == batbot_nick) \
            or self.is_one_of_ours(nick)
        asyncio.run_coroutine_threadsafe(
            relay_to_staralign(
                username="bot" if is_bot else nick,
                text=text,
                kind="bot" if is_bot else "user",
            ),
            self.loop,
        )

    # ── Admin relay (owner-visible mirror of non-home room chatter) ────────
    def _admin_relay(self, source_room: str, nick: str, message: str) -> None:
        """Pipe a watcher-room line to a dedicated Discord admin channel so
        the owner can scan non-home IRC traffic from mobile. Rate-limited
        per-source-room (6 lines / 60s per room): more than that is unreadable
        on mobile and would blow Discord's rate limits with a busy recruit
        room. Owner-set 2026-10-07; dormant if LUNA_ADMIN_CHANNEL is empty."""
        admin_ch = (getattr(config, "LUNA_ADMIN_CHANNEL", "") or "").strip()
        if not admin_ch:
            return                             # feature off
        now = time.time()
        history = getattr(self, "_admin_relay_history", None)
        if history is None:
            history = {}                       # source_room(lower) -> [ts, ts]
            self._admin_relay_history = history
        key = (source_room or "").lower()
        recent = [t for t in history.get(key, []) if now - t < 60.0]
        if len(recent) >= 6:
            return                             # busy room, drop silently
        recent.append(now)
        history[key] = recent
        # Keep the map bounded so an unbounded number of distinct rooms
        # cannot grow it forever.
        if len(history) > 50:
            for k in list(history)[:25]:
                history.pop(k, None)
        # Discord markdown-safe. We truncate to 400 chars because long IRC
        # lines (quoted blocks, pastes) are unreadable on mobile anyway.
        text = f"[`{source_room}`] **{nick}**: {(message or '')[:400]}"
        try:
            self._relay_to_discord(text, discord_channel=admin_ch)
        except Exception as e:                 # noqa: BLE001
            print(f"[irc_bridge] admin relay post error: {e}")

    # ── Discord relay ─────────────────────────────────────────────────────────

    def _relay_to_discord(self, text: str, system: bool = False,
                          discord_channel: str = None):
        """Thread-safe: post a message to the correct Discord bridge channel."""
        if self.loop is None or not self.loop.is_running():
            # The other silent drop on this path. IRC runs on its own thread and
            # hands work to Discord's event loop; if that loop is missing or has
            # stopped, every relayed line was discarded without a word, which
            # looks identical to "the bridge is fine but nobody is talking".
            # Say it once — repeating it per message would bury the room's log.
            if not self._warned_no_loop:
                self._warned_no_loop = True
                print("[irc_bridge] NO EVENT LOOP — nothing is reaching Discord. "
                      "The bridge was started before the Discord client was ready, "
                      "or the client has stopped.")
            return
        self._warned_no_loop = False
        asyncio.run_coroutine_threadsafe(
            self._post_discord(text, discord_channel),
            self.loop,
        )

    async def _post_discord(self, text: str, discord_channel: str = None):
        channel = self._get_bridge_channel(discord_channel)
        if channel is None:
            # Do NOT drop this silently. A missing Discord channel used to make
            # a whole room's relay vanish with no error anywhere: #batcave went
            # across because a channel called "batcave" happened to exist, and
            # the emoji room did not, and nothing ever said so. Complain once
            # per target and name what is actually available.
            want = (discord_channel or getattr(config, "BRIDGE_CHANNEL", "")).lower()
            if want not in self._missing_targets:
                self._missing_targets.add(want)
                have = ", ".join(
                    sorted(c.name for g in self.bot.guilds for c in g.text_channels)
                )[:400] if self.bot else "(no guilds yet)"
                print(f"[irc_bridge] NO DISCORD CHANNEL for '{want}' — relay from that "
                      f"IRC room is going nowhere. Channels I can see: {have}")
            return
        try:
            await channel.send(text[:2000])
        except Exception as e:
            print(f"[irc_bridge] Discord send error: {e}")

    def _get_bridge_channel(self, channel_name: str = None):
        """Find the Discord channel to post into.

        Accepts a numeric channel ID as well as a name. Names are the friendly
        option and the fragile one: a room called "#🅱🅰🆃🅲🅰🆅🅴" cannot always be
        reproduced as a Discord channel name, and one character of difference
        matches nothing. An ID is ASCII, stable, and survives a rename.
        """
        name = (channel_name or getattr(config, "BRIDGE_CHANNEL", "")).lower()
        if not name:
            return None
        if name.isdigit() and self.bot:
            ch = self.bot.get_channel(int(name))
            if ch is not None:
                return ch
        for guild in (self.bot.guilds if self.bot else []):
            ch = next(
                (c for c in guild.text_channels if c.name.lower() == name),
                None,
            )
            if ch:
                return ch
        return None

    # Nick changes, rate-limited at the last possible moment.
    #
    # The owner's reason for wanting the Guest#### loop fixed at all: "i dont wanna
    # be banned cause of fast nick changes". This network kills for nick flooding,
    # and a reconnect loop that reclaims its name on every attempt is precisely
    # that shape. Guarding the individual call sites is not enough — the next one
    # written will not know — so the ceiling sits on the wire itself.
    #
    # Six in five minutes is generous for a bot that should change nick twice per
    # connect at most, and far under anything the server objects to.
    def trust_loaded(self) -> bool:
        """Whether the trust list has ever arrived. Until it has, we know
        nothing about who is exempt, and acting on that ignorance is what
        removed somebody the owner had just trusted."""
        return self._trust_loaded

    def is_trusted(self, nick: str) -> bool:
        n = str(nick or "").lower()
        if not n:
            return False
        if n in self._trusted:
            return True
        # Entries can be hostmasks rather than names — "*!*@hazel.sees.u.peek"
        # is what !!protect writes — so a regular who changes nick is still
        # covered.
        host = (self._hosts.get(n) or "")
        if host:
            ident = f"{n}!{host}".lower()
            for entry in self._trusted:
                if any(ch in entry for ch in "!@*") and fnmatch(ident, entry):
                    return True
        return False

    def is_one_of_ours(self, nick: str) -> bool:
        """One of our own bots, known by the host rather than the name.

        BATBOT_IRC_NICK is a NICK, and a nick is exactly what rotation changes.
        A vhost belongs to the connection, so Dracula@Sat.Chit.Ananda stays
        recognisable whatever it is currently called.
        """
        host = (self._hosts.get(str(nick).lower()) or "").split("@")[-1].lower()
        if not host:
            return False
        return any(host == h or host.endswith("." + h) for h in config.IRC_OUR_HOSTS)

    def nick_limit(self) -> int:
        """Longest nick this server accepts.

        From the server's own 005 when it has said, and the configured value
        until then — 005 arrives before we are anywhere near rotating, so in
        practice this is always the real number.
        """
        try:
            said = getattr(self, "_isupport", {}).get("NICKLEN", 0)
            return max(9, int(said) or config.IRC_NICK_MAXLEN)
        except (TypeError, ValueError):
            return config.IRC_NICK_MAXLEN

    def _is_home_room(self, irc_ch: str) -> bool:
        """A bridged room, or one named at boot. These are the job and are never
        auto-parted — leaving one silently would take the relay down, which is
        the kind of quiet failure that goes unnoticed for hours."""
        c = irc_ch.lower()
        with self._map_lock:
            if c in self._i2d:
                return True
        boot = {x.strip().lower() if x.strip().startswith("#") else f"#{x.strip().lower()}"
                for x in os.getenv("IRC_EXTRA_CHANNELS", "").split(",") if x.strip()}
        return c in boot

    def followed_rooms(self) -> Set[str]:
        return set(self._follow)

    def follow_add(self, irc_ch: str) -> bool:
        """Bring a room into the follow set and join it now. Event-driven only —
        an op's command or an INVITE — never a timer."""
        ch = irc_ch if irc_ch.startswith("#") else f"#{irc_ch}"
        self._follow.add(ch)
        self._last_activity[ch.lower()] = time.time()   # grace period before idle-part
        if self._connected:
            self._raw(f"JOIN {ch}")
        return True

    def _is_bridged(self, irc_ch: str) -> bool:
        """A room whose messages are relayed to Discord. Leaving one of THESE
        would take the relay down, so a manual leave refuses only these — unlike
        the idle sweep, which also spares boot rooms. Everything else can be
        left on request."""
        with self._map_lock:
            return irc_ch.lower() in self._i2d

    def follow_remove(self, irc_ch: str) -> bool:
        """Leave a room on request. Parts anything that is not a bridged relay
        room — including boot/extra rooms, which is what the owner could not get
        her out of before (the old check treated those as un-leaveable too)."""
        ch = irc_ch if irc_ch.startswith("#") else f"#{irc_ch}"
        self._follow.discard(ch)
        if self._connected and not self._is_bridged(ch):
            self._raw(f"PART {ch} :leaving on request")
            return True
        if self._is_bridged(ch):
            return False        # bridged: refused, caller explains
        return True

    def _follow_sweep(self) -> None:
        """Leave followed rooms that have gone quiet. Home rooms are exempt, and
        this only ever PARTS — the rejoin is an INVITE or a command, so there is
        no join/part churn for the network to punish."""
        if not (self._follow_on and self._connected):
            return
        now = time.time()
        for ch in list(self._follow):
            c = ch.lower()
            if self._is_home_room(ch):
                continue
            last = self._last_activity.get(c)
            # Unknown last-activity gets a grace stamp rather than an instant
            # part: a room we just joined has simply not spoken yet.
            if last is None:
                self._last_activity[c] = now
                continue
            if now - last > _FOLLOW_IDLE:
                self._raw(f"PART {ch} :quiet in here — back when there is life")
                self._follow.discard(ch)
                self._last_activity.pop(c, None)
                print(f"[irc_bridge] Left {ch}: no activity for "
                      f"{int((now - last) / 60)} min.")

    def _rotation_allowed(self) -> bool:
        now = time.time()
        self._rotations_at = [t for t in self._rotations_at if now - t < 3600]
        return len(self._rotations_at) < config.IRC_NICK_MAX_PER_HOUR

    def _revert_nick(self, why: str) -> None:
        """Back to the name the room knows, and stop rotating for now."""
        self._pending_rotation = ""
        if self._wanted_nick.lower() != config.IRC_NICK.lower():
            print(f"[irc_bridge] Reverting to {config.IRC_NICK} — {why}")
            self._wanted_nick = config.IRC_NICK
        # Unconditional, so a bot that never rotates behaves exactly as before.
        if self._nick.lower() != config.IRC_NICK.lower():
            self._reclaim_nick()

    def _next_rotation_name(self, with_number: bool = False, force_base: str = "") -> str:
        """The next name to ask for.

        The owner's design: "i want it to be done by the bot itself that it can
        change to a different id can add a number on back of it to avoid any
        conflicts." The number is what makes it work with no setup — Luna47 is
        almost certainly not registered to anyone, so NickServ has no reason to
        force a rename, and it still reads as obviously her.

        On a retry the base is not re-chosen: the name that came back taken is
        the one to number.
        """
        now = self._nick.lower()
        base = force_base
        if not base:
            # A genuinely different name — not the one she wears, and not the
            # bare stem of it either, since Selene -> Selene12 is precisely the
            # "just changing numbers" that was rejected.
            stem = now.rstrip("0123456789")
            bank = config.IRC_NICK_POOL or config.IRC_DEFAULT_NAMES
            options = [n for n in bank
                       if n.lower() not in (now, stem)
                       and n.lower() not in self._unusable_names]
            if not options:
                return ""
            base = random.choice(options)
        limit = self.nick_limit()
        # Plain name first: the number is for CONFLICTS, not for naming.
        if not with_number and len(base) <= limit and base.lower() != now:
            return base
        trunk = base[:max(3, limit - 3)]
        for _ in range(25):
            candidate = f"{trunk}{random.randint(2, 99)}"
            if candidate.lower() != now:
                return candidate
        return ""

    def _rotate_nick(self) -> bool:
        if not config.IRC_NICK_ROTATE:
            return False
        if not self._connected or self._pending_rotation:
            return False
        if not self._rotation_allowed():
            return False
        nxt = self._next_rotation_name()
        if not nxt:
            return False
        # Still subject to the flood ceiling below: two limits that can only
        # ever make each other stricter is the right shape for something that
        # gets you killed for being wrong.
        if not self._nick_allowed():
            return False
        self._pending_rotation = nxt
        self._rotation_numbered = False
        self._rotations_at.append(time.time())
        print(f"[irc_bridge] Rotating {self._nick} -> {nxt}")
        self._raw(f"NICK {nxt}")
        # Owner-only visibility: private NOTICE to each configured owner nick
        # so Vikram can see rotations happening without spamming the room (and
        # without strangers correlating base-nick -> rotation-nick, which is
        # the whole point of the rotation).
        try:
            from shared_cmds import OWNERS_IRC
            for owner_nick in OWNERS_IRC:
                self._raw(f"NOTICE {owner_nick} :\x02[rotation]\x02 {self._nick} -> {nxt}")
        except Exception:
            pass            # never block the rotation
        threading.Timer(30.0, self._clear_pending_rotation, args=(nxt,)).start()
        return True

    def _rotation_tick(self, now: float = None) -> None:
        """Decide, once per sweep, whether it is time to rotate — and advance the
        clock only when a rotation actually FIRES.

        The clock used to advance unconditionally, so a first attempt that failed
        (the flood budget briefly spent by the connect/reclaim NICKs) still burned
        the whole interval and left the bot on its base nick — Luna1 — for up to
        90 minutes after every connect. Every reconnect starts as Luna1 by design;
        this is what decides how long it stays that way, and the answer is "as
        short as the flood limit allows".
        """
        if not (self._connected and config.IRC_NICK_ROTATE):
            return
        now = time.time() if now is None else now
        if now - self._last_rotate <= config.IRC_NICK_ROTATE_MIN * 60:
            return
        if self._rotate_nick():
            self._last_rotate = now
        else:
            # Retry on the next sweep, not a full interval later — but not this
            # same line either, so a spent budget is not hammered.
            self._last_rotate = now - config.IRC_NICK_ROTATE_MIN * 60 + 20

    def _clear_pending_rotation(self, asked_for: str) -> None:
        """A request the server never answered must not block every later one."""
        if self._pending_rotation == asked_for:
            self._pending_rotation = ""

    _NICK_MAX = 6
    _NICK_WINDOW = 300

    def _nick_allowed(self) -> bool:
        now = time.time()
        self._nick_times = [t for t in getattr(self, "_nick_times", []) if now - t < self._NICK_WINDOW]
        if len(self._nick_times) >= self._NICK_MAX:
            if now - getattr(self, "_nick_gripe", 0) > 300:
                self._nick_gripe = now
                print(f"[irc_bridge] Refusing to change nick again — {len(self._nick_times)} "
                      f"in {self._NICK_WINDOW}s. This network kills for nick flooding, and "
                      "whatever is asking needs fixing, not retrying.")
            return False
        self._nick_times.append(now)
        return True

    # Protective shield: hard cap on outbound rate so a loop that calls _raw
    # tightly can never flood-kill the bot. Dracula has a 2 lines/sec pacer;
    # Luna had NOTHING between code and server. HybridIRC's RecvQ limit is
    # roughly 10 lines/sec sustained — the 2026-10-06 Carfax drop proved it.
    # Everyday traffic is well under the cap; a dropped non-protocol line is
    # survivable, a server flood-kill is not.
    _FLOOD_LIMIT = 10
    _FLOOD_WINDOW_SEC = 1.0
    _FLOOD_EXEMPT = ("PING", "PONG", "QUIT")

    def _raw(self, msg: str):
        if not self._sock:
            return
        try:
            # PING/PONG/QUIT bypass the cap: the protocol requires them to go
            # through promptly, and they are small, bounded and self-driven
            # so they can never themselves be the source of a flood.
            up = (msg or "").lstrip().upper()
            if not up.startswith(self._FLOOD_EXEMPT):
                now = time.time()
                recent = getattr(self, "_recent_sends", None)
                if recent is None:
                    recent = []
                    self._recent_sends = recent
                # In-place prune of entries past the window.
                cutoff = now - self._FLOOD_WINDOW_SEC
                while recent and recent[0] < cutoff:
                    recent.pop(0)
                if len(recent) >= self._FLOOD_LIMIT:
                    # One warning per shed, so a surge is visible in logs but
                    # does not become its own flood of warnings.
                    print(f"[irc_bridge] ★ FLOOD SHIELD dropped: {msg[:60]}... "
                          f"(cap {self._FLOOD_LIMIT}/{self._FLOOD_WINDOW_SEC}s)")
                    return
                recent.append(now)
            self._sock.sendall(f"{msg}\r\n".encode("utf-8"))
        except Exception as e:  # noqa: BLE001 — never let an outbound error kill the bridge
            print(f"[irc_bridge] _raw send error: {e}")

"""
BatCave Brawl — Game Controller
Wires IRC connections + game engine together.
Handles all !! commands from #batcave channel.
"""

import logging
import os
import sys
import threading
import time

import random

from .engine    import BrawlEngine, BrawlFight, Combatant
from .fighters  import FIGHTERS, FIGHTER_NAMES, get_rank, LUNA_COMMENTARY
from .irc_client import IRCClient

# config is a top-level module (not inside brawl package)
try:
    import config as config
except ImportError:
    config = None  # type: ignore

log = logging.getLogger("brawl.game")

_ch = os.getenv("BRAWL_CHANNEL", os.getenv("BRIDGE_CHANNEL", "#batcave"))
CHANNEL = _ch if _ch.startswith("#") else f"#{_ch}"
IRC_SERVER = os.getenv("IRC_SERVER",    "irc.hybridirc.com")
IRC_PORT   = int(os.getenv("IRC_PORT",  "6667"))
NS_PASS    = os.getenv("IRC_NICKSERV_PASS", "")
NS_ACCOUNT = os.getenv("IRC_NICKSERV_ACCOUNT", "")

# Fighter bots always connect on plain-TCP port (6667 confirmed working on HybridIRC).
# Luna1 may use SSL/6697, but fighter bots are short-lived, unregistered nicks.
FIGHTER_IRC_PORT = int(os.getenv("FIGHTER_IRC_PORT", "6667"))
FIGHTER_IRC_SSL  = FIGHTER_IRC_PORT >= 6697

SEP = "─" * 46

# Street attack constants
STREET_HIT_LIMIT   = 3    # hits before victim is kicked
ATTACK_COOLDOWN    = 15   # seconds between attacks by same player


class BrawlGame:
    """Main game controller — one instance for the whole server.

    Two modes:
      • Bridged mode  — pass say_fn from luna.py; uses Luna's IRC connection.
        No extra IRC client is created. Zero flood risk.
      • Standalone mode — no say_fn; creates a BrawlBot IRC client directly.
        Kept for local dev / testing without Luna.
    """

    def __init__(self, say_fn=None, kick_fn=None):
        self.engine  = BrawlEngine()
        self.channel = CHANNEL

        # ── Bridged mode (production) ──────────────────────────────────────
        self._ext_say  = say_fn   # callable(text) → None, or None
        self._kick_fn  = kick_fn  # callable(nick, reason) → None, or None

        # ── Standalone mode (local dev / no bridge) ────────────────────────
        self.brawl_bot: IRCClient | None = None
        if self._ext_say is None:
            self.brawl_bot = IRCClient(
                nick          = "BrawlBot",
                server        = IRC_SERVER,
                port          = IRC_PORT,
                channel       = self.channel,
                realname      = "BatCave Brawl Bot",
                nickserv_pass = NS_PASS,
                ns_account    = NS_ACCOUNT,
            )
            self.brawl_bot.on_message = self.handle_irc_message
            self.brawl_bot.on_connect = self._on_brawl_connect

        # ── Active fighter IRC bots ────────────────────────────────────────
        # fighter_name.lower() → IRCClient  (real IRC connection per picked fighter)
        self._fighter_bots: dict[str, IRCClient] = {}
        # player_nick.lower() → fighter_name  (which fighter each player has active)
        self._player_fighter: dict[str, str] = {}

        # Player → chosen fighter (before a fight starts)
        self._chosen: dict[str, str] = {}   # nick.lower() → fighter_name

        # Street attack tracking (outside structured fights)
        self._street_hits: dict[str, dict] = {}  # victim.lower() → {hits, last_attacker, fighter}
        self._attack_cd: dict[str, float]   = {}  # attacker.lower() → last attack timestamp

        # Rumble lobby: nick.lower() → (nick, fighter_name)
        self._rumble_lobby: dict[str, tuple] = {}
        self._rumble_timer: threading.Timer | None = None
        self._rumble_host: str | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self):
        if self.brawl_bot:
            log.info("Starting BrawlGame (standalone) on %s %s", IRC_SERVER, self.channel)
            self.brawl_bot.start()
        else:
            log.info("BrawlGame running in bridged mode (Luna1's IRC connection).")
            def _announce():
                time.sleep(5)
                self.say(f"\x02⚔️  BatCave Brawl is LIVE!\x02  :batman: Type \x02!!brawl\x02 to learn how to play! :parrot:")
            threading.Thread(target=_announce, daemon=True).start()

    def stop(self):
        if self.brawl_bot:
            self.brawl_bot.stop()
        for bot in list(self._fighter_bots.values()):
            try:
                bot.stop("BatCave Brawl shutting down.")
            except Exception:
                pass

    def _on_brawl_connect(self):
        time.sleep(2)
        self.say(f"\x02⚔️  BatCave Brawl is LIVE!\x02  :batman: Type \x02!!brawl\x02 to learn how to play! :parrot:")

    # ── Fighter IRC connection management ──────────────────────────────────

    def _connect_fighter(self, fighter_name: str, player_nick: str):
        """Open a real IRC connection for this fighter. Called when player picks."""
        f = FIGHTERS.get(fighter_name)
        if not f:
            return

        # Disconnect any previous fighter this player had
        old_fighter = self._player_fighter.get(player_nick.lower())
        if old_fighter and old_fighter.lower() in self._fighter_bots:
            self._disconnect_fighter(old_fighter, announce=False)

        # Fighter bots use plain TCP on FIGHTER_IRC_PORT (default 6667).
        # This is separate from Luna's port which may be SSL/6697.
        print(f"[brawl] Connecting fighter {fighter_name} ({f['nick']}) → "
              f"{IRC_SERVER}:{FIGHTER_IRC_PORT} ssl={FIGHTER_IRC_SSL}")
        log.info("[brawl] Connecting %s to %s:%s ssl=%s",
                 fighter_name, IRC_SERVER, FIGHTER_IRC_PORT, FIGHTER_IRC_SSL)

        bot = IRCClient(
            nick      = f["nick"],
            server    = IRC_SERVER,
            port      = FIGHTER_IRC_PORT,
            channel   = self.channel,
            realname  = f"{f['style']} — BatCave Brawl",
            nickserv_pass = "",
            ns_account    = "",
            use_ssl   = FIGHTER_IRC_SSL,
        )

        connect_msg = f.get("connect_msg", f"{f['emoji']} *{fighter_name} enters the arena!*")

        def _on_connect(bot=bot, msg=connect_msg):
            time.sleep(1.5)
            print(f"[brawl] {fighter_name} joined IRC — saying connect msg")
            bot.say(self.channel, msg)

        bot.on_connect = _on_connect
        self._fighter_bots[fighter_name.lower()] = bot
        self._player_fighter[player_nick.lower()] = fighter_name
        bot.start()
        print(f"[brawl] {fighter_name} IRC client started (player={player_nick})")

    def _disconnect_fighter(self, fighter_name: str, announce: bool = True):
        """Disconnect a fighter's IRC bot."""
        bot = self._fighter_bots.pop(fighter_name.lower(), None)
        if not bot:
            return
        f = FIGHTERS.get(fighter_name, {})
        quit_msg = f.get("quit_msg", f"{fighter_name} has left the arena.")
        if announce:
            try:
                bot.say(self.channel, f.get("ko_msg", f"*{fighter_name} falls...*"))
            except Exception:
                pass
            time.sleep(1.5)
        bot.stop(quit_msg)

    # ── IRC helpers ────────────────────────────────────────────────────────

    def say(self, msg: str):
        """Send a message to the brawl channel via Luna1's connection."""
        if self._ext_say:
            self._ext_say(msg)
        elif self.brawl_bot:
            self.brawl_bot.say(self.channel, msg)

    def fighter_say(self, fighter_name: str, msg: str):
        """Make a fighter speak — via their own IRC connection if active, else via Luna1."""
        bot = self._fighter_bots.get(fighter_name.lower())
        if bot and bot._connected:
            bot.say(self.channel, msg)
        else:
            # Fallback: speak through Luna1 with fighter prefix
            f = FIGHTERS.get(fighter_name, {})
            emoji = f.get("emoji", "⚔️")
            self.say(f"{emoji} \x02[{fighter_name}]\x02 {msg}")

    def _luna_comment(self, event: str):
        """Luna1 reacts to fight events with AI commentary."""
        lines = LUNA_COMMENTARY.get(event, [])
        if lines:
            line = random.choice(lines)
            threading.Timer(0.5, self.say, args=[f"🤖 \x02[Luna1]\x02 {line}"]).start()

    def kick_player(self, nick: str, reason: str):
        """Kick a player from IRC using Luna1's ops (special attack effect)."""
        if self._kick_fn:
            self._kick_fn(nick, reason)

    def _debug_connections(self, nick: str):
        """Report fighter IRC connection state into the channel (for debugging)."""
        mode = "bridged" if self._ext_say else "standalone"
        self.say(f"  🔧 \x02[debug]\x02 mode={mode}  fighter_port={FIGHTER_IRC_PORT}  ssl={FIGHTER_IRC_SSL}")
        if not self._fighter_bots:
            self.say(f"  🔧 \x02[debug]\x02 No active fighter IRC bots.")
        for name, bot in self._fighter_bots.items():
            sock_ok = bot._sock is not None
            self.say(
                f"  🔧 \x02[debug]\x02 {name}: nick={bot.nick}  connected={bot._connected}"
                f"  running={bot._running}  sock={sock_ok}"
            )
        self.say(f"  🔧 \x02[debug]\x02 chosen={dict(self._chosen)}  player_fighter={dict(self._player_fighter)}")

    # ── Street Attack (out-of-fight humiliation + kick) ───────────────────────

    def _street_attack(self, nick: str, target: str):
        """!!attack <nick> — humiliate a channel user. 3 hits = Luna1 kick."""
        if not target:
            self.say(f"  ❓ {nick}: \x02!!attack <nick>\x02 — pick a target!")
            return

        # Must have picked a fighter
        fighter_name = self._chosen.get(nick.lower())
        if not fighter_name:
            self.say(f"  ❓ {nick}: pick your fighter first!  \x02!!pick <name>\x02")
            return

        # Block self-attacks and bot attacks
        protected = {"luna1", "luna", "brawlbot", "chanbot"}
        if target.lower() == nick.lower():
            self.say(f"  ⚠️ {nick}: can't attack yourself!")
            return
        if target.lower() in protected:
            self.say(f"  🛡️ {nick}: {target} is protected! Pick on someone your own size.")
            return

        # Cooldown check
        now = time.time()
        last = self._attack_cd.get(nick.lower(), 0.0)
        remaining = int(ATTACK_COOLDOWN - (now - last))
        if remaining > 0:
            self.say(f"  ⏳ {nick}: cooldown! Attack again in \x02{remaining}s\x02.")
            return

        self._attack_cd[nick.lower()] = now

        # Track hits on victim
        vkey = target.lower()
        if vkey not in self._street_hits:
            self._street_hits[vkey] = {"hits": 0, "last_attacker": nick, "fighter": fighter_name}

        self._street_hits[vkey]["hits"] += 1
        self._street_hits[vkey]["last_attacker"] = nick
        hits = self._street_hits[vkey]["hits"]

        # Deliver humiliation message from the fighter
        f = FIGHTERS[fighter_name]
        humiliations = f.get("humiliations", [
            f"{f['emoji']} *{fighter_name} attacks {target}!* Sent by {nick}."
        ])
        msg = random.choice(humiliations).replace("{victim}", target).replace("{attacker}", nick)
        self.fighter_say(fighter_name, msg)

        if hits >= STREET_HIT_LIMIT:
            # Final blow — Luna1 delivers the kick
            del self._street_hits[vkey]
            time.sleep(0.6)
            self.say(
                f"  💀 \x02{target}\x02 has been OBLITERATED by \x02{nick}\x02's \x02{fighter_name}\x02! "
                f":batman: Luna1 steps in..."
            )
            time.sleep(0.7)
            kick_reason = (
                f"💥 {fighter_name} sent by {nick} just ended you! "
                f"Respawn & seek revenge~ :batman:"
            )
            threading.Timer(0.3, self.kick_player, args=[target, kick_reason]).start()
            # Luna commentary on the kill
            self._luna_comment("ko")
        else:
            left = STREET_HIT_LIMIT - hits
            bars = "💢" * hits + "🖤" * left
            self.say(
                f"  {bars}  \x02{target}\x02 takes hit \x02{hits}/{STREET_HIT_LIMIT}\x02! "
                f"— \x02{left}\x02 more blow(s) until they're KICKED! "
                f"({ATTACK_COOLDOWN}s cooldown)"
            )
            # Close-call commentary on final warning
            if hits == STREET_HIT_LIMIT - 1:
                self._luna_comment("close_call")

    # ── Message handler (public — called by bridge OR standalone on_message) ─

    def handle_irc_message(self, nick: str, target: str, text: str):
        """Entry point for IRC messages. Called by IRCBridge hook or BrawlBot."""
        # Accept both (nick, target, text) and (nick, text) signatures
        # When called from standalone brawl_bot.on_message: (nick, target, text)
        # When called from irc_bridge hook: (nick, target, text)
        if not isinstance(target, str) or not target.startswith("#"):
            # Called as (nick, text) — no target arg — shouldn't happen but guard
            return

        if target.lower() != self.channel.lower():
            return
        if not text.startswith("!!"):
            return

        parts = text[2:].strip().split(None, 2)
        if not parts:
            return
        cmd  = parts[0].lower()
        args = parts[1:]

        # Ignore Luna itself and any legacy BrawlBot
        _luna_nick = getattr(config, "IRC_NICK", "Luna1").lower() if config else "luna1"
        ignore = {"luna", "luna1", "brawlbot", _luna_nick}
        if nick.lower() in ignore:
            return

        try:
            self._dispatch(nick, cmd, args)
        except Exception as e:
            log.error("dispatch error [%s] %s: %s", nick, cmd, e)

    def _dispatch(self, nick: str, cmd: str, args: list[str]):

        # ── INFO commands ──
        if cmd == "brawl" or cmd == "help":
            self._show_help(nick)

        elif cmd == "fighters":
            self._show_fighters()

        elif cmd == "debugconn" or cmd == "dbg":
            self._debug_connections(nick)

        elif cmd == "stats":
            self._show_stats(nick)

        elif cmd == "leaderboard" or cmd == "lb":
            self._show_leaderboard()

        # ── SETUP commands ──
        elif cmd == "attack" or cmd == "strike" or cmd == "mug":
            target = args[0] if args else ""
            self._street_attack(nick, target)

        elif cmd == "pick":
            fighter = args[0] if args else ""
            self._pick(nick, fighter)

        elif cmd == "fight":
            target = args[0] if args else ""
            self._fight(nick, target)

        elif cmd == "challenge":
            target = args[0] if args else ""
            self._challenge(nick, target, args[1] if len(args) > 1 else "")

        elif cmd == "accept":
            self._accept(nick)

        elif cmd == "dodge" or cmd == "decline":
            self._decline(nick)

        elif cmd == "rumble":
            sub = args[0].lower() if args else "join"
            if sub == "start":
                self._rumble_start(nick)
            elif sub == "join":
                fighter = args[1] if len(args) > 1 else ""
                self._rumble_join(nick, fighter)
            elif sub == "go":
                self._rumble_force_start(nick)
            elif sub == "cancel":
                self._rumble_cancel(nick)

        elif cmd == "yield":
            self._yield_fight(nick)

        # ── COMBAT commands (attack moves) ──
        else:
            self._submit_attack(nick, cmd)

    # ── Pick fighter ───────────────────────────────────────────────────────

    def _pick(self, nick: str, fighter: str):
        match = next((f for f in FIGHTER_NAMES if f.lower() == fighter.lower()), None)
        if not match:
            self.say(f"  ❓ {nick}: unknown fighter '{fighter}'. Type \x02!!fighters\x02 to see the roster.")
            return
        self._chosen[nick.lower()] = match
        f = FIGHTERS[match]
        self.say(f"  ✅ \x02{nick}\x02 picks \x02{match}\x02 ({f['style']}) {f['emoji']}")
        # Connect fighter to IRC — they'll announce themselves when joined
        threading.Thread(
            target=self._connect_fighter,
            args=(match, nick),
            daemon=True,
        ).start()

    def _get_or_pick(self, nick: str, fighter_arg: str) -> str | None:
        """Resolve fighter for nick: from arg, from pre-pick, or error."""
        if fighter_arg:
            match = next((f for f in FIGHTER_NAMES if f.lower() == fighter_arg.lower()), None)
            if match:
                self._chosen[nick.lower()] = match
                return match
            self.say(f"  ❓ Unknown fighter '{fighter_arg}'. !!fighters for roster.")
            return None
        chosen = self._chosen.get(nick.lower())
        if not chosen:
            self.say(f"  ❓ {nick}: pick a fighter first!  \x02!!pick <name>\x02  or  \x02!!fighters\x02")
            return None
        return chosen

    # ── 1v1 vs bot ─────────────────────────────────────────────────────────

    def _fight(self, nick: str, target: str):
        if self.engine.in_fight(nick):
            self.say(f"  ⚠️ {nick}: you're already in a fight! !!yield to surrender.")
            return

        # target must be a fighter name
        fighter_target = next((f for f in FIGHTER_NAMES if f.lower() == target.lower()), None)
        if not fighter_target:
            self.say(f"  ❓ '{target}' is not a fighter name. Use \x02!!fight <FighterName>\x02 "
                     f"or \x02!!challenge <player>\x02 for PvP.")
            return

        # Pick fighter for player
        c_fighter = self._chosen.get(nick.lower())
        if not c_fighter:
            self.say(f"  ❓ Pick your fighter first: \x02!!pick <name>\x02  then  !!fight {target}")
            return

        if c_fighter == fighter_target:
            self.say(f"  ⚠️ {nick}: you can't fight yourself! Choose a different opponent.")
            return

        # Build fight
        player_c = Combatant(nick,        c_fighter,     is_ai=False)
        bot_c    = Combatant(None,         fighter_target, is_ai=True)

        fight = BrawlFight(
            fight_id  = self.engine.new_fight_id(),
            mode      = "1v1",
            channel   = self.channel,
            say_fn    = self.say,
            bot_say_fn= self.fighter_say,
            on_end    = self._on_fight_end,
            kick_fn   = self.kick_player,
        )
        fight.add(player_c)
        fight.add(bot_c)
        self.engine.register_fight(fight)

        self.say(f"  ⚔️  {nick} (\x02{c_fighter}\x02) vs AI (\x02{fighter_target}\x02) — FIGHT STARTING!")
        self._luna_comment("fight_start")
        threading.Thread(target=fight.start, daemon=True).start()

    # ── PvP challenge ──────────────────────────────────────────────────────

    def _challenge(self, nick: str, target: str, fighter_arg: str):
        if not target:
            self.say(f"  Usage: \x02!!challenge <player> [FighterName]\x02")
            return
        if target.lower() == nick.lower():
            self.say(f"  ⚠️ {nick}: can't challenge yourself.")
            return
        if self.engine.in_fight(nick):
            self.say(f"  ⚠️ {nick}: finish your current fight first.")
            return

        c_fighter = self._get_or_pick(nick, fighter_arg)
        if not c_fighter:
            return

        t_fighter = self._chosen.get(target.lower())  # target may have pre-picked

        self.engine.set_challenge(nick, target, c_fighter, t_fighter)
        tf_note = f" as \x02{t_fighter}\x02" if t_fighter else ""
        self.say(
            f"  ⚔️  \x02{nick}\x02 (\x02{c_fighter}\x02) challenges \x02{target}\x02{tf_note}!"
            f"  — {target}: type \x02!!accept [FighterName]\x02 or \x02!!dodge\x02"
        )

    def _accept(self, nick: str):
        challenger, ch = self.engine.find_challenge_for(nick)
        if not challenger:
            self.say(f"  ❓ {nick}: no pending challenge for you.")
            return
        if self.engine.in_fight(nick) or self.engine.in_fight(challenger):
            self.say(f"  ⚠️ One of you is already in a fight!")
            self.engine.clear_challenge(challenger)
            return

        t_fighter = ch.get("t_fighter") or self._chosen.get(nick.lower())
        if not t_fighter:
            self.say(f"  ❓ {nick}: pick your fighter first!  \x02!!pick <name>\x02  then !!accept")
            return

        c_fighter = ch["c_fighter"]
        self.engine.clear_challenge(challenger)

        p1 = Combatant(challenger, c_fighter, is_ai=False)
        p2 = Combatant(nick,       t_fighter, is_ai=False)

        fight = BrawlFight(
            fight_id  = self.engine.new_fight_id(),
            mode      = "1v1",
            channel   = self.channel,
            say_fn    = self.say,
            bot_say_fn= self.fighter_say,
            on_end    = self._on_fight_end,
            kick_fn   = self.kick_player,
        )
        fight.add(p1)
        fight.add(p2)
        self.engine.register_fight(fight)

        self.say(
            f"  ✅ \x02{nick}\x02 accepts! "
            f"\x02{challenger}\x02(\x02{c_fighter}\x02)  vs  \x02{nick}\x02(\x02{t_fighter}\x02)"
            f" — FIGHT STARTING! :batman:"
        )
        self._luna_comment("fight_start")
        threading.Thread(target=fight.start, daemon=True).start()

    def _decline(self, nick: str):
        challenger, ch = self.engine.find_challenge_for(nick)
        if challenger:
            self.engine.clear_challenge(challenger)
            self.say(f"  😤 \x02{nick}\x02 dodges the challenge from {challenger}. Coward? Or smart?")
        else:
            self.say(f"  ❓ {nick}: no challenge to dodge.")

    # ── Royal Rumble ───────────────────────────────────────────────────────

    def _rumble_start(self, nick: str):
        if self._rumble_host:
            self.say(f"  ⚠️ A rumble lobby is already open! Type \x02!!rumble join [Fighter]\x02")
            return

        self._rumble_host   = nick
        self._rumble_lobby  = {}
        self.say(SEP)
        self.say(f"  👑 \x02{nick}\x02 opens a ROYAL RUMBLE lobby!")
        self.say(f"  Type \x02!!rumble join [FighterName]\x02 to enter  (auto-starts in 90s or !!rumble go)")
        self.say(f"  Min 3 fighters needed. AI bots fill empty slots up to 6 fighters.")
        self.say(SEP)

        self._rumble_timer = threading.Timer(90, self._rumble_auto_start)
        self._rumble_timer.start()

    def _rumble_join(self, nick: str, fighter_arg: str):
        if not self._rumble_host:
            self.say(f"  ❓ No rumble lobby open. \x02!!rumble start\x02 to open one.")
            return
        if self.engine.in_fight(nick):
            self.say(f"  ⚠️ {nick}: finish your current fight first.")
            return

        fighter = self._get_or_pick(nick, fighter_arg)
        if not fighter:
            return

        self._rumble_lobby[nick.lower()] = (nick, fighter)
        f = FIGHTERS[fighter]
        self.say(f"  ✅ \x02{nick}\x02 enters as \x02{fighter}\x02 {f['emoji']}  "
                 f"[{len(self._rumble_lobby)}/6 fighters]")
        self.fighter_say(fighter, f.get("intro", f"*{fighter} enters the arena*"))

    def _rumble_force_start(self, nick: str):
        if nick.lower() != (self._rumble_host or "").lower():
            self.say(f"  ⚠️ Only the host ({self._rumble_host}) can force-start.")
            return
        if self._rumble_timer:
            self._rumble_timer.cancel()
        self._rumble_auto_start()

    def _rumble_cancel(self, nick: str):
        if nick.lower() != (self._rumble_host or "").lower():
            self.say(f"  ⚠️ Only the host can cancel.")
            return
        if self._rumble_timer:
            self._rumble_timer.cancel()
        self._rumble_lobby  = {}
        self._rumble_host   = None
        self._rumble_timer  = None
        self.say(f"  ❌ Royal Rumble lobby cancelled.")

    def _rumble_auto_start(self):
        players = list(self._rumble_lobby.values())  # [(nick, fighter), ...]
        lobby   = self._rumble_lobby
        host    = self._rumble_host
        self._rumble_lobby  = {}
        self._rumble_host   = None
        self._rumble_timer  = None

        if len(players) < 2:
            self.say(f"  ❌ Not enough players for Royal Rumble (need at least 2). Lobby closed.")
            return

        # Fill up to 6 with AI fighters not already taken
        taken = {p[1] for p in players}
        ai_pool = [f for f in FIGHTER_NAMES if f not in taken]
        import random; random.shuffle(ai_pool)
        while len(players) < 6 and ai_pool:
            ai_f = ai_pool.pop(0)
            players.append((None, ai_f))

        fight = BrawlFight(
            fight_id  = self.engine.new_fight_id(),
            mode      = "rumble",
            channel   = self.channel,
            say_fn    = self.say,
            bot_say_fn= self.fighter_say,
            on_end    = self._on_fight_end,
        )

        for (player_nick, fighter_name) in players:
            is_ai = player_nick is None
            c = Combatant(player_nick, fighter_name, is_ai=is_ai)
            fight.add(c)
            if player_nick:
                self.engine._player_fight[player_nick.lower()] = fight.fight_id

        self.engine._fights[fight.fight_id] = fight

        self.say(f"  👑 ROYAL RUMBLE STARTING with {len(players)} fighters!")
        threading.Thread(target=fight.start, daemon=True).start()

    # ── Combat submission ──────────────────────────────────────────────────

    def _submit_attack(self, nick: str, move: str):
        fight = self.engine.get_fight(nick)
        if not fight:
            return   # silently ignore — player not in a fight

        result = fight.submit_move(nick, move)
        if result == "already_submitted":
            self.say(f"  ⚠️ {nick}: already submitted this round.")
        elif result == "special_used":
            self.say(f"  ⚠️ {nick}: special already used this fight!")
        elif result == "invalid_move":
            f = self.engine.get_fight(nick)
            if f:
                c = next((x for x in f.combatants if x.player_nick and x.player_nick.lower() == nick.lower()), None)
                if c:
                    valid = list(c.fighter["attacks"].keys())
                    self.say(f"  ❓ {nick}: invalid move. Your moves: {', '.join(valid)}")

    def _yield_fight(self, nick: str):
        fight = self.engine.get_fight(nick)
        if not fight:
            self.say(f"  ❓ {nick}: you're not in a fight.")
            return
        c = next((x for x in fight.combatants if x.player_nick and x.player_nick.lower() == nick.lower()), None)
        if c:
            c.hp = 0
            self.say(f"  🏳️ \x02{nick}\x02 yields! ({c.fighter_name} falls!)")
            fight._end()

    # ── Fight end callback ─────────────────────────────────────────────────

    def _on_fight_end(self, fight: BrawlFight):
        alive = [c for c in fight.combatants if c.alive]
        dead  = [c for c in fight.combatants if not c.alive]
        self.engine.finish_fight(fight)

        # Luna1 win commentary
        self._luna_comment("win")

        # Disconnect KO'd fighters from IRC (they QUIT dramatically)
        for c in dead:
            if c.player_nick:
                fighter_key = self._player_fighter.get(c.player_nick.lower(), "")
                if fighter_key:
                    threading.Thread(
                        target=self._disconnect_fighter,
                        args=(fighter_key, True),
                        daemon=True,
                    ).start()
                    self._player_fighter.pop(c.player_nick.lower(), None)

        # Announce XP gains
        time.sleep(1.5)
        for c in fight.combatants:
            if not c.is_ai and c.player_nick:
                stats = self.engine.get_stats(c.player_nick)
                xp_gain = 50 if c.alive else 10
                self.say(
                    f"  {'🏆' if c.alive else '💔'} {c.player_nick} "
                    f"+{xp_gain}XP  |  Total: {stats['xp']}XP  |  Rank: {stats['rank']}"
                )

    # ── Info commands ──────────────────────────────────────────────────────

    def _show_help(self, nick: str):
        self.say(SEP)
        self.say(f"\x02🥊 BATCAVE BRAWL — Commands\x02")
        self.say(f"  \x02!!fighters\x02              — show all playable fighters")
        self.say(f"  \x02!!pick <Fighter>\x02         — choose your fighter")
        self.say(f"  \x02!!fight <Fighter>\x02        — 1v1 vs AI-controlled fighter")
        self.say(f"  \x02!!challenge <player>\x02     — PvP: challenge another user")
        self.say(f"  \x02!!accept [Fighter]\x02       — accept a PvP challenge")
        self.say(f"  \x02!!dodge\x02                  — decline a challenge")
        self.say(f"  \x02!!rumble start\x02           — open Royal Rumble lobby")
        self.say(f"  \x02!!rumble join [Fighter]\x02  — join the rumble")
        self.say(f"  \x02!!rumble go\x02              — host force-starts the rumble")
        self.say(f"  \x02!!yield\x02                  — surrender current fight")
        self.say(f"  \x02!!stats\x02  \x02!!leaderboard\x02   — your record / top players")
        self.say(f"  \x02!!<attack>\x02               — submit your move when in a fight")
        self.say(f"  Attack types: POWER beats QUICK  |  QUICK beats TRICK  |  TRICK beats POWER")
        self.say(SEP)
        self.say(f"\x02🗡️  STREET ATTACKS\x02  (no fight needed — just pick a fighter!)")
        self.say(f"  \x02!!attack <nick>\x02          — humiliate someone in chat")
        self.say(f"  3 hits on same target = Luna1 \x02KICKS\x02 them! ({ATTACK_COOLDOWN}s cooldown per hit)")
        self.say(SEP)

    def _show_fighters(self):
        self.say(SEP)
        self.say(f"\x02🥊 BATCAVE BRAWL — Fighter Roster\x02")
        self.say(f"  {'Name':<14} {'Style':<12} {'HP':>4}  Attacks")
        self.say(f"  {'─'*13} {'─'*11} {'──':>4}  {'─'*20}")
        for name, f in FIGHTERS.items():
            attacks = ", ".join(f["attacks"].keys())
            self.say(f"  {f['emoji']} {name:<12} {f['style']:<12} {f['hp']:>3}HP  {attacks}")
        self.say(f"  Use \x02!!pick <name>\x02 to choose your fighter")
        self.say(SEP)

    def _show_stats(self, nick: str):
        s = self.engine.get_stats(nick)
        self.say(
            f"  📊 {nick}:  {s['xp']} XP  |  {s['wins']}W / {s['losses']}L  |  {s['rank']}"
        )

    def _show_leaderboard(self):
        lb = self.engine.leaderboard(8)
        if not lb:
            self.say(f"  No fights recorded yet. Start one: \x02!!fight BladeX\x02")
            return
        self.say(f"  \x02🏆 BatCave Brawl Leaderboard:\x02")
        for i, (nick, xp, rank) in enumerate(lb, 1):
            self.say(f"  {i}. {nick:<16} {xp:>5} XP  {rank}")

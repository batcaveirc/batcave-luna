"""
BatCave Brawl — Game Engine
Handles all fight logic: HP, damage, type advantages, turn resolution,
Royal Rumble, and the state machine for each active fight.
"""

import random
import threading
import time
import logging
from typing import Callable

from .fighters import FIGHTERS, TYPE_BEATS, get_rank

log = logging.getLogger("brawl.engine")

TURN_TIMEOUT   = 8     # seconds players have to submit a move
HP_FULL        = "❤️"
HP_EMPTY       = "💔"
SEPARATOR      = "─" * 46
THICK_SEP      = "═" * 46


def hp_bar(current: int, max_hp: int) -> str:
    current = max(0, current)
    filled  = min(current, max_hp)
    empty   = max(0, max_hp - filled)
    # cap display at 14 hearts for readability
    scale = max_hp / 10
    disp_filled = round(filled / scale)
    disp_empty  = round(empty  / scale)
    return HP_FULL * disp_filled + HP_EMPTY * disp_empty


def speed_label(elapsed: float) -> str:
    if elapsed < 1.0:  return "⚡\x02CRITICAL!\x02 (×1.5)"
    if elapsed < 2.5:  return "💨 \x02FAST!\x02 (×1.2)"
    if elapsed < 5.0:  return "✅ Normal (×1.0)"
    if elapsed < 7.0:  return "🐢 Slow… (×0.7)"
    return "❌ TIMED OUT (×0)"


def speed_mult(elapsed: float) -> float:
    if elapsed < 1.0:  return 1.5
    if elapsed < 2.5:  return 1.2
    if elapsed < 5.0:  return 1.0
    if elapsed < 7.0:  return 0.7
    return 0.0


def type_advantage(atk_type: str, def_type: str) -> tuple[float, str]:
    """Returns (damage_multiplier, label_string)."""
    if atk_type == def_type:
        return 1.0, ""
    if TYPE_BEATS.get(atk_type) == def_type:
        return 1.35, " \x0303[TYPE ADVANTAGE +35%]\x03"
    return 0.75, " \x0304[type disadvantage -25%]\x03"


class Combatant:
    def __init__(self, player_nick: str | None, fighter_name: str, is_ai: bool = False):
        self.player_nick  = player_nick    # human IRC nick or None
        self.fighter_name = fighter_name
        self.fighter      = FIGHTERS[fighter_name]
        self.max_hp       = self.fighter["hp"]
        self.hp           = self.max_hp
        self.is_ai        = is_ai
        # Status effects
        self.poison       = 0    # damage per round
        self.armor        = 0    # absorbs incoming damage once
        self.evading      = False
        # Per-round state
        self.move         = None   # attack key submitted this round
        self.move_time    = None   # time.time() when submitted
        self.special_used = False

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def label(self) -> str:
        tag = "[AI]" if self.is_ai else f"[{self.player_nick}]"
        return f"{self.fighter['emoji']} \x02{self.fighter_name}\x02{tag}"

    def short_label(self) -> str:
        tag = "[AI]" if self.is_ai else f"[{self.player_nick}]"
        return f"{self.fighter_name}{tag}"

    def hp_display(self) -> str:
        bar = hp_bar(self.hp, self.max_hp)
        return f"  {self.label:<28}  {bar}  {self.hp}/{self.max_hp}HP"

    def reset_round(self):
        self.move      = None
        self.move_time = None
        self.evading   = False


class BrawlFight:
    """One active fight (1v1 or Royal Rumble)."""

    def __init__(
        self,
        fight_id:  str,
        mode:      str,                    # "1v1" | "rumble"
        channel:   str,
        say_fn:    Callable[[str], None],  # announces to channel
        bot_say_fn:Callable[[str, str], None],  # fn(fighter_name, text)
        on_end:    Callable[["BrawlFight"], None],
        kick_fn:   Callable[[str, str], None] | None = None,  # fn(nick, reason) — kick on special
    ):
        self.fight_id  = fight_id
        self.mode      = mode
        self.channel   = channel
        self.say       = say_fn
        self.bot_say   = bot_say_fn
        self.on_end    = on_end
        self.kick_fn   = kick_fn   # optional: kick player when hit by special
        self.combatants: list[Combatant] = []
        self.state     = "active"   # active | ended
        self.round_num = 0
        self._timer: threading.Timer | None = None
        self._round_start = 0.0
        self._lock = threading.Lock()

    # ── Setup ──────────────────────────────────────────────────────────────

    def add(self, combatant: Combatant):
        self.combatants.append(combatant)

    def start(self):
        self._announce_start()
        time.sleep(0.6)
        self._next_round()

    # ── Command entry point (called from game controller) ─────────────────

    def submit_move(self, player_nick: str, move_key: str) -> str:
        """Returns: 'ok'|'not_your_fight'|'already_submitted'|'invalid_move'|'special_used'"""
        with self._lock:
            if self.state != "active":
                return "not_your_fight"
            c = self._get_by_player(player_nick)
            if c is None or not c.alive:
                return "not_your_fight"
            if c.move is not None:
                return "already_submitted"

            fighter = c.fighter
            attacks = fighter["attacks"]
            special = fighter["special"]

            # Normalise "special" keyword
            if move_key == "special":
                if c.special_used:
                    return "special_used"
                move_key = special
                c.special_used = True

            if move_key not in attacks:
                return "invalid_move"

            c.move      = move_key
            c.move_time = time.time()
            elapsed     = c.move_time - self._round_start
            self.say(f"  ✅ {c.short_label()} → \x02{move_key.upper()}\x02  {speed_label(elapsed)}")

            # If all human combatants have submitted, resolve immediately
            humans_pending = [x for x in self._alive() if not x.is_ai and x.move is None]
            if not humans_pending:
                if self._timer:
                    self._timer.cancel()
                threading.Timer(0.4, self._resolve).start()

            return "ok"

    # ── Round flow ─────────────────────────────────────────────────────────

    def _next_round(self):
        alive = self._alive()
        if len(alive) < 2:
            self._end()
            return

        self.round_num += 1
        for c in alive:
            c.reset_round()

        self.say(SEPARATOR)
        self.say(f"\x02⚔️  ROUND {self.round_num}\x02  — submit your attack!")
        self.say("")

        for c in alive:
            attacks   = list(c.fighter["attacks"].keys())
            special   = c.fighter["special"]
            sp_text   = f"  \x02!!special\x02({special.upper()})" if not c.special_used else ""
            atk_list  = "  ".join(
                f"!!\x02{a}\x02" for a in attacks if a != special
            )
            self.say(f"  {c.label}: {atk_list}{sp_text}")

        self.say(f"")
        self.say(f"  ⏱️  {TURN_TIMEOUT}s — fastest correct move wins!")

        self._round_start = time.time()

        # AI combatants submit after random delay
        for c in alive:
            if c.is_ai:
                threading.Thread(target=self._ai_submit, args=(c,), daemon=True).start()

        # Timeout
        self._timer = threading.Timer(TURN_TIMEOUT, self._on_timeout)
        self._timer.start()

    def _on_timeout(self):
        with self._lock:
            if self.state != "active":
                return
        self._resolve()

    def _resolve(self):
        with self._lock:
            if self.state != "active":
                return
            alive = self._alive()

            self.say(SEPARATOR)
            self.say(f"\x02💥 ROUND {self.round_num} — RESULTS:\x02")
            self.say("")

            # Mark timed-out combatants
            for c in alive:
                if c.move is None:
                    c.move = "_timeout"
                    c.move_time = time.time()

            if self.mode == "1v1" and len(alive) == 2:
                self._resolve_pair(alive[0], alive[1])
                self._resolve_pair(alive[1], alive[0])
            else:
                # Rumble: each attacks highest-HP enemy
                for attacker in list(alive):
                    if not attacker.alive:
                        continue
                    targets = [x for x in alive if x is not attacker and x.alive]
                    if not targets:
                        continue
                    target = max(targets, key=lambda x: x.hp)
                    self._resolve_pair(attacker, target)

            # Poison ticks
            self.say("")
            for c in self._alive():
                if c.poison > 0:
                    c.hp = max(0, c.hp - c.poison)
                    self.say(f"  🐍 Poison: {c.short_label()} loses {c.poison}HP!")

            # Reset armor
            for c in self.combatants:
                c.armor = 0

            # Show HP
            self.say("")
            for c in self.combatants:
                self.say(c.hp_display())

            # KO messages (let the fighter bot speak)
            newly_ko = [c for c in alive if not c.alive]
            for c in newly_ko:
                ko_msg = c.fighter.get("ko_msg", "*falls*")
                threading.Timer(0.3, self.bot_say, args=[c.fighter_name, ko_msg]).start()

            # Check win condition
            still_alive = self._alive()
            if len(still_alive) < 2:
                threading.Timer(1.2, self._end).start()
            else:
                threading.Timer(1.5, self._next_round).start()

    def _resolve_pair(self, attacker: Combatant, defender: Combatant):
        if not attacker.alive or not defender.alive:
            return

        move = attacker.move
        if move == "_timeout":
            self.say(f"  ⌛ {attacker.short_label()} timed out — NO MOVE!")
            return

        atk_data = attacker.fighter["attacks"][move]
        elapsed  = (attacker.move_time or time.time()) - self._round_start
        s_mult   = speed_mult(elapsed)

        if s_mult == 0.0:
            self.say(f"  ❌ {attacker.short_label()} was too slow — MISS!")
            return

        # Evade: if attacker used evade move, skip their attack but mark evading
        if atk_data.get("evade"):
            attacker.evading = True
            self.say(f"  👻 {attacker.label} — \x02{atk_data['msg']}\x02 (will dodge next incoming hit)")
            return

        # Heal: affects self only
        if atk_data.get("heal"):
            heal = atk_data["heal"]
            attacker.hp = min(attacker.max_hp, attacker.hp + heal)
            self.say(f"  💚 {attacker.label} — \x02{atk_data['msg']}\x02 (now {attacker.hp}HP)")
            return

        # If defender is evading, miss
        if defender.evading:
            self.say(f"  👻 {defender.short_label()} evades {attacker.short_label()}'s attack!")
            return

        # Type advantage vs defender's move
        atk_type = atk_data.get("type", "POWER")
        def_move = defender.move
        def_type = "POWER"
        if def_move and def_move != "_timeout":
            def_data = defender.fighter["attacks"].get(def_move, {})
            def_type = def_data.get("type", "POWER")

        t_mult, t_label = type_advantage(atk_type, def_type)

        # Base damage
        dmg_range = atk_data.get("damage", (1, 2))
        base_dmg  = random.randint(dmg_range[0], dmg_range[1])
        raw_dmg   = base_dmg * s_mult * t_mult
        final_dmg = max(1, int(raw_dmg))

        # Apply armor absorption
        if defender.armor > 0:
            absorbed   = min(defender.armor, final_dmg)
            final_dmg  = max(0, final_dmg - absorbed)
            defender.armor -= absorbed

        # Apply damage
        defender.hp = max(0, defender.hp - final_dmg)

        # Lifesteal
        if atk_data.get("lifesteal") and final_dmg > 0:
            steal = max(1, final_dmg // 2)
            attacker.hp = min(attacker.max_hp, attacker.hp + steal)
            ls_note = f"  💜 {attacker.short_label()} steals {steal}HP!"
        else:
            ls_note = ""

        # Poison apply
        if atk_data.get("dot"):
            defender.poison += atk_data["dot"]
            poison_note = " 🐍 POISONED!"
        else:
            poison_note = ""

        # Armor apply (deflect / iron_block grants armor)
        if atk_data.get("armor"):
            attacker.armor += atk_data["armor"]

        self.say(
            f"  🥊 {attacker.short_label()} → \x02{atk_data['msg']}\x02"
            f"  \x02{final_dmg} dmg\x02{poison_note}{t_label}"
        )
        if ls_note:
            self.say(ls_note)

        # ── Special attack: IRC kick the hit player (drama!) ─────────────
        is_special = (move == attacker.fighter.get("special"))
        if is_special and final_dmg > 0 and not defender.is_ai and defender.player_nick:
            if self.kick_fn:
                reason = f"💥 {attacker.fighter_name}'s {move.upper()} sent you FLYING! Rejoin when ready!"
                threading.Timer(0.8, self.kick_fn, args=[defender.player_nick, reason]).start()

        # Fighter taunts (30% on special, 20% otherwise)
        taunt_chance = 0.30 if is_special else 0.20
        if random.random() < taunt_chance:
            taunt = random.choice(attacker.fighter.get("taunts", []))
            threading.Timer(0.15, self.bot_say, args=[attacker.fighter_name, taunt]).start()
        elif final_dmg > 0 and random.random() < 0.25:
            hit_msg = random.choice(defender.fighter.get("hit_msgs", ["..."]))
            threading.Timer(0.15, self.bot_say, args=[defender.fighter_name, hit_msg]).start()

    def _end(self):
        with self._lock:
            if self.state == "ended":
                return
            self.state = "ended"
        if self._timer:
            self._timer.cancel()

        alive = self._alive()
        self.say(THICK_SEP)
        if alive:
            w = alive[0]
            if self.mode == "rumble":
                self.say(f"  👑  \x02ROYAL RUMBLE WINNER:  {w.label}!\x02")
            else:
                self.say(f"  🏆  \x02{w.label}  WINS!\x02")
            win_msg = w.fighter.get("win_msg", "...")
            self.bot_say(w.fighter_name, win_msg)
        else:
            self.say(f"  💀  \x02DOUBLE KO — Nobody wins!\x02")
        self.say(THICK_SEP)

        self.on_end(self)

    # ── AI ─────────────────────────────────────────────────────────────────

    def _ai_submit(self, c: Combatant):
        delay = random.uniform(0.8, TURN_TIMEOUT - 1.5)
        time.sleep(delay)
        if self.state != "active" or not c.alive or c.move:
            return

        weights = dict(c.fighter["ai_weights"])
        special = c.fighter["special"]
        if c.special_used:
            weights.pop(special, None)

        if not weights:
            return
        moves  = list(weights.keys())
        w_vals = list(weights.values())

        # Smart AI: prefer type advantage
        alive_enemies = [x for x in self._alive() if x is not c]
        if alive_enemies:
            enemy = alive_enemies[0]
            em    = enemy.move
            if em and em != "_timeout":
                em_type = enemy.fighter["attacks"].get(em, {}).get("type", "POWER")
                # Find a move that beats enemy's type
                for atk_key, atk_data in c.fighter["attacks"].items():
                    if TYPE_BEATS.get(atk_data["type"]) == em_type and atk_key in moves:
                        idx = moves.index(atk_key)
                        w_vals[idx] = w_vals[idx] * 2  # double weight for counters

        chosen = random.choices(moves, weights=w_vals)[0]
        c.move       = chosen
        c.move_time  = time.time()
        if chosen == special:
            c.special_used = True

    # ── Helpers ────────────────────────────────────────────────────────────

    def _alive(self) -> list[Combatant]:
        return [c for c in self.combatants if c.alive]

    def _get_by_player(self, nick: str) -> Combatant | None:
        for c in self.combatants:
            if c.player_nick and c.player_nick.lower() == nick.lower():
                return c
        return None

    def _announce_start(self):
        names = "  vs  ".join(c.short_label() for c in self.combatants)
        if self.mode == "rumble":
            header = f"  👑  ROYAL RUMBLE  —  {len(self.combatants)} fighters enter!"
        else:
            header = f"  🥊  BATCAVE BRAWL  —  {names}"

        self.say(THICK_SEP)
        self.say(header)
        self.say(THICK_SEP)
        time.sleep(0.3)
        for c in self.combatants:
            intro = c.fighter.get("intro", "...")
            self.bot_say(c.fighter_name, intro)
            time.sleep(0.25)
        time.sleep(0.4)
        self.say("")
        for c in self.combatants:
            self.say(c.hp_display())
        self.say("")
        self.say(f"  Type your fighter's attacks when the round starts!")
        self.say(f"  !!yield to surrender  |  !!stats for your record")


class BrawlEngine:
    """Manages all active fights and player registrations."""

    def __init__(self):
        self._fights: dict[str, BrawlFight] = {}      # fight_id  → BrawlFight
        self._player_fight: dict[str, str]  = {}      # player    → fight_id
        self._pending: dict[str, dict]       = {}     # challenger → {target, fighter, ts}
        self._xp: dict[str, int]             = {}     # player    → int
        self._wins: dict[str, int]           = {}
        self._losses: dict[str, int]         = {}
        self._fight_counter = 0
        self._lock = threading.Lock()

    # ── Player registration ────────────────────────────────────────────────

    def in_fight(self, nick: str) -> bool:
        return nick.lower() in self._player_fight

    def get_fight(self, nick: str) -> BrawlFight | None:
        fid = self._player_fight.get(nick.lower())
        return self._fights.get(fid) if fid else None

    def register_fight(self, fight: BrawlFight):
        with self._lock:
            self._fight_counter += 1
            self._fights[fight.fight_id] = fight
            for c in fight.combatants:
                if c.player_nick:
                    self._player_fight[c.player_nick.lower()] = fight.fight_id

    def finish_fight(self, fight: BrawlFight):
        with self._lock:
            alive = [c for c in fight.combatants if c.alive]
            dead  = [c for c in fight.combatants if not c.alive]

            for c in alive:
                if not c.is_ai and c.player_nick:
                    xp_gain = 50 if fight.mode == "1v1" else 80
                    self._add_xp(c.player_nick, xp_gain)
                    self._wins[c.player_nick.lower()] = self._wins.get(c.player_nick.lower(), 0) + 1

            for c in dead:
                if not c.is_ai and c.player_nick:
                    self._add_xp(c.player_nick, 10)   # consolation XP
                    self._losses[c.player_nick.lower()] = self._losses.get(c.player_nick.lower(), 0) + 1

            # Clean up
            for c in fight.combatants:
                if c.player_nick:
                    self._player_fight.pop(c.player_nick.lower(), None)
            self._fights.pop(fight.fight_id, None)

    def new_fight_id(self) -> str:
        self._fight_counter += 1
        return f"fight_{self._fight_counter}"

    # ── Challenges ────────────────────────────────────────────────────────

    def set_challenge(self, challenger: str, target: str, c_fighter: str, t_fighter: str | None):
        self._pending[challenger.lower()] = {
            "target":    target,
            "c_fighter": c_fighter,
            "t_fighter": t_fighter,
            "ts":        time.time(),
        }

    def get_challenge(self, challenger: str) -> dict | None:
        ch = self._pending.get(challenger.lower())
        if ch and time.time() - ch["ts"] < 60:
            return ch
        self._pending.pop(challenger.lower(), None)
        return None

    def clear_challenge(self, challenger: str):
        self._pending.pop(challenger.lower(), None)

    def find_challenge_for(self, target: str) -> tuple[str, dict] | tuple[None, None]:
        for challenger, ch in list(self._pending.items()):
            if ch["target"].lower() == target.lower():
                if time.time() - ch["ts"] < 60:
                    return challenger, ch
                else:
                    del self._pending[challenger]
        return None, None

    # ── XP / Ranks ────────────────────────────────────────────────────────

    def _add_xp(self, nick: str, amount: int):
        self._xp[nick.lower()] = self._xp.get(nick.lower(), 0) + amount

    def get_stats(self, nick: str) -> dict:
        nl = nick.lower()
        xp = self._xp.get(nl, 0)
        return {
            "xp":     xp,
            "rank":   get_rank(xp),
            "wins":   self._wins.get(nl, 0),
            "losses": self._losses.get(nl, 0),
        }

    def leaderboard(self, top_n: int = 5) -> list[tuple[str, int, str]]:
        sorted_xp = sorted(self._xp.items(), key=lambda x: x[1], reverse=True)
        return [(nick, xp, get_rank(xp)) for nick, xp in sorted_xp[:top_n]]

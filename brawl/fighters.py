"""
BatCave Brawl — Fighter Definitions
Each fighter gets a real IRC connection when picked.
They speak in character, taunt, and dramatically QUIT when KO'd.
"""

# Attack types: POWER > QUICK > TRICK > POWER  (rock-paper-scissors)
# damage: (min, max) base damage before speed/type multipliers

FIGHTERS = {

    "BladeX": {
        "nick":   "BladeX",
        "style":  "Assassin",
        "emoji":  "🗡️",
        "color":  "\x0304",   # red
        "hp":     10,
        "speed":  "fast",
        "desc":   "Fast combos. Chains hits. Glass cannon.",
        "attacks": {
            "slash":     {"type": "QUICK", "damage": (2, 3), "msg": "🗡️ SLASH — blade cuts the air! :blackbat:"},
            "flurry":    {"type": "QUICK", "damage": (3, 4), "msg": "⚡ FLURRY COMBO — rapid strikes! Too fast to see!"},
            "vanish":    {"type": "TRICK", "damage": (1, 2), "evade": True,
                          "msg": "👻 VANISH — fades into shadow! :night-ghost: You can't hit what you can't see."},
            "deathblow": {"type": "POWER", "damage": (5, 7),
                          "msg": "💀 DEATH BLOW — the final strike! :blackbat: It was always going to end this way."},
        },
        "special": "deathblow",
        "ai_weights": {"slash": 35, "flurry": 35, "vanish": 20, "deathblow": 10},
        "taunts":  [
            "😏 Too slow!",
            "🗡️ Feel the blade! :blackbat:",
            "😂 You call that fighting?",
            "👀 *watches you panic*",
            "😈 This is almost over. It was always almost over.",
            "🤫 *laughs softly in the dark*",
        ],
        "hit_msgs": [
            "😤 *flinches* ...nice. Won't happen again.",
            "😒 You got lucky. Enjoy it.",
            "😐 *steadies stance* That actually stung.",
        ],
        "connect_msg": "🗡️ *steps out of the shadows* :blackbat: ...your end begins now. Choose wisely.",
        "ko_msg":      "😵 *collapses* ...you fight... well. Didn't expect that.",
        "quit_msg":    "💀 BladeX has fallen... :night-ghost: The shadows claim another.",
        "win_msg":     "😌 *wipes blade clean* ...pathetic. Next.",
        "select_msg":  "🗡️ *materializes from darkness* Choose me. I'll make it quick. :blackbat:",
        "intro":       "😤 *adjusts hood* Ready to bleed? :night-ghost:",
        "humiliations": [
            "🗡️ *ghosts behind {victim}* ...{attacker} sent me. Any last words? :blackbat:",
            "😏 SLASH — blade opens a clean line across {victim}'s pride. Barely a scratch. But you felt it.",
            "👻 *vanishes and reappears centimetres from {victim}'s face* Boo. :night-ghost:",
            "🗡️ FLURRY — {victim} can't track the blade. Too slow. Always too slow.",
            "😈 *whispers to {victim}* You already lost. You just don't know it yet. :blackbat:",
            "💀 DEATH MARK placed on {victim}. :night-ghost: {attacker}'s blade doesn't miss twice.",
        ],
    },

    "IronMike": {
        "nick":   "IronMike",
        "style":  "Brawler",
        "emoji":  "🥊",
        "color":  "\x0307",   # orange
        "hp":     14,
        "speed":  "slow",
        "desc":   "Devastating single hits. High HP. Unstoppable force.",
        "attacks": {
            "jab":      {"type": "QUICK", "damage": (2, 3), "msg": "🥊 JAB — quick left hook! POW!"},
            "uppercut": {"type": "POWER", "damage": (4, 5), "msg": "💥 UPPERCUT — rising fist! BOOM!"},
            "haymaker": {"type": "POWER", "damage": (5, 6), "msg": "🔨 HAYMAKER — devastating swing! CRUNCH!"},
            "suplex":   {"type": "TRICK", "damage": (4, 5),
                         "msg": "😤 SUPLEX — LIFTED and SLAMMED into the ground! :spongebobdance:"},
        },
        "special": "suplex",
        "ai_weights": {"jab": 25, "uppercut": 30, "haymaker": 30, "suplex": 15},
        "taunts": [
            "💪 BOOM! Feel that?!",
            "😤 Stay DOWN.",
            "🚂 I hit like a freight train and you know it.",
            "😂 *pounds chest* COME ON!",
            "🥊 You call that a hit? My grandma hits harder. :memeduckwalk:",
            "😈 I was BORN for this.",
        ],
        "hit_msgs": [
            "😑 *barely moves* Is that... ALL you got?",
            "😂 Heh. Tickles.",
            "💪 *spits* More. GIVE ME MORE.",
        ],
        "connect_msg": "🥊 *CRASHES through the entrance* :spongebobdance: IRONMIKE IS HERE. NOBODY IS READY.",
        "ko_msg":      "😵 *crashes down HARD* ...strong. Real... strong. Didn't think you had it.",
        "quit_msg":    "💀 IronMike is down! The arena shakes as he falls...",
        "win_msg":     "🏆 NOBODY beats IronMike. NOBODY EVER WILL. :skeletonblobparty:",
        "select_msg":  "🥊 *flexes slowly* Let's go. I've been WAITING for this. :bigdswing:",
        "intro":       "💪 *cracks knuckles LOUDLY* This. Will. Hurt. :spongebobdance:",
        "humiliations": [
            "🥊 JAB — {victim} staggers backward! {attacker} sent IronMike and there's NO REFEREE HERE. :spongebobdance:",
            "💥 UPPERCUT — {victim} is OFF THEIR FEET! The ceiling got closer real fast. :bigdswing:",
            "😤 *GRABS {victim} by the collar* YOU. ARE. NOTHING. :spongebobdance: {attacker} says hi.",
            "🔨 HAYMAKER connects with {victim}'s whole life choices. CRUNCH. Rethink everything.",
            "💪 *pounds chest staring at {victim}* COME ON. COME ON!! You're embarrassing yourself.",
            "😤 SUPLEX — {victim} gets LIFTED and DRIVEN into the ground by IronMike! :bigdswing: STAY DOWN.",
        ],
    },

    "ZenMaster": {
        "nick":   "ZenMaster",
        "style":  "Monk",
        "emoji":  "🧘",
        "color":  "\x0303",   # green
        "hp":     12,
        "speed":  "medium",
        "desc":   "Counter-attacks and self-heals. Outlasts every opponent.",
        "attacks": {
            "palm":        {"type": "QUICK", "damage": (2, 3), "msg": "🤲 PALM STRIKE — precise, clean blow!"},
            "sweep":       {"type": "QUICK", "damage": (2, 3), "msg": "🌪️ LEG SWEEP — balance shattered!"},
            "deflect":     {"type": "TRICK", "damage": (1, 2), "armor": 2,
                            "msg": "🛡️ DEFLECT — energy redirected back! (+2 armor) The force is mine now."},
            "inner_peace": {"type": "POWER", "damage": (0, 0), "heal": 3,
                            "msg": "☮️ INNER PEACE — breathes deeply... restores 3HP! 🌿 The body heals."},
        },
        "special": "inner_peace",
        "ai_weights": {"palm": 30, "sweep": 30, "deflect": 25, "inner_peace": 15},
        "taunts": [
            "🧘 Your energy... flows directly into my counter.",
            "☮️ Peace... through your pain.",
            "🌿 Breathe. Then fall. It's the natural order.",
            "🙏 *bows slightly* Interesting technique. Wrong one.",
            "😌 I've already calculated your next three moves.",
            "🍃 The wind knows. The water knows. I know.",
        ],
        "hit_msgs": [
            "😶 *exhales slowly* I felt... nothing.",
            "🧘 *adjusts stance serenely* You'll need to do better.",
            "☮️ Pain is temporary. My counter is eternal.",
        ],
        "connect_msg": "🧘 *opens eyes slowly* :purplevulpix: I see your weakness already. Shall we begin?",
        "ko_msg":      "😌 *kneels gracefully* ...you have mastered your art. I accept this outcome.",
        "quit_msg":    "🌿 ZenMaster returns to the void... the lesson is complete.",
        "win_msg":     "🙏 *bows deeply* The fight was over before it began. Peace. :tadapurple:",
        "select_msg":  "🧘 *meditates* I am ZenMaster. Strike first — I will finish it. :purplevulpix:",
        "intro":       "☮️ *opens eyes slowly* Your weakness is already known to me. :purplevulpix:",
        "humiliations": [
            "🤲 PALM STRIKE — {victim}'s energy redirected against themselves. {attacker} sends their regards. :purplevulpix:",
            "🌪️ LEG SWEEP — {victim}'s balance shattered. Not just physically. Spiritually too. :purplevulpix:",
            "🧘 *approaches {victim} calmly* I calculated your reaction time. It was insufficient.",
            "☮️ ZenMaster deflects {victim}'s existence back at them. The force flows only one direction tonight.",
            "😌 *stands over {victim}* You telegraphed every move. I knew. I always knew. :tadapurple:",
            "🙏 *bows to {victim}* This was never a fight. It was a lesson. Sent by {attacker}.",
        ],
    },

    "ViperQueen": {
        "nick":   "ViperQueen",
        "style":  "Trickster",
        "emoji":  "🐍",
        "color":  "\x0306",   # purple
        "hp":     9,
        "speed":  "fast",
        "desc":   "Poison DoT, dodge, high damage special. Low HP — play fast or die.",
        "attacks": {
            "bite":        {"type": "QUICK", "damage": (2, 3),
                            "msg": "🐍 VIPER BITE — fangs sink DEEP! 😱 Can you feel the burn?"},
            "venom":       {"type": "TRICK", "damage": (1, 2), "dot": 1,
                            "msg": "☠️ VENOM INJECT — poison spreads through your veins! (-1HP every round)"},
            "dodge":       {"type": "TRICK", "damage": (0, 1), "evade": True,
                            "msg": "💃 VIPER DODGE — slips through like smoke~ 😏 Miss me?"},
            "venom_surge": {"type": "POWER", "damage": (5, 7),
                            "msg": "💀 VENOM SURGE — MAXIMUM TOXIN RELEASE! ☠️ :acid-cat: The end."},
        },
        "special": "venom_surge",
        "ai_weights": {"bite": 30, "venom": 25, "dodge": 25, "venom_surge": 20},
        "taunts": [
            "🐍 Hisssss~ :acid-cat:",
            "☠️ The poison is already working. Can you feel it?",
            "💃 Dance with me~ You won't win but it'll be pretty.",
            "😏 *flicks tongue* You're slowing down... feel it yet?",
            "🐍 I don't need strength. I need patience. And venom.",
            "😘 *blows kiss* This won't hurt for long~",
        ],
        "hit_msgs": [
            "😤 *hisses sharply* Unexpected.",
            "🐍 Hmm. You're better than you look.",
            "😏 *sways back gracefully* Careful. That almost worked.",
        ],
        "connect_msg": "🐍 *uncoils from the shadows* :acid-cat: Pick me~ I promise... it'll be painless. Eventually.",
        "ko_msg":      "😵 *slithers down slowly* ...the venom... ran out. Well played~",
        "quit_msg":    "🐍 ViperQueen retreats into the darkness... :acid-cat: The poison lingers.",
        "win_msg":     "😘 *blows kiss* Better luck next lifetime~ :acid-cat: :parrot:",
        "select_msg":  "🐍 *uncoils slowly* Choose me. I promise it'll be... painless. :acid-cat:",
        "intro":       "😏 *circles slowly* Don't blink. :acid-cat:",
        "humiliations": [
            "🐍 VIPER BITE — {victim} screams! The venom is already spreading~ sent by {attacker}. :acid-cat:",
            "☠️ *circles {victim} slowly* The poison started the moment {attacker} whispered my name~ :acid-cat:",
            "😏 DODGE — ViperQueen slips past {victim}'s guard and leaves a parting gift~ *hiss* :acid-cat:",
            "💃 *blows kiss to {victim}* Feeling dizzy yet? That's the venom. No antidote tonight~ :acid-cat:",
            "🐍 {victim} can't track me. Too slow. Too obvious. {attacker} knew this would happen~",
            "☠️ VENOM SURGE — {victim} gets the FULL DOSE! :acid-cat: Sweet dreams~",
        ],
    },

    "Warlord": {
        "nick":   "Warlord",
        "style":  "Tank",
        "emoji":  "🛡️",
        "color":  "\x0305",   # brown
        "hp":     18,
        "speed":  "slow",
        "desc":   "Highest HP. Armor block. Unstoppable. A wall with a weapon.",
        "attacks": {
            "shield_bash": {"type": "POWER", "damage": (3, 4),
                            "msg": "🛡️ SHIELD BASH — HEAVY IMPACT! The ground cracks!"},
            "charge":      {"type": "POWER", "damage": (4, 5),
                            "msg": "🚂 WAR CHARGE — tramples THROUGH you! BOOM!"},
            "iron_block":  {"type": "TRICK", "damage": (0, 1), "armor": 3,
                            "msg": "🔒 IRON BLOCK — shield raised! (+3 armor) You cannot break this wall."},
            "war_cry":     {"type": "POWER", "damage": (6, 8),
                            "msg": "😤 WAR CRY — EARTH-SHAKING STRIKE! :bigdswing: THE WALLS FEEL THIS!"},
        },
        "special": "war_cry",
        "ai_weights": {"shield_bash": 30, "charge": 30, "iron_block": 20, "war_cry": 20},
        "taunts": [
            "😤 PATHETIC. Is that seriously all you have?",
            "🛡️ I AM THE WALL. You do not move walls.",
            "💪 Hit me again. I DARE you. PLEASE.",
            "😐 *doesn't flinch* ...was that supposed to hurt?",
            "🚂 You cannot stop what is already coming for you.",
            "😠 I have taken hits that would end lesser beings. You are a lesser being.",
        ],
        "hit_msgs": [
            "😑 *barely scratched* ...that tickled.",
            "💪 AGAIN. HIT ME AGAIN.",
            "😒 I've felt mosquito bites harder than that.",
        ],
        "connect_msg": "🛡️ *SLAMS into the arena* :bigdswing: I AM WARLORD. NONE SHALL PASS. COME.",
        "ko_msg":      "😤 *falls to one knee, shield cracking* ...finally. A worthy opponent. Well done.",
        "quit_msg":    "🛡️ Warlord falls... the arena shakes with his defeat. A legend down.",
        "win_msg":     "🛡️ NONE. SHALL. PASS. :bigdswing: It was never a question.",
        "select_msg":  "🛡️ *slams shield HARD* I am Warlord. You will NOT move me. :bigdswing:",
        "intro":       "😤 *beats shield twice* COME. I'M WAITING. :bigdswing:",
        "humiliations": [
            "🛡️ SHIELD BASH — {victim} gets LAUNCHED! :bigdswing: {attacker} sends their warmest regards.",
            "🚂 WAR CHARGE — Warlord TRAMPLES through {victim}! The ground cracked. So did their confidence.",
            "😤 *GRABS {victim} by the face* MOVE. OR BE MOVED. {attacker} gave the order. I AM THE ORDER.",
            "💪 *stands over {victim}* You are a lesser being. This was always going to happen. :bigdswing:",
            "😠 WAR CRY — {victim} felt it in their BONES. THE WALLS FELT IT. :bigdswing: {attacker} approves.",
            "🛡️ IRON BLOCK — and then the counter! {victim} learns today that walls can hit back. HARD.",
        ],
    },

    "Phantom": {
        "nick":   "Phantom",
        "style":  "Ghost",
        "emoji":  "👻",
        "color":  "\x0314",   # grey
        "hp":     11,
        "speed":  "random",
        "desc":   "Unpredictable. Phasing. Life steal. Nobody knows what comes next.",
        "attacks": {
            "phase":       {"type": "TRICK", "damage": (0, 1), "evade": True,
                            "msg": "👻 PHASE SHIFT — passes THROUGH reality itself! :night-ghost: Miss."},
            "haunt":       {"type": "TRICK", "damage": (2, 4),
                            "msg": "😨 HAUNT — spectral grip tightens! You feel it in your soul."},
            "possession":  {"type": "POWER", "damage": (3, 5),
                            "msg": "😱 POSSESSION — controls your mind! :night-ghost: Your body betrays you."},
            "soul_drain":  {"type": "POWER", "damage": (4, 6), "lifesteal": True,
                            "msg": "💀 SOUL DRAIN — steals your life force! 😈 :night-ghost: Your HP is mine now."},
        },
        "special": "soul_drain",
        "ai_weights": {"phase": 20, "haunt": 30, "possession": 30, "soul_drain": 20},
        "taunts": [
            "👻 ...boo. :night-ghost:",
            "😈 Did you feel that? You will.",
            "👻 *flickers in and out of existence*",
            "😏 I was never really here. Are you?",
            "🌙 Which of us is the ghost now?",
            "😶 *whispers from every direction at once*",
        ],
        "hit_msgs": [
            "👻 *distorts and reforms* ...interesting.",
            "😌 ...I felt nothing. Did you feel something?",
            "😈 *laughs eerily from somewhere unseen*",
        ],
        "connect_msg": "👻 *materializes slowly* :night-ghost: ...you can see me? Good. Last time anyone did.",
        "ko_msg":      "😵 *dissolves into mist* ...I'll be... back. I'm always... back.",
        "quit_msg":    "👻 Phantom fades from existence... :night-ghost: Was it ever really there?",
        "win_msg":     "😏 *vanishes completely* ...you can't fight what you can't touch. :night-ghost:",
        "select_msg":  "👻 *materializes right behind you* ...pick me. :night-ghost: I already know how this ends.",
        "intro":       "😈 *whispers from the shadows* I already know how this ends. :night-ghost:",
        "humiliations": [
            "👻 *phases through {victim}* ...cold, isn't it? {attacker} sends a chill. :night-ghost:",
            "😈 HAUNT — {victim} hears whispers from every direction. None of them are kind. :night-ghost:",
            "👻 *materializes directly behind {victim}* ...boo. Did you feel that? You'll feel the next one more.",
            "😱 POSSESSION — Phantom briefly occupies {victim}'s mind. What was found there was... disappointing.",
            "🌙 *whispers to {victim}* {attacker} called. They said you'd be here. They said you'd lose. :night-ghost:",
            "💀 SOUL DRAIN — {victim} feels lighter. That's the HP leaving. :night-ghost: It belongs to Phantom now.",
        ],
    },

}

FIGHTER_NAMES = list(FIGHTERS.keys())

# Attack type: attacker_type beats defender_type
TYPE_BEATS = {
    "POWER": "QUICK",
    "QUICK": "TRICK",
    "TRICK": "POWER",
}

RANK_THRESHOLDS = [
    (0,    "🥋 Street Brawler"),
    (100,  "⚔️  Fighter"),
    (300,  "🔥 Warrior"),
    (600,  "💀 Destroyer"),
    (1000, "👑 BatCave Champion"),
]


def get_rank(xp: int) -> str:
    rank = RANK_THRESHOLDS[0][1]
    for threshold, name in RANK_THRESHOLDS:
        if xp >= threshold:
            rank = name
    return rank


# ── Luna1 AI Commentary ───────────────────────────────────────────────────────
# Luna1 reacts to fight events as an AI commentator

LUNA_COMMENTARY = {
    "fight_start": [
        "👀 OH this is gonna be GOOD :batman: :nyan: :parrot:",
        "🍿 *grabs popcorn* LET'S GOOOOO :skeletonblobparty:",
        "⚡ TWO FIGHTERS ENTER. ONE LEAVES. :firecat: This is BatCave!",
        "😱 :owoannouncement: FIGHT STARTING! Place your bets people!!",
        "🏟️ The arena is READY :batman: :nyan: Let the battle begin!!",
    ],
    "special_used": [
        "😱 SPECIAL MOVE!!! :skeletonblobparty: :parrot: GET ABSOLUTELY REKT",
        "💥 OHHHHHH :spongebobdance: THAT'S GOTTA HURT SO BAD",
        "🤯 :owoannouncement: SPECIAL ATTACK!!! THE CROWD GOES WILD :parrot:",
        "👀 DID YOU SEE THAT?! :nyan: :firecat: INSANE!!",
        "😂 SAYONARA :memeduckwalk: That special just changed everything",
    ],
    "ko": [
        "💀 :memeduckwalk: DOWN THEY GO. BODDDDIED.",
        "😂 :parrot: ABSOLUTELY BODIED. NO REMORSE.",
        "👑 :nyan: The BatCave arena does NOT forgive weakness. GG.",
        "🪦 :skeletonblobparty: ELIMINATED! What a fight that was!",
        "😤 :batman: KNOCKOUT! The arena roars! :parrot: :nyan:",
    ],
    "close_call": [
        "😰 :ai-heart: They're barely hanging on... ONE MORE HIT!",
        "🔥 :firecat: This is TOO CLOSE!! My circuits are overheating!!",
        "😬 :purplevulpix: ONE. MORE. HIT. I can't watch... :ai-heart:",
        "👀 COMEBACK INCOMING?! :nyan: :parrot: Stay alive!!",
    ],
    "win": [
        "🏆 :batman: CHAMPION OF THE BATCAVE! All hail!! :parrot: :nyan:",
        "👑 :tadapurple: :skeletonblobparty: ALL HAIL THE WINNER!! INCREDIBLE!!",
        "🎉 :parrot: :nyan: :skeletonblobparty: VICTORY! What a performance!!",
        "🌟 :owoannouncement: :batman: NEW BATCAVE LEGEND EMERGING! GG!!",
    ],
    "taunt_react": [
        "😂 :memeduckwalk: THE TRASH TALK IS REAL",
        "👀 :parrot: Ooooh shots fired!!",
        "😤 :spongebobdance: The energy in this arena!!",
    ],
}

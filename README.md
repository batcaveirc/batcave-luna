# Luna — Discord ↔ IRC relay

Luna bridges a Discord channel to an IRC channel on HybridIRC. Anything said on
one side appears on the other. She runs on **GitHub Actions**, the same way the
Vampire bot does: a job runs for just under 6 hours, a `*/5` cron starts the
next one as soon as it ends, and `cancel-in-progress: false` keeps exactly one
alive at a time.

Rooms: she joins `#🅱🅰🆃🅲🅰🆅🅴` and `#batcave` as `Luna1`, but **only the
emoji room is relayed**. `#batcave` is joined and moderated with nothing
crossing to Discord — run `$ircjoin #batcave` on the Discord side to start
relaying it too. Being present and being republished elsewhere are separate
decisions, and the second one should always be deliberate.

## Auto-moderation

`$mod on|off` (owner only, **off by default**). Dracula is the primary
moderator in both rooms and already handles profanity, flood, caps, repeats,
raids and bad nicks. Luna deliberately does **not** repeat any of that — two
bots kicking one person for one line is worse than either doing it alone. She
covers what a word list structurally cannot see:

- **disguised text** — Cyrillic and Greek look-alikes, fullwidth forms, and
  zero-width characters wedged between letters, all folded back to ASCII
  before checking. This is how a determined person walks past a word filter.
- **mass highlights** — naming a dozen people at once so every client beeps.
- **colour-code flooding** — control characters that make a client unreadable.
- **advertising** — invites to other servers and networks.
- **walls of text** and **join/part cycling** (one person alone never trips a
  raid guard, which counts distinct users).

Warn first, kick on the second offence, **never a ban**. Channel operators,
the other bots and anyone in `LUNA_WHITELIST_IRC` are never touched.

## Setup

Add these as repository **Secrets** (Settings → Secrets and variables →
Actions). Everything else has a working default in the workflow.

| Secret | Why |
|---|---|
| `DISCORD_TOKEN` | the bot's Discord login |
| `DISCORD_GUILD_ID` | restricts her to one server |
| `OWNER_IDS` | comma-separated Discord user IDs with full command access |
| `IRC_NICKSERV_PASS` | identifies `Luna1` so the nick isn't force-renamed |

Optional: `IRC_NICK`, `IRC_CHANNEL`, `BRIDGE_CHANNEL`, `MOD_ROLE`,
`LUNA_CREDIT`, `LUNA_OWNERS_IRC`, `LUNA_PRIMARY_OWNER`, `GROQ_API_KEY`,
`STARALIGN_RELAY_URL` + `STARALIGN_RELAY_SECRET`, `LUNA_PREFIX`.

Then run the workflow once from the Actions tab; after that the cron keeps it
going on its own.

## Commands

Luna's prefix is **`$`** everywhere — Discord and IRC. `!` belongs to the
Vampire bot and `!!` to Dracula, so a shared prefix would mean two bots racing
the same line.

**`$help`** in the IRC channel explains the relay and lists the commands;
`$help all` prints the full set.

In Discord: `$ircjoin #channel` maps the current Discord channel to an IRC
channel, `$ircpart` unmaps it, `$ircbridges` lists the mappings,
`$ircinfo` / `$ircping` report status. `$ai <question>` asks Luna something.

## What this repo deliberately does not contain

This repository is **public** — that is what makes the Actions minutes free —
so nothing here identifies a real person or a member of the room:

- **No secrets.** Every credential is read from the environment. There is no
  `.env` in git and `.gitignore` blocks one from being added.
- **No nicknames in source.** The owner's IRC handle and the author credit come
  from `LUNA_PRIMARY_OWNER` / `LUNA_OWNERS_IRC` / `LUNA_CREDIT`.
- **No chat history.** `data/` and `*.db` are ignored; the relay keeps its
  state in memory and rebuilds it on each run.

## Privacy rules built into the relay

- **Direct messages are never relayed.** A PM to Luna on IRC is dropped, and
  the Discord→IRC path requires `message.guild`, so a Discord DM has no route
  out. Only messages in an explicitly bridged channel cross over.
- **Relayed lines are tagged `:discord:` and nothing more** — the tag marks a
  message as coming from elsewhere without naming where.
- Bots in a bridged channel relay under the single shared identity `bot`.

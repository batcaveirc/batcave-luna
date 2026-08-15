# Luna — Discord ↔ IRC relay

Luna bridges a Discord channel to an IRC channel on HybridIRC. Anything said on
one side appears on the other. She runs on **GitHub Actions**, the same way the
Vampire bot does: a job runs for just under 6 hours, a `*/5` cron starts the
next one as soon as it ends, and `cancel-in-progress: false` keeps exactly one
alive at a time.

Default room: `#🅱🅰🆃🅲🅰🆅🅴`, as `Luna1`.

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
`STARALIGN_RELAY_URL` + `STARALIGN_RELAY_SECRET`, `LUNA_BRAWL`.

Then run the workflow once from the Actions tab; after that the cron keeps it
going on its own.

## Commands

In Discord: `!!ircjoin #channel` maps the current Discord channel to an IRC
channel, `!!ircpart` unmaps it, `!!ircbridges` lists the mappings,
`!!ircinfo` / `!!ircping` report status.

## notsobot from IRC

Anyone in the bridged IRC channel can drive notsobot using the same syntax it
uses in Discord:

```
.img black cat
.magik https://example.com/photo.png
.luna                 ← lists every command that works
```

Luna types the command into the Discord channel, waits for notsobot's reply,
and relays the resulting image URL back to IRC. `!img` also works, for muscle
memory. Image commands like `.edit` and `.magik` need something to work on, so
pass an image URL — an IRC user has nothing to attach.

**Only image and text toys are forwarded, never moderation.** The list is an
allowlist (`_NOTSOBOT_DEFAULT_CMDS` in `utils/irc_bridge.py`); without one,
anybody in the IRC room could make Luna run `.ban` or `.purge` in Discord under
her own permissions. Add new commands with the `NOTSOBOT_EXTRA_CMDS` secret
(comma-separated) — a hardcoded denylist of destructive verbs still applies, so
a typo there cannot open that door.

Guards: 8s per person between requests, 4 in flight per channel, and a request
that goes unanswered for 60s says so in the room instead of leaving a "fetching
…" hanging forever.

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

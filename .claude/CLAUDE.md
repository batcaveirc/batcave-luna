# Project skills — Luna/Andromeda (Discord-IRC bridge, Python)

This bot runs live on GitHub Actions bridging Discord to #BatCave and the
emoji room. Same operational constraints as Dracula; different language.

## Default toolkit for THIS project

**Before implementing:**
- `brainstorming` — the Discord↔IRC relay has subtle double-send and
  echo-loop traps; think it through before coding
- `writing-plans` — any change touching `utils/irc_bridge.py` or the AI
  cog is multi-step, write a plan
- `codebase-design` — the threaded IRC bridge + async Discord loop is a
  deep module; the seams are what makes it readable (`ask_luna`, `_raw`,
  `_remember_line`, `_handle_trust_line`)

**While implementing:**
- `tdd` / `test-driven-development` — write/extend `test_*.py` BEFORE
  changing cogs or the bridge. The `test_teamwork.py` + `test_ai.py` pattern
  (behaviour + source assertions) is the standard here.
- `systematic-debugging` / `diagnosing-bugs` — pair with field-debugging.md.
  Luna's threaded architecture adds race conditions as a bug class; always
  check lock usage first.
- `using-git-worktrees` — never debug the running bot in the live tree
- `worktree` (hidorakai) — concrete setup. Trigger: "set up a worktree for
  <feature>". Copies `.env`, installs deps (`pip install -r
  requirements.txt`), so a side branch can run `python3 test_*.py` without
  touching the main tree. For Python-only (no Prisma step is relevant here).

**Before claiming done:**
- `verification-before-completion` — mandatory before "pushed":
  - `python3 -m py_compile luna.py cogs/ai_cog.py utils/irc_bridge.py`
  - `for t in test_*.py; do python3 "$t"; done` (14 files; all green)
  - `ruby -ryaml -e "YAML.load_file('.github/workflows/luna.yml')"`
  - watch deploy run in_progress past 3 min
- `unlazy` — explicit gates for substantial changes (new provider, new
  Discord cog, new `::` verb). Skip for a one-line prompt tweak.

## Project-specific rituals

**Workflow queues pushes** — this repo (unlike Dracula) has had
`cancel-in-progress: ${{ github.event_name == 'push' }}` since 2026-10-05.
A push now cancels the running scheduled run and deploys immediately. If a
deploy sits "pending" > 2 min, cancel the old run manually.

**AI fallback chain** — `ask()` in `cogs/ai_cog.py`: Groq → Gemini → OpenRouter.
When a provider errors, it falls to the next and logs `[ai] <provider> HTTP N`
to stdout for diagnostic. Don't surface provider plumbing to the IRC room —
that leak was shipped once and reverted (2026-10-04).

**Flood shield in `_raw`** — 10 lines/sec cap on non-protocol writes. PING,
PONG, QUIT bypass. Any new feature that calls `self._raw` in a loop must
NOT exceed this cap. The `_trust_broadcast_ok()` and `_recent_sends` patterns
are templates.

**Memory is thread-safe** — `_user_memory_lock` protects per-user memory.
Any new read or write to `self._user_memory` must take the lock.

## Prefer-NOT skills

- `migrate-to-shoehorn` — TypeScript-only skill, no fit for Python
- `vercel-react-best-practices` — no React here
- `pr-polish`/`pr-review` — push-to-main, same as Dracula

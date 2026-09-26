"""Every command the help ADVERTISES must exist — in BOTH help paths.

This is the "advertised but dead" check, and it is the only test here that
looks for silence rather than for errors. It exists because that failure keeps
shipping: a command listed in help whose handler was never written, or written
and then discarded by a branch above it.

It also caught a subtler one — this project has TWO help implementations, an
embed in luna.py for Discord and cmd_help in shared_cmds.py for IRC. Updating
one and assuming both is exactly how $to came to be missing from the Discord
help while being documented on IRC.

    python3 test_help_audit.py
"""
import re
import pathlib
import glob
import sys


def registered():
    """Every command name and alias the bot actually answers to."""
    names = set()
    for f in glob.glob('cogs/*.py') + ['luna.py']:
        src = pathlib.Path(f).read_text()
        names |= set(re.findall(
            r'@(?:bot|commands)\.command\(\s*name\s*=\s*["\'](\w+)["\']', src))
        for al in re.findall(r'aliases\s*=\s*\[([^\]]*)\]', src):
            names |= set(re.findall(r'["\'](\w+)["\']', al))
    names |= set(re.findall(
        r'def cmd_(\w+)\(', pathlib.Path('shared_cmds.py').read_text()))
    return names


def advertised_discord():
    src = pathlib.Path('luna.py').read_text()
    block = re.search(r'async def help_cmd.*?await ctx\.send\(embed=em\)',
                      src, re.S).group(0)
    return set(re.findall(r'\{p\}(\w+)', block))


def advertised_irc():
    src = pathlib.Path('shared_cmds.py').read_text()
    block = re.search(r'def cmd_help\(self.*?(?=\n    def )', src, re.S).group(0)
    return set(re.findall(r'\{p\}(\w+)', block))


def primaries_and_aliases():
    """Command names split into the real name and the extra names for it.

    An alias does not need its own help entry — $warns is $warnings — but a
    PRIMARY that nothing advertises is a command nobody can discover.
    """
    prim, alias = set(), set()
    for f in glob.glob('cogs/*.py') + ['luna.py']:
        src = pathlib.Path(f).read_text()
        prim |= set(re.findall(
            r'@(?:bot|commands)\.command\(\s*name\s*=\s*["\'](\w+)["\']', src))
        prim |= set(re.findall(
            r'@(?:bot|commands)\.group\(\s*\n?\s*name\s*=\s*["\'](\w+)["\']', src))
        for al in re.findall(r'aliases\s*=\s*\[([^\]]*)\]', src):
            alias |= set(re.findall(r'["\'](\w+)["\']', al))
    return prim, alias


def irc_commands():
    """Everything an IRC user can actually reach.

    Not just the cmd_ functions any more. The memory commands ($find, $quote,
    $onthisday, $tell, $stats) are answered by the bridge, which hands them to
    Discord's loop and queues the reply back — so they are reachable from IRC
    without being cmd_ functions, and DISCORD_ONLY must not claim otherwise.
    """
    names = set(re.findall(r'def cmd_(\w+)\(',
                           pathlib.Path('shared_cmds.py').read_text()))
    bridge = pathlib.Path('utils/irc_bridge.py').read_text()
    for grp in ('MEMORY_CMDS', 'NSFW_CMDS'):
        block = re.search(grp + r' = \(([^)]*)\)', bridge)
        if block:
            names |= set(re.findall(r'"(\w+)"', block.group(1)))
    # And the third path: a couple are matched straight out of the line handler
    # rather than going through either dispatcher — $ai is one. Missing this made
    # the reachability check below fail on a command that works perfectly well.
    names |= set(re.findall(r'startswith\(f"\{config\.PREFIX\}(\w+)', bridge))
    return names


def discord_only_list():
    src = pathlib.Path('shared_cmds.py').read_text()
    block = re.search(r'DISCORD_ONLY = \{.*?\n    \}', src, re.S).group(0)
    return set(re.findall(r'"(\w+)"', block))


# Commands deliberately not advertised, and why. Anything else that exists
# without appearing in a help listing fails the suite.
HIDDEN = {
    'about':     'a vanity blurb, not something anyone needs told about',
    'batstatus': 'operational internals, and now mod-only',
    's':         'the shared group prefix, reached through its subcommands',
}


def irc_reachable_help():
    """The parts of the IRC help a person in the room reads as "type this here".

    The mod and bridge subtopics deliberately describe DISCORD-side commands and
    say so, so they are excluded. The top-level listing and the memory section
    make no such caveat.
    """
    src = pathlib.Path('shared_cmds.py').read_text()
    block = re.search(r'def cmd_help\(self.*?(?=\n    def )', src, re.S).group(0)
    mem = re.search(r'\[\\x02Memory\\x02\].*?\n            \)', block, re.S)
    top = re.search(r'# The first line states what Luna is\..*?\n        \)', block, re.S)
    text = (mem.group(0) if mem else '') + (top.group(0) if top else '')
    return set(re.findall(r'\{p\}(\w+)', text))


def main():
    fails = 0
    # Bridge-handled commands (memory, adult) are real even though they are
    # not cog @command functions, so they count as existing here.
    have = registered() | irc_commands()
    paths = (('Discord embed', advertised_discord()), ('IRC help', advertised_irc()))

    for label, adv in paths:
        dead = sorted(adv - have)
        ok = not dead
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {len(adv)} advertised, "
              f"{'all real' if ok else 'DEAD: ' + ', '.join(dead)}")
        fails += 0 if ok else 1

    # The command that decides which room Discord talks to. Without it in the
    # help, the second bridged room is unreachable and nobody knows why.
    for label, adv in paths:
        ok = 'to' in adv
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} tells people about $to")
        fails += 0 if ok else 1

    # Added, then absent from every help listing for weeks.
    mods = {'op', 'deop', 'voice', 'devoice', 'irckick', 'ircban'}
    for label, adv in paths:
        missing = sorted(mods - adv)
        ok = not missing
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} lists the mod commands"
              f"{'' if ok else ' — missing ' + ', '.join(missing)}")
        fails += 0 if ok else 1


    # ── the OTHER direction ──────────────────────────────────────────────
    #
    # Everything above asks "does what we advertise exist?". Nothing asked
    # "do we advertise what exists?", and that is the half the room actually
    # complained about — twice. $find, $tell, $stats and $mood shipped and
    # appeared in no help listing an IRC user could read, so as far as the room
    # was concerned they were not there.
    prim, alias = primaries_and_aliases()
    irc = irc_commands()
    everywhere = set()
    for _, a in paths:
        everywhere |= a
    undiscoverable = sorted((prim | irc) - everywhere - alias - set(HIDDEN))
    ok = not undiscoverable
    print(f"  [{'PASS' if ok else 'FAIL'}] every command is advertised somewhere"
          f"{'' if ok else ' — nothing mentions: ' + ', '.join(undiscoverable)}")
    fails += 0 if ok else 1

    stale_hidden = sorted(set(HIDDEN) - prim - irc)
    ok = not stale_hidden
    print(f"  [{'PASS' if ok else 'FAIL'}] the HIDDEN list has no ghosts"
          f"{'' if ok else ' — gone: ' + ', '.join(stale_hidden)}")
    fails += 0 if ok else 1

    # DISCORD_ONLY decides whether IRC says "that lives on Discord" or the flatly
    # untrue "I do not know $find". It was hand-written and 35 commands had
    # drifted out of it. It is a fallback now — elsewhere_on_discord() asks the
    # live bot first — but a fallback that lies is still worth failing over.
    should_be = (prim | alias) - irc
    drifted = sorted(should_be - discord_only_list())
    ok = not drifted
    print(f"  [{'PASS' if ok else 'FAIL'}] DISCORD_ONLY covers every Discord-side command"
          f"{'' if ok else ' — missing ' + str(len(drifted)) + ': ' + ', '.join(drifted[:8])}")
    fails += 0 if ok else 1

    # The check that would have caught me. Everything above asks whether an
    # advertised command EXISTS somewhere; nothing asked whether a command
    # advertised to IRC can be run FROM IRC. I had just listed $seen and $mood
    # in the IRC memory help while both were Discord-only — advertised and dead,
    # the exact failure this project keeps shipping, committed while fixing it.
    unreachable = sorted(irc_reachable_help() - irc_commands())
    ok = not unreachable
    print(f"  [{'PASS' if ok else 'FAIL'}] every command IRC help offers can be run from IRC"
          f"{'' if ok else ' — advertised but unreachable: ' + ', '.join(unreachable)}")
    fails += 0 if ok else 1

    print('\nALL PASS' if not fails else f'\n{fails} FAILED')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())

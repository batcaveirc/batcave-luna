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


def main():
    fails = 0
    have = registered()
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

    print('\nALL PASS' if not fails else f'\n{fails} FAILED')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())

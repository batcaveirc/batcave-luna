"""Who Luna is allowed to act on, and what counts as advertising.

    python3 test_moderation.py

Written after Luna kicked hazel out of #batcave on 2026-09-25, eleven seconds
after Vikram had run BOTH "!!protect add hazel" and "!!trust add hazel". Hazel
had posted an audio clip through the room's own uploader. The transcript:

    [7:42:02] <hazel> Uploaded file: https://kiwiirc.hybridirc.com/files/...webm
    [7:42:03] ← hazel was kicked from #batcave by Noctua (advertising)

Three separate faults lined up:

 1. "kiwiirc.hybridirc.com" contains "irc.hybridirc.com", which the advertising
    pattern reads as an invitation to another network. A guard for this already
    existed — and had been applied to the Discord-side check only. The path that
    actually kicks people never got it. One rule, two implementations, one fixed.

 2. Luna's exempt list was a SECRET read once at startup. Trust lives in ChanServ
    FLAGS, which is what !!trust writes, so a user trusted thirty seconds ago was
    a stranger to her.

 3. "Noctua" is Luna wearing a rotated nick — and her own never-touch list was a
    list of NICKS containing "dracula". A renamed Dracula was one rotation away
    from being moderated by his own colleague.
"""
import sys
import types

import utils.moderation as M

fails = 0


def c(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


UPLOAD = ("Uploaded file: https://kiwiirc.hybridirc.com/files/"
          "b9788d6771dd0a51b97d79bf1ddee0f6/audio-1790340110783.webm")

print("— the link that got hazel kicked —")
c("the room's own file host is not an advertisement",
  not M._ADVERT.search(M._strip_home_links(UPLOAD)),
  "kiwiirc.hybridirc.com contains irc.hybridirc.com")
c("the webchat client is not either",
  not M._ADVERT.search(M._strip_home_links("come back via https://kiwiirc.hybridirc.com/nextclient/")),
  "")
# BOTH paths, because having fixed one of two is the whole reason this happened.
src = __import__("pathlib").Path("utils/moderation.py").read_text()
acts = src.count("_ADVERT.search(_strip_home_links(")
c("every advertising check strips home links, not just one of them",
  acts >= 2 and "_ADVERT.search(text)" not in src,
  f"{acts} stripped call(s); a bare _ADVERT.search(text) still present: "
  f"{'_ADVERT.search(text)' in src}")

print("\n— and a real advertisement still is one —")
for bad in ("join us at irc.otherplace.net", "discord.gg/abcdefg",
            "t.me/somechannel", "join #room on othernet"):
    c(f'"{bad[:28]}" is still caught',
      bool(M._ADVERT.search(M._strip_home_links(bad))))


class FakeBridge:
    def __init__(self, trusted=(), ours=(), loaded=True, here=(), prefix=False):
        self._trusted = {t.lower() for t in trusted}
        self._ours = {o.lower() for o in ours}
        self._loaded = loaded
        self._here = list(here)
        self._prefix = prefix
        self.sent, self.kicked, self.queued = [], [], []

    def is_trusted(self, nick):       return nick.lower() in self._trusted
    def is_one_of_ours(self, nick):   return nick.lower() in self._ours
    def trust_loaded(self):           return self._loaded
    def has_prefix(self, ch, nick):   return self._prefix
    def host_of(self, nick):          return "u@example.net"
    def get_channel_nicks(self, ch):  return self._here
    # The deferred voice-restore timer calls this; without it the test
    # prints errors that are not failures, and noisy output is how real
    # failures get scrolled past.
    def is_nick_in_channel(self, ch, nick): return True
    def send_raw(self, m):            self.sent.append(m)
    def kick_irc(self, n, r, ch):     self.kicked.append((n, r))
    def _queue(self, ch, m):          self.queued.append(m)


def mod(**kw):
    m = M.Moderator.__new__(M.Moderator)
    m.bridge = FakeBridge(**kw)
    m.enabled = True
    m._warns, m._joins = {}, {}
    m._whitelist, m._trusted_masks = set(), []
    m._never = {"chanserv", "nickserv", "dracula"}
    m._peer = "Dracula"
    return m


print("\n— who is off limits —")
m = mod(trusted=("hazel",))
c("somebody on the LIVE trust list is exempt", m._exempt("hazel", "#batcave"),
  "this is the one that would have saved hazel")
m = mod(trusted=())
c("and somebody who is not is fair game", not m._exempt("stranger", "#batcave"))

# The rotation bug I introduced hours before the incident.
m = mod(ours=("nosferatu",))
c("a bot of ours under a ROTATED nick is exempt", m._exempt("Nosferatu", "#batcave"),
  "_never is a list of nicks and both bots now change theirs")
m = mod()
c("an ordinary stranger is not mistaken for one of ours",
  not m._exempt("Nosferatu", "#batcave"))

print("\n— blind means gentle, not confident —")
m = mod(loaded=False)
m._act("#batcave", "hazel", "advertising")
m._act("#batcave", "hazel", "advertising")
m._act("#batcave", "hazel", "advertising")
m._act("#batcave", "hazel", "advertising")
c("with no trust list loaded she never removes anybody", not m.bridge.kicked,
  f"kicked anyway: {m.bridge.kicked} — we do not know who is exempt yet")
c("but she does still act, quietly", bool(m.bridge.sent),
  "doing nothing at all would leave the room unmoderated")

m = mod(loaded=True)
for _ in range(M.WARN_LIMIT):
    m._act("#batcave", "spammer", "advertising")
c("once the list HAS loaded, a persistent offender is still removed",
  bool(m.bridge.kicked), "the failsafe must not disable moderation permanently")

print("\n— is Dracula here? —")
m = mod(here=("Nosferatu", "Luna", "vikram"), ours=("nosferatu",))
c("the peer is recognised under a rotated nick", m._peer_present("#batcave"),
  "otherwise Luna silently takes over the whole job while he is standing there")
m = mod(here=("Luna", "vikram"))
c("and genuinely absent is still absent", not m._peer_present("#batcave"))

print(f"\n{fails} FAILED" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

"""
BatCave Brawl — Lightweight IRC Client
Single-connection mode: one BrawlBot handles everything.
Threaded, auto-reconnects, supports PRIVMSG callbacks.
Uses a send queue with rate-limiting to avoid flood bans.
"""

import queue
import socket
import threading
import time
import re
import logging

log = logging.getLogger("brawl.irc")

RECONNECT_DELAY = 15   # seconds between reconnect attempts
SEND_RATE       = 0.75  # seconds between outbound lines (flood protection)


class IRCClient:
    """Single IRC connection. Can send messages and receive PRIVMSGs."""

    def __init__(
        self,
        nick: str,
        server: str,
        port: int,
        channel: str,
        realname: str = "BatCave Brawler",
        nickserv_pass: str = "",
        ns_account: str = "",
        use_ssl: bool = False,
    ):
        self.nick       = nick
        self.server     = server
        self.port       = port
        self.channel    = channel
        self.realname   = realname
        self.nickserv_pass = nickserv_pass
        self.ns_account    = ns_account
        self.use_ssl       = use_ssl

        self._sock: socket.socket | None = None
        self._running   = False
        self._connected = False
        self._buf       = ""
        self._send_lock = threading.Lock()

        # Rate-limited outbound queue
        self._send_queue: queue.Queue[str | None] = queue.Queue()

        # Callbacks
        self.on_message = None    # fn(nick, target, text)
        self.on_connect = None    # fn()

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self):
        """Connect and start background threads."""
        self._running = True
        threading.Thread(target=self._run_loop,   daemon=True, name=f"irc-{self.nick}").start()
        threading.Thread(target=self._sender_loop, daemon=True, name=f"irc-send-{self.nick}").start()

    def stop(self, quit_msg: str = "BatCave Brawl"):
        self._quit_msg = quit_msg
        self._running = False
        self._send_queue.put(None)   # unblock sender loop
        self._disconnect()

    # ── Sender loop (rate-limited) ─────────────────────────────────────────

    def _sender_loop(self):
        """Drain the send queue at SEND_RATE lines/sec to avoid flood bans."""
        while True:
            item = self._send_queue.get()
            if item is None:
                break   # stop() sentinel
            if self._connected:
                try:
                    self._raw(item)
                except Exception as e:
                    log.warning("[%s] send error: %s", self.nick, e)
            time.sleep(SEND_RATE)

    def say(self, target: str, msg: str):
        """Enqueue PRIVMSG. Rate-limited sender loop drains the queue."""
        if not self._running:
            return
        for chunk in self._split_msg(msg, 400):
            self._send_queue.put(f"PRIVMSG {target} :{chunk}")

    def action(self, target: str, msg: str):
        self.say(target, f"\x01ACTION {msg}\x01")

    def join(self, channel: str):
        self._raw(f"JOIN {channel}")

    def nick_change(self, new_nick: str):
        self._raw(f"NICK {new_nick}")
        self.nick = new_nick

    # ── Internal ───────────────────────────────────────────────────────────

    def _run_loop(self):
        while self._running:
            try:
                self._connect()
                log.info("[%s] connected to %s:%s ssl=%s", self.nick, self.server, self.port, self.use_ssl)
                print(f"[irc/{self.nick}] ✅ connected to {self.server}:{self.port} ssl={self.use_ssl}")
                self._listen()
            except Exception as e:
                log.warning("[%s] connection error: %s", self.nick, e)
                print(f"[irc/{self.nick}] ❌ connection error: {e}")
            if self._running:
                log.info("[%s] reconnecting in %ds…", self.nick, RECONNECT_DELAY)
                print(f"[irc/{self.nick}] reconnecting in {RECONNECT_DELAY}s…")
                time.sleep(RECONNECT_DELAY)

    def _connect(self):
        import ssl as _ssl
        self._buf = ""
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(300)
        raw_sock.connect((self.server, self.port))
        if self.use_ssl:
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = _ssl.CERT_NONE
            self._sock = ctx.wrap_socket(raw_sock, server_hostname=self.server)
        else:
            self._sock = raw_sock
        if self.nickserv_pass:
            self._raw(f"PASS {self.nickserv_pass}")
        self._raw(f"NICK {self.nick}")
        self._raw(f"USER {self.nick} 0 * :{self.realname}")

    def _disconnect(self):
        self._connected = False
        quit_msg = getattr(self, "_quit_msg", "BatCave Brawl")
        try:
            self._raw(f"QUIT :{quit_msg}")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    def _listen(self):
        while self._running:
            try:
                data = self._sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    break
                self._buf += data
                while "\r\n" in self._buf:
                    line, self._buf = self._buf.split("\r\n", 1)
                    self._handle(line.strip())
            except socket.timeout:
                self._raw("PING :keepalive")
            except Exception as e:
                log.debug("[%s] recv error: %s", self.nick, e)
                break

    def _handle(self, line: str):
        if not line:
            return

        # PING/PONG
        if line.startswith("PING"):
            self._raw(f"PONG {line[5:]}")
            return

        # 001 welcome → join channel
        if re.search(r" 001 ", line):
            self._connected = True
            print(f"[irc/{self.nick}] 001 welcome received — joining {self.channel}")
            time.sleep(1.0)
            # Identify with NickServ if configured
            if self.nickserv_pass and self.ns_account:
                self._raw(f"PRIVMSG NickServ :IDENTIFY {self.ns_account} {self.nickserv_pass}")
                time.sleep(1.5)
            self._raw(f"JOIN {self.channel}")
            if self.on_connect:
                threading.Thread(target=self.on_connect, daemon=True).start()
            return

        # 433 nick in use → append _
        if re.search(r" 433 ", line):
            self.nick = self.nick + "_"
            self._raw(f"NICK {self.nick}")
            return

        # PRIVMSG
        m = re.match(r":([^!]+)!(\S+) PRIVMSG (\S+) :(.*)", line)
        if m and self.on_message:
            sender  = m.group(1)
            target  = m.group(3)
            text    = m.group(4)
            # Strip CTCP ACTION wrapper for display but keep for handler
            try:
                self.on_message(sender, target, text)
            except Exception as e:
                log.error("[%s] on_message error: %s", self.nick, e)

    def _raw(self, msg: str):
        if self._sock is None:
            return
        try:
            with self._send_lock:
                self._sock.sendall(f"{msg}\r\n".encode("utf-8"))
        except Exception as e:
            log.debug("[%s] send error: %s", self.nick, e)

    @staticmethod
    def _split_msg(msg: str, max_len: int) -> list[str]:
        if len(msg) <= max_len:
            return [msg]
        chunks = []
        while msg:
            chunks.append(msg[:max_len])
            msg = msg[max_len:]
        return chunks

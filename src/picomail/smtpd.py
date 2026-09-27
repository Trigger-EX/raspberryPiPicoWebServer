# Receive-only SMTP server (RFC 5321) for the domains in config.DOMAINS.
#
# It only accepts mail addressed to local users, so it can never be abused as
# an open relay. Messages are spooled to flash and handed to the Store.
import asyncio
import gc
import os

from .util import Conn, LineTooLong, clean_text, fs_free, mail_date, new_id, opt, peer_ip


def parse_path(arg, prefix):
    """Parse 'FROM:<addr> PARAMS' -> (addr, [params]) or (None, None)."""
    if not arg.upper().startswith(prefix):
        return None, None
    rest = arg[len(prefix):].strip()
    if rest.startswith("<"):
        end = rest.find(">")
        if end < 0:
            return None, None
        addr, params = rest[1:end], rest[end + 1 :].split()
    else:
        parts = rest.split()
        if not parts:
            return None, None
        addr, params = parts[0], parts[1:]
    if addr.startswith("@") and ":" in addr:
        addr = addr.split(":", 1)[1]  # obsolete source route
    return addr, params


class SMTPServer:
    def __init__(self, cfg, store):
        self.store = store
        self.hostname = cfg.HOSTNAME
        self.domains = [d.lower() for d in cfg.DOMAINS]
        self.users = cfg.USERS
        aliases = opt(cfg, "ALIASES", {}) or {}
        self.aliases = {}
        for k in aliases:
            self.aliases[k.lower()] = aliases[k]
        self.catch_all = opt(cfg, "CATCH_ALL")
        self.max_size = opt(cfg, "MAX_MESSAGE_SIZE", 512 * 1024)
        self.max_rcpt = opt(cfg, "SMTP_MAX_RECIPIENTS", 20)
        self.max_clients = opt(cfg, "SMTP_MAX_CLIENTS", 3)
        self.timeout = opt(cfg, "SMTP_TIMEOUT", 120)
        self.active = 0

    def resolve(self, addr):
        """Map a recipient address to a local user name, or None."""
        addr = addr.strip().lower()
        if addr == "postmaster":
            local, domain = addr, self.domains[0]
        elif "@" in addr:
            local, _, domain = addr.rpartition("@")
        else:
            return None
        if domain not in self.domains or not local:
            return None
        local = local.split("+", 1)[0]  # user+tag@domain
        if local in self.users:
            return local
        if local in self.aliases:
            return self.aliases[local]
        return self.catch_all

    async def handle(self, reader, writer):
        gc.collect()  # limit heap fragmentation between sessions
        ip = peer_ip(writer)
        c = Conn(reader, writer, self.timeout, 2048)
        if self.active >= self.max_clients:
            try:
                await c.send("421 4.7.0 %s busy, try again later\r\n" % self.hostname)
            except Exception:
                pass
            await c.close()
            return
        self.active += 1
        try:
            await self.session(c, ip)
        except (OSError, asyncio.TimeoutError, LineTooLong):
            pass
        except Exception as e:
            print("smtp: error from %s: %r" % (ip, e))
        finally:
            self.active -= 1
            await c.close()

    async def session(self, c, ip):
        await c.send("220 %s ESMTP PicoMail\r\n" % self.hostname)
        helo = None
        sender = None
        rcpts = []
        errors = 0
        while True:
            line = await c.readline()
            if not line:
                return
            try:
                cmd = line.rstrip(b"\r\n").decode()
            except UnicodeError:
                cmd = ""
            parts = cmd.split(" ", 1)
            verb = parts[0].upper()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if verb in ("EHLO", "HELO"):
                if not arg:
                    reply = "501 5.5.4 Hostname required"
                else:
                    helo = clean_text(arg, 100) or "unknown"
                    sender, rcpts = None, []
                    if verb == "EHLO":
                        reply = "250-%s\r\n250-SIZE %d\r\n250 8BITMIME" % (self.hostname, self.max_size)
                    else:
                        reply = "250 %s" % self.hostname
            elif verb == "MAIL":
                addr, params = parse_path(arg, "FROM:")
                if not helo:
                    reply = "503 5.5.1 Send HELO/EHLO first"
                elif sender is not None:
                    reply = "503 5.5.1 Sender already given"
                elif addr is None:
                    reply = "501 5.5.4 Syntax: MAIL FROM:<address>"
                else:
                    reply = "250 2.1.0 OK"
                    for p in params:
                        if p.upper().startswith("SIZE="):
                            try:
                                if int(p[5:]) > self.max_size:
                                    reply = "552 5.3.4 Message too big"
                            except ValueError:
                                pass
                    if reply.startswith("250"):
                        sender, rcpts = addr, []
            elif verb == "RCPT":
                addr, _ = parse_path(arg, "TO:")
                if sender is None:
                    reply = "503 5.5.1 Need MAIL first"
                elif addr is None:
                    reply = "501 5.5.4 Syntax: RCPT TO:<address>"
                elif len(rcpts) >= self.max_rcpt:
                    reply = "452 4.5.3 Too many recipients"
                else:
                    user = self.resolve(addr)
                    if user is None:
                        reply = "550 5.1.1 No such user here"
                        errors += 1
                    else:
                        if user not in rcpts:
                            rcpts.append(user)
                        reply = "250 2.1.5 OK"
            elif verb == "DATA":
                if not rcpts:
                    reply = "503 5.5.1 No valid recipients"
                elif fs_free(self.store.spool) < self.max_size + 65536:
                    reply = "452 4.3.1 Insufficient storage, try again later"
                else:
                    reply = await self.data(c, ip, helo, sender, rcpts)
                    sender, rcpts = None, []
            elif verb == "RSET":
                sender, rcpts = None, []
                reply = "250 2.0.0 OK"
            elif verb == "NOOP":
                reply = "250 2.0.0 OK"
            elif verb == "VRFY":
                reply = "252 2.5.0 Cannot VRFY user"
            elif verb == "QUIT":
                await c.send("221 2.0.0 Bye\r\n")
                return
            else:
                reply = "502 5.5.1 Command not implemented"
                errors += 1
            await c.send(reply + "\r\n")
            if errors >= 10:
                await c.send("421 4.7.0 Too many errors\r\n")
                return

    async def data(self, c, ip, helo, sender, rcpts):
        await c.send("354 End data with <CR><LF>.<CR><LF>\r\n")
        mid = new_id()
        path = self.store.spool_path(mid)
        hdr = ("Return-Path: <%s>\r\nReceived: from %s ([%s])\r\n\tby %s (PicoMail) with ESMTP id %s;\r\n\t%s\r\n"
               % (clean_text(sender), helo, clean_text(ip, 45), self.hostname, mid, mail_date())).encode()
        size = len(hdr)
        too_big = False
        at_start = True
        prev_cr = False
        f = open(path, "wb")
        try:
            f.write(hdr)
            while True:
                line = await c.readline(partial=True)
                if not line:
                    raise OSError("connection lost during DATA")
                if at_start:
                    if line == b".\r\n" or line == b".\n":
                        break
                    if line.startswith(b"."):
                        line = line[1:]  # dot-unstuffing
                at_start = line.endswith(b"\n")
                if at_start and not line.endswith(b"\r\n") and not (line == b"\n" and prev_cr):
                    line = line[:-1] + b"\r\n"  # normalise bare LF
                prev_cr = line.endswith(b"\r")
                size += len(line)
                if size > self.max_size:
                    too_big = True  # keep reading to stay in sync, but discard
                elif not too_big:
                    f.write(line)
        except BaseException:
            f.close()
            os.remove(path)
            raise
        f.close()
        if too_big:
            os.remove(path)
            return "552 5.3.4 Message exceeds %d bytes" % self.max_size
        self.store.queue(mid, rcpts)
        print("smtp: %s from=<%s> to=%s size=%d id=%s" % (ip, sender, ",".join(rcpts), size, mid))
        return "250 2.0.0 OK queued as %s" % mid

# POP3 server (RFC 1939) so normal mail apps can fetch mail stored on the NAS.
# Messages are streamed from the NAS straight to the client.
import asyncio
import gc

from .util import Conn, LineTooLong, opt, peer_ip


class POP3Server:
    def __init__(self, cfg, store, auth, tls=False):
        self.store = store
        self.auth = auth
        self.tls = tls
        self.max_clients = opt(cfg, "POP3_MAX_CLIENTS", 2)
        self.max_msgs = opt(cfg, "POP3_MAX_MESSAGES", 500)
        self.timeout = opt(cfg, "POP3_TIMEOUT", 600)
        self.active = 0

    async def handle(self, reader, writer):
        gc.collect()  # limit heap fragmentation between sessions
        ip = peer_ip(writer)
        c = Conn(reader, writer, self.timeout, 512)
        if self.active >= self.max_clients:
            try:
                await c.send("-ERR [SYS/TEMP] Too busy, try again later\r\n")
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
            print("pop3: error from %s: %r" % (ip, e))
        finally:
            self.active -= 1
            await c.close()

    async def _cmd(self, c):
        line = await c.readline()
        if not line:
            return None, None
        try:
            parts = line.rstrip(b"\r\n").decode().split(" ", 1)
        except UnicodeError:
            return "", ""
        return parts[0].upper(), (parts[1] if len(parts) > 1 else "")

    async def session(self, c, ip):
        await c.send("+OK PicoMail POP3 ready\r\n")
        user = None
        fails = 0
        # AUTHORIZATION state
        while True:
            verb, arg = await self._cmd(c)
            if verb is None:
                return
            if verb == "CAPA":
                await c.send("+OK\r\nUSER\r\nUIDL\r\nTOP\r\n.\r\n")
            elif verb == "USER":
                if not self.auth.plaintext_allowed(ip, self.tls):
                    await c.send("-ERR [SYS/PERM] Use the TLS port (POP3S) from outside your network\r\n")
                else:
                    user = arg.strip()
                    await c.send("+OK\r\n")
            elif verb == "PASS":
                if user is None:
                    await c.send("-ERR USER first\r\n")
                elif self.auth.check(ip, user, arg):
                    break
                else:
                    fails += 1
                    await asyncio.sleep(1)
                    await c.send("-ERR [AUTH] Invalid login\r\n")
                    if fails >= 3:
                        return
            elif verb == "QUIT":
                await c.send("+OK Bye\r\n")
                return
            else:
                await c.send("-ERR Not allowed before login\r\n")

        try:
            msgs = await self.store.list(user)
        except Exception as e:
            print("pop3: list failed:", repr(e))
            await c.send("-ERR [SYS/TEMP] Mail storage unavailable\r\n")
            return
        msgs = msgs[: self.max_msgs]
        deleted = set()
        await c.send("+OK %d messages\r\n" % len(msgs))

        def pick(arg):
            try:
                n = int(arg.split()[0])
            except (ValueError, IndexError):
                return None
            if 1 <= n <= len(msgs) and (n - 1) not in deleted:
                return n - 1
            return None

        # TRANSACTION state
        while True:
            verb, arg = await self._cmd(c)
            if verb is None:
                return  # connection dropped: no deletions (RFC 1939)
            if verb == "STAT":
                live = [m for i, m in enumerate(msgs) if i not in deleted]
                await c.send("+OK %d %d\r\n" % (len(live), sum(s for _, s in live)))
            elif verb in ("LIST", "UIDL"):
                col = 1 if verb == "LIST" else 0
                if arg:
                    i = pick(arg)
                    if i is None:
                        await c.send("-ERR No such message\r\n")
                    else:
                        await c.send("+OK %d %s\r\n" % (i + 1, msgs[i][col]))
                else:
                    out = ["+OK\r\n"]
                    for i, m in enumerate(msgs):
                        if i not in deleted:
                            out.append("%d %s\r\n" % (i + 1, m[col]))
                    out.append(".\r\n")
                    await c.send("".join(out))
            elif verb in ("RETR", "TOP"):
                i = pick(arg)
                lines = None
                if verb == "TOP":
                    try:
                        lines = int(arg.split()[1])
                    except (ValueError, IndexError):
                        i = None
                if i is None:
                    await c.send("-ERR No such message\r\n")
                else:
                    await self.send_msg(c, user, msgs[i], lines)
            elif verb == "DELE":
                i = pick(arg)
                if i is None:
                    await c.send("-ERR No such message\r\n")
                else:
                    deleted.add(i)
                    await c.send("+OK Marked for deletion\r\n")
            elif verb == "RSET":
                deleted.clear()
                await c.send("+OK\r\n")
            elif verb == "NOOP":
                await c.send("+OK\r\n")
            elif verb == "CAPA":
                await c.send("+OK\r\nUSER\r\nUIDL\r\nTOP\r\n.\r\n")
            elif verb == "QUIT":
                failed = 0
                for i in sorted(deleted):
                    try:
                        if not await self.store.delete(user, msgs[i][0]):
                            failed += 1
                    except Exception:
                        failed += 1
                if failed:
                    await c.send("-ERR %d messages not deleted\r\n" % failed)
                else:
                    await c.send("+OK Bye\r\n")
                return
            else:
                await c.send("-ERR Unknown command\r\n")

    async def send_msg(self, c, user, msg, top=None):
        try:
            resp = await self.store.open(user, msg[0])
        except Exception as e:
            print("pop3: fetch failed:", repr(e))
            resp = None
        if resp is None:
            await c.send("-ERR Message unavailable\r\n")
            return
        try:
            await c.send("+OK %d octets\r\n" % msg[1])
            if top is None:
                at_start = True
                while True:
                    d = await resp.read(1024)
                    if not d:
                        break
                    if at_start and d[:1] == b".":
                        d = b"." + d
                    d = d.replace(b"\n.", b"\n..")  # byte-stuffing
                    at_start = d[-1:] == b"\n"
                    await c.send(d)
                await c.send(b".\r\n" if at_start else b"\r\n.\r\n")
            else:
                await self._send_top(c, resp, top)
        finally:
            await resp.close()

    async def _send_top(self, c, resp, n):
        buf = b""
        in_body = False
        sent = 0
        out = []
        done = False
        eof = False
        while not done and not eof:
            d = await resp.read(1024)
            if d:
                buf += d
            else:
                eof = True
            while not done:
                i = buf.find(b"\n")
                if i >= 0:
                    line, buf = buf[: i + 1], buf[i + 1 :]
                elif eof and buf:
                    line, buf = buf + b"\r\n", b""
                elif len(buf) > 1024:
                    line, buf = buf + b"\r\n", b""  # truncate absurd lines
                else:
                    break
                if in_body:
                    if sent >= n:
                        done = True
                        break
                    sent += 1
                elif line in (b"\r\n", b"\n"):
                    in_body = True
                if line.startswith(b"."):
                    line = b"." + line
                out.append(line)
            if out:
                await c.send(b"".join(out))
                out = []
        await c.send(b".\r\n")

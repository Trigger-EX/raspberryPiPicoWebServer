# Tiny web inbox: list, read, download and delete messages stored on the NAS.
# Uses HTTP Basic auth against config.USERS. Pages are streamed, never built
# in RAM in full.
import asyncio
import binascii
import gc
import os

from .util import Conn, LineTooLong, ct_eq, html_escape, opt, peer_ip, safe_id

PAGE_SIZE = 15

_CSS = (b"body{font:15px system-ui,sans-serif;margin:0 auto;max-width:900px;padding:0 16px;"
        b"background:#fafafa;color:#222}a{color:#0b5cad}table{width:100%;border-collapse:collapse}"
        b"td{padding:8px 6px;border-bottom:1px solid #ddd;vertical-align:top;overflow-wrap:anywhere}"
        b"pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;border:1px solid #ddd;"
        b"padding:12px;font-size:13px}.m{color:#666;font-size:13px}button{font:inherit}"
        b"@media(prefers-color-scheme:dark){body{background:#161616;color:#ddd}a{color:#7ab7ff}"
        b"pre{background:#1f1f1f;border-color:#333}td{border-color:#333}.m{color:#999}}")


def parse_headers(data):
    """Parse the header block of a (possibly truncated) message."""
    end = data.find(b"\r\n\r\n")
    if end >= 0:
        data = data[:end]
    hdrs = {}
    last = None
    for line in data.split(b"\r\n"):
        if line[:1] in (b" ", b"\t") and last:
            hdrs[last] += b" " + line.strip()
            continue
        k, sep, v = line.partition(b":")
        if not sep:
            continue
        last = k.strip().lower()
        if last not in hdrs:
            hdrs[last] = v.strip()
        else:
            last = None  # only keep the first occurrence
    return hdrs


def _qdecode(t):
    t = t.replace(b"_", b" ")
    out = []
    i = 0
    while i < len(t):
        if t[i:i + 1] == b"=" and i + 3 <= len(t):
            try:
                out.append(binascii.unhexlify(t[i + 1 : i + 3]))
                i += 3
                continue
            except (ValueError, TypeError):
                pass
        out.append(t[i:i + 1])
        i += 1
    return b"".join(out)


def _latin1_to_utf8(b):
    return "".join([chr(x) for x in b]).encode()


def decode_words(s):
    """Decode RFC 2047 encoded-words (=?utf-8?B?...?=) into UTF-8 bytes."""
    out = []
    i = 0
    prev_encoded = False
    while True:
        j = s.find(b"=?", i)
        k = s.find(b"?", j + 2) if j >= 0 else -1
        e = s.find(b"?=", k + 3) if k >= 0 else -1
        if j < 0 or k < 0 or e < 0 or s[k + 2 : k + 3] != b"?":
            out.append(s[i:])
            break
        between = s[i:j]
        if not (prev_encoded and not between.strip()):
            out.append(between)
        charset = s[j + 2 : k].lower().split(b"*")[0]
        enc = s[k + 1 : k + 2].upper()
        text = s[k + 3 : e]
        try:
            if enc == b"B":
                dec = binascii.a2b_base64(text + b"=" * (-len(text) % 4))
            elif enc == b"Q":
                dec = _qdecode(text)
            else:
                raise ValueError
            if charset in (b"iso-8859-1", b"latin1", b"windows-1252", b"iso-8859-15"):
                dec = _latin1_to_utf8(dec)
            elif charset not in (b"utf-8", b"utf8", b"us-ascii"):
                raise ValueError
            out.append(dec)
            prev_encoded = True
        except (ValueError, TypeError):
            out.append(s[j : e + 2])
            prev_encoded = False
        i = e + 2
    return b"".join(out)


class WebServer:
    def __init__(self, cfg, store, auth, tls=False):
        self.store = store
        self.auth = auth
        self.tls = tls
        self.hostname = cfg.HOSTNAME
        self.max_clients = opt(cfg, "HTTP_MAX_CLIENTS", 2)
        self.token = binascii.hexlify(os.urandom(12)).decode()
        self.active = 0

    async def handle(self, reader, writer):
        gc.collect()  # limit heap fragmentation between sessions
        ip = peer_ip(writer)
        c = Conn(reader, writer, 30, 2048)
        if self.active >= self.max_clients:
            try:
                await self.simple(c, "503 Service Unavailable", b"Busy, try again shortly.")
            except Exception:
                pass
            await c.close()
            return
        self.active += 1
        try:
            await self.request(c, ip)
        except (OSError, asyncio.TimeoutError, LineTooLong):
            pass
        except Exception as e:
            print("web: error from %s: %r" % (ip, e))
        finally:
            self.active -= 1
            await c.close()

    async def head(self, c, status, ctype="text/html; charset=utf-8", extra=""):
        await c.send(
            "HTTP/1.0 %s\r\nContent-Type: %s\r\nCache-Control: no-store\r\n"
            "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'\r\n"
            "X-Frame-Options: DENY\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
            "Connection: close\r\n%s\r\n" % (status, ctype, extra))

    async def simple(self, c, status, msg, extra=""):
        await self.head(c, status, extra=extra)
        await c.send(b"<!doctype html><meta name=viewport content='width=device-width'><title>PicoMail</title><style>"
                     + _CSS + b"</style><p>" + html_escape(msg) + b"</p>")

    async def page_start(self, c, title):
        await self.head(c, "200 OK")
        await c.send(b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>"
                     + html_escape(title) + b"</title><style>" + _CSS + b"</style>")

    async def request(self, c, ip):
        line = await c.readline()
        parts = line.split()
        if len(parts) < 2:
            return
        method = parts[0].decode()
        target = parts[1].decode()
        headers = {}
        for _ in range(40):
            h = await c.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.partition(b":")
            headers[k.strip().lower()] = v.strip()
        else:
            return await self.simple(c, "431 Request Header Fields Too Large", b"Too many headers.")

        if not self.auth.plaintext_allowed(ip, self.tls):
            return await self.simple(c, "403 Forbidden", b"Use the HTTPS address when outside your network.")
        user = None
        a = headers.get(b"authorization", b"")
        if a[:6].lower() == b"basic ":
            try:
                u, _, p = binascii.a2b_base64(a[6:]).decode().partition(":")
                user = self.auth.check(ip, u, p)
            except (ValueError, UnicodeError):
                pass
        if not user:
            if self.auth.locked(ip):
                return await self.simple(c, "429 Too Many Requests", b"Too many failed logins. Try later.")
            return await self.simple(c, "401 Unauthorized", b"Login required.",
                                     'WWW-Authenticate: Basic realm="PicoMail", charset="UTF-8"\r\n')

        path, _, query = target.partition("?")
        if method == "GET" and path == "/":
            page = 1
            for kv in query.split("&"):
                if kv.startswith("p="):
                    try:
                        page = max(1, int(kv[2:]))
                    except ValueError:
                        pass
            return await self.inbox(c, user, page)
        if method == "GET" and path.startswith("/m/") and safe_id(path[3:]):
            return await self.view(c, user, path[3:])
        if method == "GET" and path.startswith("/raw/") and safe_id(path[5:]):
            return await self.raw(c, user, path[5:])
        if method == "POST" and path == "/delete":
            return await self.delete(c, user, headers)
        return await self.simple(c, "404 Not Found", b"Not found.")

    async def _summary(self, user, mid):
        try:
            resp = await self.store.open(user, mid, 2048)
        except Exception:
            resp = None
        if resp is None:
            return {}
        try:
            data = b""
            while len(data) < 2048:
                d = await resp.read(2048 - len(data))
                if not d:
                    break
                data += d
        except Exception:
            pass
        finally:
            await resp.close()
        return parse_headers(data)

    async def inbox(self, c, user, page):
        try:
            msgs = await self.store.list(user)
        except Exception as e:
            print("web: list failed:", repr(e))
            return await self.simple(c, "503 Service Unavailable", b"Mail storage (NAS) is unreachable.")
        msgs.reverse()  # newest first
        pages = max(1, (len(msgs) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        await self.page_start(c, "Inbox - " + user)
        pend = self.store.pending()
        await c.send(("<h1>Inbox</h1><p class=m>%s@%s &middot; %d messages%s</p><table>" % (
            user, self.hostname, len(msgs),
            (" &middot; %d waiting to upload" % pend) if pend else "")).encode())
        for mid, size in msgs[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]:
            h = await self._summary(user, mid)
            subj = decode_words(h.get(b"subject", b"")) or b"(no subject)"
            frm = decode_words(h.get(b"from", b""))
            await c.send(b"<tr><td><a href='/m/" + mid.encode() + b"'>" + html_escape(subj[:200])
                         + b"</a><div class=m>" + html_escape(frm[:120]) + b"</div></td><td class=m>"
                         + html_escape(h.get(b"date", b"")[:40]) + b"<br>"
                         + ("%d KB" % ((size + 1023) // 1024)).encode() + b"</td></tr>")
        await c.send(b"</table><p>")
        if page > 1:
            await c.send(("<a href='/?p=%d'>&larr; Newer</a> " % (page - 1)).encode())
        if page < pages:
            await c.send(("<a href='/?p=%d'>Older &rarr;</a>" % (page + 1)).encode())
        await c.send(b"</p>")

    async def view(self, c, user, mid):
        h = await self._summary(user, mid)
        try:
            resp = await self.store.open(user, mid)
        except Exception:
            resp = None
        if resp is None:
            return await self.simple(c, "404 Not Found", b"Message not found.")
        try:
            await self.page_start(c, "Message")
            await c.send(b"<p><a href='/'>&larr; Inbox</a> &middot; <a href='/raw/" + mid.encode()
                         + b"'>Download .eml</a></p><h2>"
                         + html_escape(decode_words(h.get(b"subject", b"")) or b"(no subject)") + b"</h2>")
            for k, label in ((b"from", b"From"), (b"to", b"To"), (b"cc", b"Cc"), (b"date", b"Date")):
                if k in h:
                    await c.send(b"<div class=m><b>" + label + b":</b> "
                                 + html_escape(decode_words(h[k])[:300]) + b"</div>")
            await c.send(b"<form method=post action='/delete'><input type=hidden name=id value='"
                         + mid.encode() + b"'><input type=hidden name=t value='" + self.token.encode()
                         + b"'><p><button>Delete</button></p></form><pre>")
            while True:
                d = await resp.read(1024)
                if not d:
                    break
                await c.send(html_escape(d))
            await c.send(b"</pre>")
        finally:
            await resp.close()

    async def raw(self, c, user, mid):
        try:
            resp = await self.store.open(user, mid)
        except Exception:
            resp = None
        if resp is None:
            return await self.simple(c, "404 Not Found", b"Message not found.")
        try:
            await self.head(c, "200 OK", "message/rfc822",
                            'Content-Disposition: attachment; filename="%s.eml"\r\n' % mid)
            while True:
                d = await resp.read(1024)
                if not d:
                    break
                await c.send(d)
        finally:
            await resp.close()

    async def delete(self, c, user, headers):
        try:
            n = int(headers.get(b"content-length", b"0"))
        except ValueError:
            n = 0
        body = b""
        while 0 < n <= 1024 and len(body) < n:
            d = await c.read(n - len(body))
            if not d:
                break
            body += d
        form = {}
        for kv in body.decode().split("&"):
            k, _, v = kv.partition("=")
            form[k] = v
        mid = form.get("id", "")
        if not ct_eq(form.get("t", ""), self.token) or not safe_id(mid):
            return await self.simple(c, "403 Forbidden", b"Invalid request.")
        try:
            ok = await self.store.delete(user, mid)
        except Exception:
            ok = False
        if not ok:
            return await self.simple(c, "503 Service Unavailable", b"Could not delete (NAS unreachable?).")
        await c.send("HTTP/1.0 303 See Other\r\nLocation: /\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

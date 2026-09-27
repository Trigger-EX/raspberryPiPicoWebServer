# Minimal streaming WebDAV client used to keep mailboxes on a NAS.
# Supports PUT (from a local file), GET (optionally ranged), DELETE, MKCOL
# and PROPFIND Depth: 1. Bodies are streamed in small chunks so large
# messages never have to fit in RAM.
import asyncio

from .util import Conn, b64enc


class DAVError(Exception):
    pass


def _client_ctx(verify):
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if not verify:
        if hasattr(ctx, "check_hostname"):
            ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Response:
    def __init__(self, conn, status, headers, no_body=False):
        self.conn = conn
        self.status = status
        self.headers = headers
        self.chunked = "chunked" in headers.get("transfer-encoding", "").lower()
        cl = headers.get("content-length")
        self.remaining = int(cl) if cl is not None and not self.chunked else -1
        self.chunk_left = 0
        self.done = no_body or self.remaining == 0 or status in (204, 304)

    async def read(self, n=1024):
        if self.done:
            return b""
        c = self.conn
        if self.chunked:
            if self.chunk_left == 0:
                line = await c.readline()
                try:
                    size = int(line.split(b";")[0].strip().decode(), 16)
                except ValueError:
                    raise DAVError("bad chunk header")
                if size == 0:
                    while True:
                        t = await c.readline()
                        if t in (b"\r\n", b"\n", b""):
                            break
                    self.done = True
                    return b""
                self.chunk_left = size
            data = await c.read(min(n, self.chunk_left))
            if not data:
                raise DAVError("connection closed mid-chunk")
            self.chunk_left -= len(data)
            if self.chunk_left == 0:
                await c.readline()
            return data
        if self.remaining >= 0:
            data = await c.read(min(n, self.remaining))
            if not data:
                raise DAVError("connection closed early")
            self.remaining -= len(data)
            if self.remaining == 0:
                self.done = True
            return data
        data = await c.read(n)
        if not data:
            self.done = True
        return data

    async def read_all(self, limit=16384):
        parts = []
        total = 0
        while True:
            d = await self.read(1024)
            if not d:
                return b"".join(parts)
            total += len(d)
            if total > limit:
                raise DAVError("response too large")
            parts.append(d)

    async def close(self):
        await self.conn.close()


_PROPFIND = (b'<?xml version="1.0" encoding="utf-8"?>'
             b'<D:propfind xmlns:D="DAV:"><D:prop>'
             b"<D:getcontentlength/><D:resourcetype/>"
             b"</D:prop></D:propfind>")


def _tag_is(name, want):
    return name == want or name.endswith(b":" + want)


def _next_response_end(low):
    """Index just past the next </...response> closing tag, or -1."""
    i = 0
    while True:
        i = low.find(b"</", i)
        if i < 0:
            return -1
        j = low.find(b">", i)
        if j < 0:
            return -1
        if _tag_is(low[i + 2 : j].strip(), b"response"):
            return j + 1
        i = j


def _tag_text(block, low, want):
    i = 0
    while True:
        i = low.find(b"<", i)
        if i < 0:
            return None
        j = low.find(b">", i)
        if j < 0:
            return None
        inner = low[i + 1 : j]
        if inner and not inner.startswith(b"/") and not inner.endswith(b"/"):
            name = inner.split()[0]
            if _tag_is(name, want):
                k = low.find(b"<", j)
                return block[j + 1 : k].strip() if k > 0 else None
        i = j


def parse_propfind_block(block):
    """Return (name, size) for a file entry, or None for collections."""
    low = block.lower()
    if b"collection/>" in low or b"collection></" in low:
        return None
    href = _tag_text(block, low, b"href")
    if not href:
        return None
    name = href.rstrip(b"/").split(b"/")[-1].decode()
    size = _tag_text(block, low, b"getcontentlength")
    try:
        size = int(size) if size else 0
    except ValueError:
        size = 0
    return name, size


class WebDAV:
    def __init__(self, url, user=None, password=None, verify_tls=True, timeout=20):
        scheme, rest = url.split("://", 1)
        self.tls = scheme.lower() == "https"
        hostport, _, path = rest.partition("/")
        path = path.strip("/")
        self.base = "/" + path if path else ""
        if hostport.startswith("["):
            host, _, port = hostport[1:].partition("]")
            port = port.lstrip(":")
        elif ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            host, port = hostport, ""
        self.host = host
        self.port = int(port) if port else (443 if self.tls else 80)
        self.hosthdr = hostport
        self.auth = ("Basic " + b64enc("%s:%s" % (user, password or ""))) if user else None
        self.verify = verify_tls
        self.timeout = timeout

    async def request(self, method, path, headers=None, body=None, body_file=None, body_len=0):
        ctx = _client_ctx(self.verify) if self.tls else None
        if ctx:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=ctx), self.timeout)
        else:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        conn = Conn(r, w, self.timeout, 8192)
        try:
            h = "%s %s%s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nUser-Agent: PicoMail\r\n" % (
                method, self.base, path, self.hosthdr)
            if self.auth:
                h += "Authorization: %s\r\n" % self.auth
            if headers:
                for k in headers:
                    h += "%s: %s\r\n" % (k, headers[k])
            if body is not None:
                body_len = len(body)
            if body is not None or body_file:
                h += "Content-Length: %d\r\n" % body_len
            await conn.send(h + "\r\n")
            if body:
                await conn.send(body)
            elif body_file:
                with open(body_file, "rb") as f:
                    while True:
                        d = f.read(1024)
                        if not d:
                            break
                        await conn.send(d)
            line = await conn.readline()
            parts = line.split(None, 2)
            if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
                raise DAVError("bad status line from NAS")
            status = int(parts[1])
            hdrs = {}
            while True:
                t = await conn.readline()
                if t in (b"\r\n", b"\n"):
                    break
                if not t:
                    raise DAVError("NAS closed connection in headers")
                k, _, v = t.decode().partition(":")
                hdrs[k.strip().lower()] = v.strip()
            return Response(conn, status, hdrs, method == "HEAD")
        except BaseException:
            await conn.close()
            raise

    async def _simple(self, method, path, **kw):
        resp = await self.request(method, path, **kw)
        try:
            try:
                await resp.read_all(65536)
            except (DAVError, OSError):
                pass
            return resp.status
        finally:
            await resp.close()

    async def mkcol(self, path):
        # 405 means the collection already exists.
        return await self._simple("MKCOL", path) in (200, 201, 405)

    async def put_file(self, path, local, size):
        st = await self._simple("PUT", path, body_file=local, body_len=size)
        return st in (200, 201, 204), st

    async def delete(self, path):
        return await self._simple("DELETE", path) in (200, 204, 404)

    async def get(self, path, first=None):
        hdrs = {"Range": "bytes=0-%d" % (first - 1)} if first else None
        return await self.request("GET", path, headers=hdrs)

    async def propfind(self, path, limit=2000):
        """List files in a collection as sorted [(name, size), ...]."""
        resp = await self.request("PROPFIND", path, body=_PROPFIND, headers={
            "Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        try:
            if resp.status == 404:
                return []
            if resp.status != 207:
                raise DAVError("PROPFIND %s -> %d" % (path, resp.status))
            items = []
            buf = b""
            while True:
                d = await resp.read(1024)
                if d:
                    buf += d
                while True:
                    low = buf.lower()
                    end = _next_response_end(low)
                    if end < 0:
                        break
                    item = parse_propfind_block(buf[:end])
                    buf = buf[end:]
                    if item and len(items) < limit:
                        items.append(item)
                if not d:
                    break
                if len(buf) > 16384:
                    raise DAVError("PROPFIND entry too large")
            items.sort()
            return items
        finally:
            await resp.close()

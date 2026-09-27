# Small helpers shared by the PicoMail servers.
# Everything here runs on both MicroPython (Pico W) and CPython (host tests).
import asyncio
import binascii
import os
import time


class LineTooLong(Exception):
    pass


class Conn:
    """Buffered stream wrapper with per-read timeouts and a line-length cap.

    The cap matters on a 264 KB device: a client that never sends a newline
    must not be able to make us buffer unbounded data.
    """

    def __init__(self, reader, writer, timeout=60, maxline=4096):
        self.r = reader
        self.w = writer
        self.timeout = timeout
        self.maxline = maxline
        self.buf = b""

    async def _fill(self, n=1024):
        return await asyncio.wait_for(self.r.read(n), self.timeout)

    async def readline(self, partial=False):
        """Return one line including its newline, or b"" at EOF.

        With partial=True an over-long line is returned in pieces (without
        a trailing newline) instead of raising LineTooLong.
        """
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = self.buf[: i + 1]
                self.buf = self.buf[i + 1 :]
                return line
            if len(self.buf) >= self.maxline:
                if not partial:
                    raise LineTooLong()
                line = self.buf
                self.buf = b""
                return line
            # Never let the buffer outgrow maxline: keeps allocations small
            # and predictable on a fragmented heap.
            data = await self._fill(min(1024, self.maxline - len(self.buf)))
            if not data:
                line = self.buf
                self.buf = b""
                return line
            self.buf += data

    async def read(self, n=1024):
        if self.buf:
            data = self.buf[:n]
            self.buf = self.buf[n:]
            return data
        return await self._fill(n)

    async def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.w.write(data)
        await asyncio.wait_for(self.w.drain(), self.timeout)

    async def close(self):
        try:
            self.w.close()
            await self.w.wait_closed()
        except Exception:
            pass


def opt(cfg, name, default=None):
    return getattr(cfg, name, default)


def peer_ip(writer):
    try:
        p = writer.get_extra_info("peername")
        if isinstance(p, (tuple, list)):
            ip = p[0]
        elif isinstance(p, (bytes, bytearray)) and len(p) >= 8 and p[0] == 2:
            # Raw sockaddr_in (MicroPython unix port): family, port, addr.
            ip = "%d.%d.%d.%d" % (p[4], p[5], p[6], p[7])
        else:
            ip = str(p)
    except Exception:
        return "?"
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def is_private(ip):
    """True for loopback, RFC 1918, link-local and IPv6 ULA/link-local."""
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if ip in ("::1", "localhost"):
        return True
    for prefix in ("127.", "10.", "192.168.", "169.254."):
        if ip.startswith(prefix):
            return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    low = ip.lower()
    return low.startswith("fe80:") or low[:2] in ("fc", "fd")


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def mail_date(t=None):
    g = time.gmtime(int(time.time() if t is None else t))
    return "%s, %02d %s %04d %02d:%02d:%02d +0000" % (
        _DAYS[g[6]], g[2], _MONTHS[g[1] - 1], g[0], g[3], g[4], g[5])


_counter = [0]


def new_id():
    # Fixed-width timestamp first so ids sort chronologically as strings.
    _counter[0] = (_counter[0] + 1) % 10000
    rnd = binascii.hexlify(os.urandom(3)).decode()
    return "%010d-%04d-%s" % (int(time.time()), _counter[0], rnd)


def safe_id(s):
    if not s or len(s) > 64:
        return False
    for ch in s:
        if ch not in "0123456789abcdef-":
            return False
    return True


def ct_eq(a, b):
    """Constant-time comparison for passwords and tokens."""
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    res = 0 if len(a) == len(b) else 1
    for x, y in zip(a, b if len(a) == len(b) else a):
        res |= x ^ y
    return res == 0


def html_escape(b):
    if isinstance(b, str):
        b = b.encode()
    return (b.replace(b"&", b"&amp;").replace(b"<", b"&lt;")
            .replace(b">", b"&gt;").replace(b'"', b"&quot;"))


def b64enc(s):
    if isinstance(s, str):
        s = s.encode()
    return binascii.b2a_base64(s).strip().decode()


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def fs_free(path):
    try:
        s = os.statvfs(path)
        return s[1] * s[4]
    except (OSError, AttributeError):
        return 0


def clean_text(s, maxlen=200):
    """Strip anything that could break out of a header line."""
    out = []
    for ch in s[:maxlen]:
        o = ord(ch)
        if 32 < o < 127 and ch not in "<>()\\\"":
            out.append(ch)
    return "".join(out)

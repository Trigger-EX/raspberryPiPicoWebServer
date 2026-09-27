# Mail storage: a small spool on the Pico's flash plus mailboxes on a NAS.
#
# Incoming messages are written to the local spool first so the SMTP "250 OK"
# is only sent once the message is safe. A background task then uploads each
# spooled message to <NAS_URL>/<user>/<id>.eml and removes it locally. If the
# NAS is unreachable, messages simply wait in the spool and are retried.
import asyncio
import os

from .util import exists, opt
from .webdav import WebDAV


class Store:
    def __init__(self, cfg):
        self.dav = WebDAV(cfg.NAS_URL, opt(cfg, "NAS_USER"), opt(cfg, "NAS_PASSWORD"),
                          opt(cfg, "NAS_VERIFY_TLS", True), opt(cfg, "NAS_TIMEOUT", 20))
        self.spool = opt(cfg, "SPOOL_DIR", "/spool").rstrip("/")
        self.users = list(cfg.USERS)
        self.retry = opt(cfg, "UPLOAD_RETRY_SECS", 30)
        self.kick = asyncio.Event()
        self.nas_ok = False
        if not exists(self.spool):
            os.mkdir(self.spool)
        self._cleanup()

    def _cleanup(self):
        # Drop half-written messages left behind by a crash or power loss.
        names = os.listdir(self.spool)
        for fn in names:
            mid = fn.split(".")[0]
            orphan = fn.endswith(".eml") and (mid + ".rcpt") not in names
            if fn.endswith(".tmp") or orphan:
                try:
                    os.remove("%s/%s" % (self.spool, fn))
                except OSError:
                    pass

    async def setup(self):
        try:
            for u in self.users:
                await self.dav.mkcol("/%s/" % u)
            self.nas_ok = True
        except Exception as e:
            print("store: NAS not reachable yet:", repr(e))

    def spool_path(self, mid):
        return "%s/%s.eml" % (self.spool, mid)

    def _write_rcpt(self, mid, users):
        tmp = "%s/%s.rcpt.tmp" % (self.spool, mid)
        with open(tmp, "w") as f:
            f.write("\n".join(users) + "\n")
        os.rename(tmp, "%s/%s.rcpt" % (self.spool, mid))

    def queue(self, mid, users):
        self._write_rcpt(mid, users)
        self.kick.set()

    def pending(self):
        return len([f for f in os.listdir(self.spool) if f.endswith(".rcpt")])

    async def run_uploader(self):
        while True:
            self.kick.clear()
            try:
                await self.flush()
            except Exception as e:
                print("store: upload error:", repr(e))
            try:
                await asyncio.wait_for(self.kick.wait(), self.retry)
            except asyncio.TimeoutError:
                pass

    async def _put(self, user, mid, path, size):
        target = "/%s/%s.eml" % (user, mid)
        ok, st = await self.dav.put_file(target, path, size)
        if not ok and st in (404, 409):
            # Mailbox folder missing (e.g. user added later): create and retry.
            await self.dav.mkcol("/%s/" % user)
            ok, st = await self.dav.put_file(target, path, size)
        if not ok:
            print("store: PUT %s -> %d" % (target, st))
        return ok

    async def flush(self):
        for fn in sorted(os.listdir(self.spool)):
            if not fn.endswith(".rcpt"):
                continue
            mid = fn[:-5]
            rpath = "%s/%s" % (self.spool, fn)
            with open(rpath) as f:
                users = f.read().split()
            eml = self.spool_path(mid)
            size = os.stat(eml)[6]
            left = []
            for u in users:
                try:
                    ok = await self._put(u, mid, eml, size)
                except Exception as e:
                    print("store: upload failed:", repr(e))
                    ok = False
                if not ok:
                    left.append(u)
            if left:
                self._write_rcpt(mid, left)
                self.nas_ok = False
                return  # NAS trouble; try again later.
            os.remove(rpath)
            os.remove(eml)
            self.nas_ok = True
            print("store: delivered %s to %s" % (mid, ",".join(users)))

    async def list(self, user):
        items = await self.dav.propfind("/%s/" % user)
        return [(n[:-4], s) for n, s in items if n.endswith(".eml")]

    async def open(self, user, mid, first=None):
        """Return a streaming Response for a message, or None if missing."""
        resp = await self.dav.get("/%s/%s.eml" % (user, mid), first)
        if resp.status in (200, 206):
            return resp
        await resp.close()
        return None

    async def delete(self, user, mid):
        return await self.dav.delete("/%s/%s.eml" % (user, mid))

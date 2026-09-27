# Wires the servers together. Hardware-independent so it can be tested on a PC.
import asyncio

from .auth import Auth
from .pop3d import POP3Server
from .smtpd import SMTPServer
from .store import Store
from .util import opt
from .webui import WebServer


def server_tls_context(cfg):
    cert = opt(cfg, "TLS_CERT")
    key = opt(cfg, "TLS_KEY")
    if not (cert and key):
        return None
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


async def start(cfg):
    """Start all listeners and the NAS uploader. Returns (store, servers)."""
    for u in cfg.USERS:
        for ch in u:
            if ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-":
                raise ValueError("user names may only use a-z 0-9 . _ - : %r" % u)
    store = Store(cfg)
    await store.setup()
    auth = Auth(cfg)
    bind = opt(cfg, "BIND", "0.0.0.0")
    servers = []

    async def listen(name, handler, port, ctx=None):
        if not port:
            return
        if ctx:
            srv = await asyncio.start_server(handler, bind, port, backlog=4, ssl=ctx)
        else:
            srv = await asyncio.start_server(handler, bind, port, backlog=4)
        servers.append(srv)
        print("picomail: %s listening on port %d" % (name, port))

    await listen("SMTP", SMTPServer(cfg, store).handle, opt(cfg, "SMTP_PORT", 25))
    await listen("POP3", POP3Server(cfg, store, auth).handle, opt(cfg, "POP3_PORT", 110))
    await listen("HTTP", WebServer(cfg, store, auth).handle, opt(cfg, "HTTP_PORT", 80))
    ctx = server_tls_context(cfg)
    if ctx:
        await listen("POP3S", POP3Server(cfg, store, auth, True).handle, opt(cfg, "POP3S_PORT", 995), ctx)
        await listen("HTTPS", WebServer(cfg, store, auth, True).handle, opt(cfg, "HTTPS_PORT", 443), ctx)
    else:
        print("picomail: no TLS_CERT/TLS_KEY set - POP3S/HTTPS disabled, logins only work from the LAN")
    asyncio.create_task(store.run_uploader())
    return store, servers


def servers_alive(servers):
    """False if a MicroPython listener task has died (e.g. MemoryError)."""
    for srv in servers:
        task = getattr(srv, "task", None)
        if task is not None and task.done():
            return False
    return True

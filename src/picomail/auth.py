# Login checking shared by POP3 and the web inbox, with per-IP lockout so a
# WAN-exposed Pico can't be brute-forced.
import time

from .util import ct_eq, is_private, opt


class Auth:
    def __init__(self, cfg):
        self.users = cfg.USERS
        self.max_fail = opt(cfg, "AUTH_MAX_FAILURES", 5)
        self.lock_secs = opt(cfg, "AUTH_LOCKOUT_SECS", 900)
        self.plain_wan = opt(cfg, "ALLOW_PLAINTEXT_AUTH_FROM_WAN", False)
        self.fails = {}  # ip -> [count, locked_until]

    def plaintext_allowed(self, ip, tls):
        """Passwords may only cross the internet inside TLS."""
        return tls or self.plain_wan or is_private(ip)

    def locked(self, ip):
        e = self.fails.get(ip)
        if not e:
            return False
        if e[1] and time.time() >= e[1]:
            del self.fails[ip]
            return False
        return bool(e[1])

    def check(self, ip, user, password):
        """Return the user name on success, else None."""
        if self.locked(ip):
            return None
        want = self.users.get(user) if user else None
        # Always run the comparison so timing doesn't reveal valid users.
        ok = ct_eq(password or "", want or "\x00invalid") and want is not None
        if ok:
            self.fails.pop(ip, None)
            return user
        if len(self.fails) > 64 and ip not in self.fails:
            # Bound memory: forget the oldest unlocked entries.
            for k in list(self.fails)[:16]:
                if not self.fails[k][1]:
                    del self.fails[k]
        e = self.fails.setdefault(ip, [0, 0])
        e[0] += 1
        if e[0] >= self.max_fail:
            e[1] = time.time() + self.lock_secs
            print("auth: locked out", ip)
        return None

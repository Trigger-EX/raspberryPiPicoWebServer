# PicoMail configuration. Copy this file to the Pico as /config.py and edit.

# --- Wi-Fi -----------------------------------------------------------------
WIFI_SSID = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"
WIFI_COUNTRY = "US"
# Fixed address so your router's port forwards keep working. Either set this
# or give the Pico a DHCP reservation on your router.
# (ip, netmask, gateway, dns)
STATIC_IP = None  # ("192.168.1.50", "255.255.255.0", "192.168.1.1", "1.1.1.1")

# --- Mail domain -----------------------------------------------------------
HOSTNAME = "mail.example.com"       # public name pointing at your home IP
DOMAINS = ["example.com"]           # domains whose mail we accept
# Mailbox users and their passwords (used for POP3 and the web inbox).
# Names: lowercase a-z, 0-9, . _ - only. Use long, unique passwords.
USERS = {
    "alice": "change-me-to-a-long-password",
}
# Extra addresses delivered to a user. postmaster is required by RFC 5321.
ALIASES = {"postmaster": "alice", "abuse": "alice"}
CATCH_ALL = None                    # e.g. "alice" to receive *@example.com

# --- NAS storage (WebDAV) --------------------------------------------------
# Folder on the NAS that holds one sub-folder per user. Create it first.
NAS_URL = "http://192.168.1.10:5005/mail"
NAS_USER = "picomail"
NAS_PASSWORD = "nas-password"
NAS_VERIFY_TLS = False              # only relevant for https:// NAS URLs

# --- Listeners (0 disables one) ---------------------------------------------
SMTP_PORT = 25        # forward from WAN: incoming mail
POP3_PORT = 110       # LAN only (plaintext)
HTTP_PORT = 80        # LAN only (plaintext)
POP3S_PORT = 995      # forward from WAN: mail apps over TLS
HTTPS_PORT = 443      # forward from WAN: web inbox over TLS

# TLS certificate + key for POP3S/HTTPS. Without them only LAN logins work.
# An EC (P-256) key is much faster on the Pico than RSA.
TLS_CERT = None       # e.g. "/certs/cert.der"
TLS_KEY = None        # e.g. "/certs/key.der"

# Refuse passwords sent without TLS from non-LAN addresses. Keep this False.
ALLOW_PLAINTEXT_AUTH_FROM_WAN = False

# --- Limits ----------------------------------------------------------------
MAX_MESSAGE_SIZE = 512 * 1024       # must fit in free flash while spooling
SPOOL_DIR = "/spool"
AUTH_MAX_FAILURES = 5               # failed logins before an IP is locked out
AUTH_LOCKOUT_SECS = 900

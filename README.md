# PicoMail: an email server on a Raspberry Pi Pico W

A Raspberry Pi Pico W handles all the mail traffic. Your NAS stores the
mailboxes. You can reach your mail from anywhere.

```
                 Internet                      Home network
 ┌──────────────┐        ┌────────┐       ┌─────────────┐  WebDAV   ┌────────┐
 │ other mail   │ SMTP   │ router │  25   │  Pico W     │──────────▶│  NAS   │
 │ servers      │───────▶│ port   │──────▶│  PicoMail   │  PUT/GET  │ /mail/ │
 └──────────────┘        │forwards│ 995   │ SMTP  POP3  │◀──────────│  alice/│
 ┌──────────────┐ POP3S/ │        │ 443   │ HTTPS inbox │           │  bob/  │
 │ phone/laptop │ HTTPS  │        │──────▶│ flash spool │           └────────┘
 └──────────────┘───────▶└────────┘       └─────────────┘
```

* **SMTP (port 25)** receives mail for your domain(s). The Pico writes each
  message to a small spool on its flash and only then replies "250 OK". A
  background task uploads it to the NAS as `<NAS_URL>/<user>/<id>.eml`. If
  the NAS is offline, messages wait in the spool and are retried.
* **POP3S (port 995)** lets a normal mail app (Thunderbird, Apple Mail,
  Outlook, K-9/FairEmail) download mail from anywhere.
* **HTTPS web inbox (port 443)** lets you read, download and delete mail
  from a browser.
* **POP3 (110) and HTTP (80)** work without TLS for use on your home
  network only. Logins arriving on them from internet addresses are refused.

Only the Pico is exposed to the internet. The NAS is never exposed: the Pico
talks to it over your LAN with its own restricted account.

## Limits

This is a microcontroller with 264 KB of RAM, so:

| | |
|---|---|
| Receiving mail | ✅ Any sender; plus-addressing (`you+tag@`), aliases, catch-all |
| Reading mail | ✅ POP3/POP3S, web inbox (shows raw message text), `.eml` download |
| **Sending mail** | ❌ Not included yet. Keep using your provider's SMTP to send. |
| IMAP | ❌ Too heavy for the Pico. POP3 with "leave on server" works in most apps. |
| Max message size | 512 KB by default (it must fit in the flash spool) |
| Concurrent connections | About 2–3 per service |
| Spam / virus filtering | ❌ None. Filter in your mail app. |
| STARTTLS on port 25 | ❌ Mail from other servers arrives unencrypted in transit |
| TLS certificate | One certificate with no intermediate chain; see [TLS](#4-tls-certificate) |

## 1. What you need

* Raspberry Pi Pico W running **MicroPython 1.24 or newer**
  (https://micropython.org/download/RPI_PICO_W/)
* A NAS with **WebDAV** turned on (Synology, QNAP, TrueNAS, OpenMediaVault,
  Nextcloud and most others support it)
* A domain name you control, and a router that can forward ports
* On your PC: `pip install mpremote mpy-cross` and `openssl`

## 2. Prepare the NAS

1. Create a shared folder, e.g. `mail`.
2. Create a NAS user (e.g. `picomail`) with read/write access to **only**
   that folder.
3. Turn on WebDAV:
   * **Synology**: install *WebDAV Server* from Package Center. HTTP uses
     port 5005, so the URL is `http://NAS-IP:5005/mail`.
   * **QNAP**: Control Panel → Applications → Web Server → WebDAV.
   * **TrueNAS**: Shares → WebDAV → add a share for the dataset, then start
     the WebDAV service.
4. Test it from a PC:
   ```sh
   curl -u picomail:PASSWORD -X PROPFIND -H "Depth: 1" http://NAS-IP:5005/mail/
   ```
   A `207 Multi-Status` XML reply means it works.

PicoMail creates one sub-folder per user by itself. Each message is a plain
`.eml` file, so you can also open or back up the mailboxes straight from the
NAS.

## 3. Configure the Pico

```sh
cp src/config_example.py src/config.py
```

Edit `src/config.py`: Wi-Fi, `HOSTNAME`, `DOMAINS`, `USERS`, `NAS_URL`,
`NAS_USER`, `NAS_PASSWORD`. Give the Pico a fixed LAN address, either with
`STATIC_IP` or a DHCP reservation on your router.

## 4. TLS certificate

TLS lets you log in from outside your home network. The Pico's TLS stack
only reads DER files and serves a single certificate, so:

```sh
# Option A: self-signed (simplest; trust it once on each of your devices)
tools/make_cert.sh mail.example.com

# Option B: convert a certificate you already have, e.g. from
#   certbot certonly --key-type ecdsa --preferred-challenges dns -d mail.example.com
tools/make_cert.sh mail.example.com fullchain.pem privkey.pem
```

Use an **EC (P-256)** key as the scripts do. RSA handshakes are much slower
on the Pico. With option B only the leaf certificate is served. Browsers and
Thunderbird usually still accept it; some phone mail apps may warn about an
incomplete chain. Let's Encrypt certificates expire after 90 days, so rerun
the conversion and redeploy when you renew.

Then set in `config.py`:

```python
TLS_CERT = "/certs/cert.der"
TLS_KEY = "/certs/key.der"
```

## 5. Deploy

Connect the Pico W over USB:

```sh
tools/deploy.sh --config --certs certs/
```

This compiles the code to `.mpy`, copies it, and resets the Pico.
Precompiling means the Pico doesn't have to compile the source at boot,
which needs a lot of temporary RAM. (If you ever need more free RAM, you
can freeze the `picomail` package into a custom MicroPython firmware build
so its bytecode stays in flash.) Watch the log with `mpremote repl`:

```
wifi: connected, ip 192.168.1.50
picomail: SMTP listening on port 25
picomail: POP3S listening on port 995
picomail: HTTPS listening on port 443
picomail: running, free RAM ...
```

The onboard LED stays on while Wi-Fi is connected. If the program crashes,
the Pico writes `/crash.log` and reboots itself.

## 6. Make it reachable from the internet

1. **Router port forwards** to the Pico's LAN IP:

   | WAN port | Pico port | Purpose |
   |---|---|---|
   | 25 | 25 | incoming mail |
   | 995 | 995 | mail apps (POP3S) |
   | 443 | 443 | web inbox (HTTPS) |

   Don't forward 80 or 110. They are plaintext and meant for the LAN.
2. **DNS** at your domain registrar:
   ```
   mail.example.com.  A     <your home public IP>
   example.com.       MX 10 mail.example.com.
   ```
   If your home IP changes, use your router's or NAS's dynamic-DNS client
   for `mail.example.com`.
3. **Check port 25.** Many home ISPs block inbound port 25. Test from
   outside (e.g. a phone on mobile data, or an online "SMTP test" tool):
   `telnet mail.example.com 25` should show `220 mail.example.com ESMTP PicoMail`.
   If it's blocked, either ask your ISP to unblock it, or have a cheap relay
   (a VPS running Postfix, or a mail forwarding service) accept mail for your
   domain and forward it to your home IP on another port. Then set
   `SMTP_PORT = 2525` and forward that port.

### Mail app settings

| | |
|---|---|
| Incoming server | `mail.example.com`, POP3, port **995**, SSL/TLS |
| Username / password | a `USERS` entry from `config.py` |
| Outgoing server | your existing provider's SMTP (PicoMail doesn't send) |

The web inbox is at `https://mail.example.com/`.

## Security

* **Not an open relay.** SMTP only accepts recipients at your own
  `DOMAINS` and never forwards mail anywhere.
* **Passwords only cross the internet inside TLS.** Plaintext POP3/HTTP
  logins from non-private IP addresses are rejected
  (`ALLOW_PLAINTEXT_AUTH_FROM_WAN = False`).
* **Brute-force lockout.** After 5 failed logins an IP is blocked for 15
  minutes. Both values are configurable.
* **Hardened web inbox.** Message content is HTML-escaped and served with a
  strict Content-Security-Policy. Delete requires a CSRF token.
* **Isolated NAS.** The Pico should use a NAS account that can only reach
  the mail folder. The NAS itself is never port-forwarded.
* Passwords live in `config.py` on the Pico's flash. Anyone with physical
  USB access to the Pico can read them.

## Development and tests

The code runs unchanged on MicroPython and on desktop Python. The tests
start PicoMail against a fake WebDAV NAS and talk to it with Python's own
`smtplib`, `poplib` and `http.client`, including TLS:

```sh
python3 -m unittest discover -s tests -v

# Run the server under MicroPython (unix port) instead, optionally limiting
# its heap to check memory headroom:
PICOMAIL_PYTHON=/path/to/micropython python3 -m unittest discover -s tests
```

The full suite passes with the server on the MicroPython 1.24.1 unix port
with a heap limited to 130 KB. That port is 64-bit, so it uses more memory
than the Pico's 32-bit chip.

## Layout

```
src/main.py              Pico entry point: Wi-Fi, NTP, start servers, auto-reboot
src/config_example.py    copy to config.py
src/picomail/smtpd.py    SMTP receiver
src/picomail/pop3d.py    POP3 / POP3S server
src/picomail/webui.py    web inbox
src/picomail/store.py    flash spool and NAS upload queue
src/picomail/webdav.py   streaming WebDAV client
src/picomail/auth.py     logins and lockout
tools/deploy.sh          compile and copy to the Pico
tools/make_cert.sh       create/convert TLS certificate
tests/                   end-to-end tests and fake NAS
```

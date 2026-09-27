"""End-to-end tests: real SMTP/POP3/HTTP clients against PicoMail and a fake NAS.

The server runs in a subprocess so it can be the MicroPython unix port:
    PICOMAIL_PYTHON=/path/to/micropython python3 -m unittest discover tests
Defaults to the current CPython interpreter.
"""
import base64
import http.client
import os
import poplib
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, HERE)
sys.path.insert(0, SRC)

from fake_nas import NAS  # noqa: E402

HOST = "127.0.0.1"


def free_port():
    s = socket.socket()
    s.bind((HOST, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_until(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in %.1fs" % timeout)


class PicoMailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="picomail-")
        cls.nas = NAS(os.path.join(cls.tmp, "nas")).start()
        cls.spool = os.path.join(cls.tmp, "spool")
        cls.ports = {k: free_port() for k in ("smtp", "pop3", "http", "pop3s", "https")}
        cert, key = os.path.join(cls.tmp, "cert.pem"), os.path.join(cls.tmp, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=localhost"],
                       check=True, capture_output=True)
        interp = os.environ.get("PICOMAIL_PYTHON", sys.executable)
        if "micropython" in os.path.basename(interp):
            # MicroPython's mbedTLS wants DER: SEC1 EC key + DER certificate.
            subprocess.run(["openssl", "ec", "-in", key, "-outform", "DER", "-out", key + ".der"],
                           check=True, capture_output=True)
            subprocess.run(["openssl", "x509", "-in", cert, "-outform", "DER", "-out", cert + ".der"],
                           check=True, capture_output=True)
            cert, key = cert + ".der", key + ".der"
        cfgdir = os.path.join(cls.tmp, "cfg")
        os.mkdir(cfgdir)
        with open(os.path.join(cfgdir, "config.py"), "w") as f:
            f.write("""
HOSTNAME = "mail.example.com"
DOMAINS = ["example.com", "example.org"]
USERS = {"alice": "alice-pw", "carol": "carol-pw"}
ALIASES = {"postmaster": "alice", "sales": "carol"}
NAS_URL = "http://%s:%d/mail"
NAS_USER = "nas"
NAS_PASSWORD = "naspw"
BIND = "%s"
SMTP_PORT = %d
POP3_PORT = %d
HTTP_PORT = %d
POP3S_PORT = %d
HTTPS_PORT = %d
TLS_CERT = %r
TLS_KEY = %r
SPOOL_DIR = %r
MAX_MESSAGE_SIZE = 64 * 1024
UPLOAD_RETRY_SECS = 1
AUTH_MAX_FAILURES = 3
AUTH_LOCKOUT_SECS = 3
""" % (HOST, cls.nas.port, HOST, cls.ports["smtp"], cls.ports["pop3"], cls.ports["http"],
       cls.ports["pop3s"], cls.ports["https"], cert, key, cls.spool))
        cmd = [interp, os.path.join(HERE, "run_server.py"), cfgdir, os.environ.get("PICOMAIL_SRC", SRC)]
        if interp == sys.executable:
            cmd.insert(1, "-u")
        cls.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = []
        while True:
            line = cls.proc.stdout.readline()
            if not line:
                raise RuntimeError("server died:\n" + "".join(out))
            out.append(line)
            if line.strip() == "READY":
                break

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        log = cls.proc.stdout.read()
        cls.proc.stdout.close()
        if os.environ.get("PICOMAIL_LOG"):
            print("\n--- server log ---\n" + log)
        cls.nas.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- helpers -----------------------------------------------------------
    def box(self, user):
        d = os.path.join(self.nas.root, user)
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def read(self, user, name):
        with open(os.path.join(self.nas.root, user, name), "rb") as f:
            return f.read()

    def clear(self):
        for u in ("alice", "carol"):
            for n in self.box(u):
                os.remove(os.path.join(self.nas.root, u, n))

    def send(self, rcpts, body, sender="bob@elsewhere.net"):
        with smtplib.SMTP(HOST, self.ports["smtp"], timeout=10) as s:
            return s.sendmail(sender, rcpts, body)

    def http(self, method, path, body=None, auth=("alice", "alice-pw"), tls=False):
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            c = http.client.HTTPSConnection(HOST, self.ports["https"], timeout=15, context=ctx)
        else:
            c = http.client.HTTPConnection(HOST, self.ports["http"], timeout=15)
        h = {}
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode()
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.headers, data

    MSG = ("From: Bob <bob@elsewhere.net>\r\nTo: alice@example.com\r\n"
           "Subject: =?utf-8?B?SGVsbG8g8J+RiyB3b3JsZA==?=\r\n\r\n"
           "Line one\r\n.leading dot\r\n..two dots\r\n.\r\nend <script>alert(1)</script>\r\n")

    # --- tests -------------------------------------------------------------
    def test_01_deliver_and_pop3(self):
        self.clear()
        # Plus-addressing, case-insensitivity and an alias all land in alice's box once.
        refused = self.send(["alice@example.com", "Alice+news@EXAMPLE.com", "postmaster@example.org"], self.MSG)
        self.assertEqual(refused, {})
        names = wait_until(lambda: self.box("alice"))
        self.assertEqual(len(names), 1)
        raw = self.read("alice", names[0])
        self.assertTrue(raw.startswith(b"Return-Path: <bob@elsewhere.net>\r\nReceived: from "))
        self.assertIn(b"\r\n\r\nLine one\r\n.leading dot\r\n..two dots\r\n.\r\nend", raw)
        wait_until(lambda: os.listdir(self.spool) == [])

        p = poplib.POP3(HOST, self.ports["pop3"], timeout=10)
        p.user("alice")
        p.pass_("alice-pw")
        count, size = p.stat()
        self.assertEqual((count, size), (1, len(raw)))
        self.assertEqual(p.uidl(1).split()[-1].decode() + ".eml", names[0])
        _, lines, octets = p.retr(1)
        self.assertEqual(b"\r\n".join(lines) + b"\r\n", raw)
        _, top, _ = p.top(1, 1)
        self.assertIn(b"Subject: =?utf-8?B?SGVsbG8g8J+RiyB3b3JsZA==?=", top)
        self.assertEqual(top[-2:], [b"", b"Line one"])
        p.dele(1)
        self.assertEqual(p.stat()[0], 0)
        p.quit()
        wait_until(lambda: not self.box("alice"))

    def test_02_multiple_users(self):
        self.clear()
        self.send(["alice@example.com", "sales@example.com"], "Subject: hi\r\n\r\nboth\r\n")
        wait_until(lambda: self.box("alice") and self.box("carol"))
        self.assertEqual(self.box("alice"), self.box("carol"))

    def test_03_not_an_open_relay(self):
        with smtplib.SMTP(HOST, self.ports["smtp"], timeout=10) as s:
            s.ehlo("test")
            s.mail("spammer@bad.example")
            self.assertEqual(s.rcpt("victim@gmail.com")[0], 550)
            self.assertEqual(s.rcpt("nobody@example.com")[0], 550)
            self.assertEqual(s.rcpt("alice@example.com.evil.net")[0], 550)
            code, _ = s.docmd("DATA")
            self.assertEqual(code, 503)

    def test_04_size_limits(self):
        with smtplib.SMTP(HOST, self.ports["smtp"], timeout=10) as s:
            s.ehlo("test")
            self.assertIn("size", s.esmtp_features)
            self.assertEqual(s.mail("bob@x.net", ["SIZE=999999"])[0], 552)
        # Plain HELO: the client can't announce a size, so the limit hits in DATA.
        with smtplib.SMTP(HOST, self.ports["smtp"], timeout=10) as s:
            s.helo("test")
            s.mail("bob@x.net")
            s.rcpt("alice@example.com")
            code, _ = s.data("Subject: big\r\n\r\n" + ("x" * 70 + "\r\n") * 1200)
            self.assertEqual(code, 552)
            self.assertEqual(s.noop()[0], 250)  # session still in sync
        self.assertEqual(os.listdir(self.spool), [])

    def test_05_raw_protocol_edge_cases(self):
        self.clear()
        s = socket.create_connection((HOST, self.ports["smtp"]), timeout=10)
        f = s.makefile("rb")
        self.assertTrue(f.readline().startswith(b"220"))
        # Pipelined commands in one packet, bare-LF line endings in the body,
        # and one line longer than the 2 KB read buffer.
        s.sendall(b"HELO a\r\nMAIL FROM:<>\r\nRCPT TO:<alice@example.com>\r\nDATA\r\n")
        for want in (b"250", b"250", b"250", b"354"):
            self.assertTrue(f.readline().startswith(want))
        long_line = b"y" * 5000
        s.sendall(b"Subject: lf\nbare lf\n" + long_line + b"\r\n.\r\nQUIT\r\n")
        self.assertTrue(f.readline().startswith(b"250"))
        self.assertTrue(f.readline().startswith(b"221"))
        s.close()
        names = wait_until(lambda: self.box("alice"))
        raw = self.read("alice", names[0])
        self.assertIn(b"Return-Path: <>\r\n", raw)
        self.assertTrue(raw.endswith(b"Subject: lf\r\nbare lf\r\n" + long_line + b"\r\n"))

    def test_06_nas_outage_is_retried(self):
        self.clear()
        self.nas.fail_puts = True
        try:
            self.send(["alice@example.com"], "Subject: queued\r\n\r\nwait for it\r\n")
            wait_until(lambda: any(n.endswith(".rcpt") for n in os.listdir(self.spool)))
            time.sleep(1.5)
            self.assertEqual(self.box("alice"), [])
        finally:
            self.nas.fail_puts = False
        wait_until(lambda: self.box("alice"), timeout=10)
        wait_until(lambda: os.listdir(self.spool) == [])

    def test_07_web_inbox(self):
        self.clear()
        self.send(["alice@example.com"], self.MSG)
        names = wait_until(lambda: self.box("alice"))
        mid = names[0][:-4]

        st, hdrs, _ = self.http("GET", "/", auth=None)
        self.assertEqual(st, 401)
        self.assertIn("Basic", hdrs["WWW-Authenticate"])

        st, _, body = self.http("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("Hello \U0001F44B world".encode(), body)
        self.assertIn(b"/m/" + mid.encode(), body)

        st, hdrs, body = self.http("GET", "/m/" + mid)
        self.assertEqual(st, 200)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn(b"<script>", body)
        self.assertIn("default-src 'none'", hdrs["Content-Security-Policy"])
        token = body.split(b"name=t value='")[1].split(b"'")[0].decode()

        st, hdrs, body = self.http("GET", "/raw/" + mid)
        self.assertEqual(body, self.read("alice", names[0]))
        self.assertEqual(hdrs["Content-Type"], "message/rfc822")

        # Other users can't see alice's mail; bad ids are rejected.
        self.assertEqual(self.http("GET", "/m/" + mid, auth=("carol", "carol-pw"))[0], 404)
        self.assertEqual(self.http("GET", "/m/../../etc")[0], 404)

        self.assertEqual(self.http("POST", "/delete", "id=%s&t=wrong" % mid)[0], 403)
        self.assertEqual(self.box("alice"), names)
        st, hdrs, _ = self.http("POST", "/delete", "id=%s&t=%s" % (mid, token))
        self.assertEqual((st, hdrs["Location"]), (303, "/"))
        self.assertEqual(self.box("alice"), [])

    def test_08_tls_listeners(self):
        self.clear()
        self.send(["alice@example.com"], "Subject: tls\r\n\r\nsecure\r\n")
        wait_until(lambda: self.box("alice"))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        p = poplib.POP3_SSL(HOST, self.ports["pop3s"], context=ctx, timeout=15)
        p.user("alice")
        p.pass_("alice-pw")
        self.assertEqual(p.stat()[0], 1)
        self.assertIn(b"secure", b"\n".join(p.retr(1)[1]))
        p.quit()
        st, _, body = self.http("GET", "/", tls=True)
        self.assertEqual(st, 200)
        self.assertIn(b"tls", body)

    def test_99_login_lockout(self):
        for _ in range(3):
            p = poplib.POP3(HOST, self.ports["pop3"], timeout=10)
            p.user("alice")
            with self.assertRaises(poplib.error_proto):
                p.pass_("wrong")
            p.close()
        # Now even the right password is refused until the lockout expires.
        p = poplib.POP3(HOST, self.ports["pop3"], timeout=10)
        p.user("alice")
        with self.assertRaises(poplib.error_proto):
            p.pass_("alice-pw")
        p.close()
        self.assertEqual(self.http("GET", "/")[0], 429)
        time.sleep(3.2)
        p = poplib.POP3(HOST, self.ports["pop3"], timeout=10)
        p.user("alice")
        p.pass_("alice-pw")
        p.quit()


class UnitTests(unittest.TestCase):
    def test_propfind_parsing_variants(self):
        from picomail.webdav import parse_propfind_block
        synology = (b'<d:response><d:href>/mail/alice/0000000001-0001-abcdef.eml</d:href><d:propstat>'
                    b'<d:prop><d:getcontentlength>1234</d:getcontentlength><d:resourcetype></d:resourcetype>'
                    b'</d:prop></d:propstat></d:response>')
        self.assertEqual(parse_propfind_block(synology), ("0000000001-0001-abcdef.eml", 1234))
        attrs = (b'<D:response><D:href>http://nas/mail/alice/x.eml</D:href><D:propstat><D:prop>'
                 b'<lp1:getcontentlength xmlns:lp1="DAV:">7</lp1:getcontentlength></D:prop></D:propstat></D:response>')
        self.assertEqual(parse_propfind_block(attrs), ("x.eml", 7))
        folder = b"<D:response><D:href>/mail/alice/</D:href><D:resourcetype><D:collection/></D:resourcetype></D:response>"
        self.assertIsNone(parse_propfind_block(folder))

    def test_plaintext_auth_policy(self):
        from picomail.auth import Auth

        class C:
            USERS = {"a": "b"}
        auth = Auth(C)
        self.assertTrue(auth.plaintext_allowed("192.168.1.20", False))
        self.assertTrue(auth.plaintext_allowed("172.20.0.1", False))
        self.assertFalse(auth.plaintext_allowed("172.32.0.1", False))
        self.assertFalse(auth.plaintext_allowed("203.0.113.9", False))
        self.assertTrue(auth.plaintext_allowed("203.0.113.9", True))

    def test_encoded_words(self):
        from picomail.webui import decode_words
        self.assertEqual(decode_words(b"=?ISO-8859-1?Q?caf=E9_ok?= =?utf-8?Q?_x?="), "café ok x".encode())
        self.assertEqual(decode_words(b"plain"), b"plain")
        self.assertEqual(decode_words(b"=?koi8-r?B?abc?="), b"=?koi8-r?B?abc?=")


if __name__ == "__main__":
    unittest.main()

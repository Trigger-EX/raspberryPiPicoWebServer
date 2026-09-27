"""A small WebDAV server that behaves like a NAS share, for tests.

Stores files under a directory, requires HTTP Basic auth, answers PROPFIND
with a chunked, Apache-style (lp1: prefixed) multistatus body and honours
Range requests.
"""
import base64
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class NAS:
    def __init__(self, root, user="nas", password="naspw"):
        self.root = root
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.fail_puts = False
        nas = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _path(self):
                p = self.path.split("?")[0]
                assert p.startswith("/mail")
                rel = p[len("/mail"):].strip("/")
                assert ".." not in rel
                return os.path.join(nas.root, rel)

            def _reply(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _authed(self):
                if self.headers.get("Authorization") != nas.auth:
                    self._body()
                    self._reply(401, b"no", {"WWW-Authenticate": 'Basic realm="nas"'})
                    return False
                return True

            def do_MKCOL(self):
                if not self._authed():
                    return
                p = self._path()
                if os.path.isdir(p):
                    return self._reply(405)
                if not os.path.isdir(os.path.dirname(p.rstrip("/"))):
                    return self._reply(409)
                os.mkdir(p)
                self._reply(201)

            def do_PUT(self):
                if not self._authed():
                    return
                data = self._body()
                if nas.fail_puts:
                    return self._reply(503)
                p = self._path()
                if not os.path.isdir(os.path.dirname(p)):
                    return self._reply(409)
                existed = os.path.exists(p)
                with open(p, "wb") as f:
                    f.write(data)
                self._reply(204 if existed else 201)

            def do_GET(self):
                if not self._authed():
                    return
                p = self._path()
                if not os.path.isfile(p):
                    return self._reply(404)
                with open(p, "rb") as f:
                    data = f.read()
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=0-"):
                    end = int(rng[8:])
                    return self._reply(206, data[: end + 1])
                self._reply(200, data, {"Content-Type": "message/rfc822"})

            def do_DELETE(self):
                if not self._authed():
                    return
                p = self._path()
                if not os.path.exists(p):
                    return self._reply(404)
                os.remove(p)
                self._reply(204)

            def do_PROPFIND(self):
                if not self._authed():
                    return
                self._body()
                p = self._path()
                if not os.path.isdir(p):
                    return self._reply(404)
                href = self.path.rstrip("/") + "/"
                parts = [b'<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:" xmlns:lp1="DAV:">\n',
                         b"<D:response><D:href>" + href.encode() + b"</D:href><D:propstat><D:prop>"
                         b"<lp1:resourcetype><D:collection/></lp1:resourcetype></D:prop>"
                         b"<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>\n"]
                for name in sorted(os.listdir(p)):
                    size = os.path.getsize(os.path.join(p, name))
                    parts.append(
                        b"<D:response xmlns:g0=\"DAV:\"><D:href>" + (href + name).encode() + b"</D:href>"
                        b"<D:propstat><D:prop><lp1:resourcetype/>"
                        b"<lp1:getcontentlength>" + str(size).encode() + b"</lp1:getcontentlength>"
                        b"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>\n")
                parts.append(b"</D:multistatus>\n")
                self.send_response(207)
                self.send_header("Content-Type", 'application/xml; charset="utf-8"')
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                # Deliberately odd chunk sizes to exercise the client parser.
                body = b"".join(parts)
                for i in range(0, len(body), 97):
                    chunk = body[i : i + 97]
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        os.makedirs(root, exist_ok=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

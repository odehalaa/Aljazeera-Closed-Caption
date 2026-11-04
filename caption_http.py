#!/usr/bin/env python3
"""
HTTP caption gateway — other machines send text via HTTP (not UDP).
Forwards to the local injector at 127.0.0.1:54001.

  POST /caption   body = plain text  (use | for new lines, CLEAR to clear)
  GET  /caption?text=HELLO
  POST /live      body = full live buffer (same as live_type UDP payload)

  curl -d 'HELLO WORLD' http://192.168.1.20:54002/caption
  curl -d 'line one|line two' http://192.168.1.20:54002/caption
  curl -d 'CLEAR' http://192.168.1.20:54002/caption
"""
from __future__ import annotations

import argparse
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def udp_send(host: str, port: int, text: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(text.encode("ascii", errors="ignore"), (host, port))
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="HTTP → local caption UDP gateway")
    ap.add_argument("--listen", default="0.0.0.0:54002", help="HTTP bind host:port")
    ap.add_argument("--cc-udp", default="127.0.0.1:54001", help="Local injector UDP")
    args = ap.parse_args()

    lh, lp = args.listen.rsplit(":", 1)
    uh, up = args.cc_udp.rsplit(":", 1)
    listen = (lh, int(lp))
    cc = (uh, int(up))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

        def _read_body(self) -> str:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return ""
            return self.rfile.read(n).decode("utf-8", errors="ignore")

        def _ok(self, msg: str = "ok") -> None:
            b = msg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b)

        def _bad(self, code: int, msg: str) -> None:
            b = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path in ("/", "/help"):
                self._ok(
                    "POST /caption  body=text\n"
                    "GET  /caption?text=HELLO\n"
                    "POST /live     body=buffer (| = new line)\n"
                    "CLEAR body or text=CLEAR to clear\n"
                )
                return
            if u.path != "/caption":
                self._bad(404, "not found\n")
                return
            text = (parse_qs(u.query).get("text") or [""])[0]
            udp_send(cc[0], cc[1], text)
            self._ok(f"sent {len(text)} bytes\n")

        def do_POST(self) -> None:
            u = urlparse(self.path)
            body = self._read_body().rstrip("\r\n")
            if u.path in ("/caption", "/live"):
                udp_send(cc[0], cc[1], body)
                self._ok(f"sent {len(body)} bytes\n")
                return
            self._bad(404, "not found\n")

    httpd = ThreadingHTTPServer(listen, Handler)
    print(
        f"[http] caption gateway http://{listen[0]}:{listen[1]}  →  udp://{cc[0]}:{cc[1]}",
        file=sys.stderr,
    )
    print(
        f"[http] example: curl -d 'HELLO' http://192.168.1.20:{listen[1]}/caption",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

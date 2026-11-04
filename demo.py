#!/usr/bin/env python3
"""
demo — pull live text from any internet source and inject CEA-608 captions.
Also serves a live web page showing every line as it is sent.

Sources (auto-detected, or forced with --type):
  rss / atom   news feeds (default)
  text         plain/UTF-8 body (one caption per non-empty line)
  json         JSON array/object — titles via --json-key (default: title)

Examples:
  ./demo
  ./demo --source https://feeds.bbci.co.uk/news/rss.xml
  ./demo --web-port 54003
  ./demo --source https://example.com/captions.txt --type text --every 1
"""
from __future__ import annotations

import argparse
import html
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

DEFAULT_SOURCES = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
]

UA = "CC608-Demo/1.0 (+live caption injector)"

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CC monitor — YouTube on-air</title>
<style>
  :root {
    --bg: #0b1020;
    --panel: #121a2f;
    --line: #1e2a4a;
    --fg: #e8eefc;
    --muted: #8b9bb8;
    --accent: #f5d76e;
    --ok: #3ddc97;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    background:
      radial-gradient(1200px 600px at 10% -10%, #1a2750 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #2a1840 0%, transparent 50%),
      var(--bg);
    color: var(--fg);
  }
  main { max-width: 920px; margin: 0 auto; padding: 28px 20px 48px; }
  header { margin-bottom: 22px; }
  h1 { margin: 0 0 6px; font-size: 1.45rem; letter-spacing: .02em; }
  .sub { color: var(--muted); font-size: .95rem; }
  .status {
    display: inline-flex; align-items: center; gap: 8px;
    margin-top: 10px; font-size: .85rem; color: var(--muted);
  }
  .cfg {
    margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px;
  }
  .cfg span {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 999px; padding: 5px 10px;
    font-size: .78rem; color: #c7d2ea;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }
  .idle {
    margin-top: 8px; font-size: .8rem; color: var(--muted);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }
  .idle.warn { color: #f0a060; }
  .idle.clear { color: #f07178; }
  .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: #666; box-shadow: 0 0 0 0 rgba(61,220,151,.5);
  }
  .dot.live {
    background: var(--ok);
    animation: pulse 1.6s ease-out infinite;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(61,220,151,.55); }
    70% { box-shadow: 0 0 0 10px rgba(61,220,151,0); }
    100% { box-shadow: 0 0 0 0 rgba(61,220,151,0); }
  }
  .now {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 22px 20px;
    margin-bottom: 18px;
    min-height: 110px;
  }
  .now .label { color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .12em; }
  .now .text {
    margin-top: 10px;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: clamp(1.25rem, 3vw, 1.85rem);
    color: var(--accent);
    line-height: 1.35;
    white-space: pre-wrap;
    word-break: break-word;
    min-height: 1.4em;
  }
  .cc-line {
    min-height: 1.35em;
    padding: 2px 0;
    border-bottom: 1px solid var(--line);
  }
  .cc-line:last-child { border-bottom: 0; }
  .rows {
    margin-top: 8px;
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    color: #c7d2ea;
    font-size: .85rem;
    opacity: .85;
    word-break: break-all;
  }
  .list {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    overflow: hidden;
  }
  .list h2 {
    margin: 0; padding: 12px 16px;
    font-size: .8rem; font-weight: 600;
    letter-spacing: .1em; text-transform: uppercase;
    color: var(--muted); border-bottom: 1px solid var(--line);
  }
  #log { list-style: none; margin: 0; padding: 0; max-height: 55vh; overflow: auto; }
  #log li {
    display: grid;
    grid-template-columns: 72px 1fr;
    gap: 12px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--line);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: .92rem;
  }
  #log li:last-child { border-bottom: 0; }
  #log .t { color: var(--muted); }
  #log .b { color: var(--fg); white-space: pre-wrap; word-break: break-word; }
  #log li.fresh .b { color: var(--accent); }
</style>
</head>
<body>
<main>
  <header>
    <h1>Live caption monitor</h1>
    <div class="sub">Read-only view of UDP sent to the injector (YouTube eye).</div>
    <div class="status"><span id="dot" class="dot"></span><span id="conn">connecting…</span></div>
    <div class="cfg" id="cfg"></div>
  </header>
  <section class="now">
    <div class="label">On air = exact injector UDP (same text as captions)</div>
    <div class="text" id="current">—</div>
    <div class="rows" id="rows"></div>
    <div class="idle" id="idle"></div>
  </section>
  <section class="list">
    <h2>Sent to injector</h2>
    <ul id="log"></ul>
  </section>
</main>
<script>
const log = document.getElementById('log');
const current = document.getElementById('current');
const rowsEl = document.getElementById('rows');
const idleEl = document.getElementById('idle');
const dot = document.getElementById('dot');
const conn = document.getElementById('conn');
const MAX = 200;
let cfg = { timeout_ms: 6000 };
let lastTs = 0;
let lastPaintKey = '';

function renderCc(ev) {
  if (!ev || ev.line === 'CLEAR' || (!ev.line && !ev.display)) {
    current.textContent = '—';
    rowsEl.textContent = '';
    return;
  }
  // display = exact UDP payload sent to cc_injector (line1|line2)
  const payload = (ev.display != null && String(ev.display).length)
    ? String(ev.display)
    : String(ev.line || '').replace(/ \| /g, '|');
  // Exact UDP lines only — 1/2/3 as sent (no empty top pad → leading "|").
  const parts = payload.split('|').filter((r, i, a) =>
    r.length > 0 || (i > 0 && i < a.length - 1));
  const win = parts.length ? parts : [''];
  current.textContent = '';
  win.forEach((r) => {
    const d = document.createElement('div');
    d.className = 'cc-line';
    d.textContent = r || ' ';
    current.appendChild(d);
  });
  rowsEl.textContent = 'injector udp: ' + payload;
}

function addLine(ev) {
  if (ev && ev.history === false) {
    renderCc(ev);
    lastTs = ev.ts || (Date.now() / 1000);
    return;
  }
  const li = document.createElement('li');
  li.className = 'fresh';
  const t = document.createElement('span'); t.className = 't'; t.textContent = ev.time || '';
  const b = document.createElement('span'); b.className = 'b';
  const payload = (ev.display != null && String(ev.display).length)
    ? String(ev.display)
    : String(ev.line || '');
  b.textContent = payload.replace(/\|/g, ' | ');
  li.appendChild(t); li.appendChild(b);
  log.prepend(li);
  while (log.children.length > MAX) log.removeChild(log.lastChild);
  setTimeout(() => li.classList.remove('fresh'), 1200);
  renderCc(ev);
  lastTs = ev.ts || (Date.now() / 1000);
}

function connect() {
  const es = new EventSource('/events');
  es.onopen = () => { dot.classList.add('live'); conn.textContent = 'live'; };
  es.onerror = () => { dot.classList.remove('live'); conn.textContent = 'reconnecting…'; };
  es.onmessage = (m) => {
    try { addLine(JSON.parse(m.data)); } catch (e) {}
  };
}

function tickIdle() {
  const ms = Number(cfg.timeout_ms || 0);
  if (!ms || !lastTs) {
    idleEl.textContent = ms ? 'waiting for first caption…' : 'auto-clear off';
    idleEl.className = 'idle';
    return;
  }
  const left = Math.max(0, ms - (Date.now() - lastTs * 1000));
  const sec = (left / 1000).toFixed(1);
  if (left <= 0) {
    idleEl.textContent = 'idle timeout — should CLEAR';
    idleEl.className = 'idle clear';
  } else if (left < 2000) {
    idleEl.textContent = `auto-clear in ${sec}s`;
    idleEl.className = 'idle warn';
  } else {
    idleEl.textContent = `auto-clear in ${sec}s`;
    idleEl.className = 'idle';
  }
}

fetch('/api/config').then(r => r.json()).then(c => {
  cfg = c || cfg;
  const el = document.getElementById('cfg');
  const bits = [
    c.config ? `cfg ${c.config}` : null,
    c.source || null,
    c.mode || null,
    c.player_delay_sec != null ? `delay ${c.player_delay_sec}s` : null,
    `rows ${c.rows}`,
    `width ${c.width}`,
    `row ${c.row}`,
    `timeout ${c.timeout_ms}ms`,
    `font ${c.font}`,
    `pen ${c.pen_size}`,
    `color ${c.color}`,
    `cc ${c.cc}`,
  ].filter(Boolean);
  el.innerHTML = bits.map(b => `<span>${b}</span>`).join('');
}).catch(() => {});

function showLast(ev) {
  if (!ev || ev.line === 'CLEAR') {
    renderCc(ev || { line: 'CLEAR' });
    return;
  }
  if (!ev.line && !ev.display) return;
  renderCc(ev);
  lastTs = ev.ts || (Date.now() / 1000);
  idleEl.textContent = '';
}

// Seed current caption immediately (don't wait for SSE).
fetch('/api/last').then(r => r.json()).then(ev => {
  if (ev && ev.line && ev.line !== 'CLEAR') {
    addLine(ev);
  } else {
    idleEl.textContent = 'waiting for first caption…';
  }
}).catch(() => {
  idleEl.textContent = 'waiting for first caption…';
});

setInterval(() => {
  fetch('/api/last').then(r => r.json()).then(showLast).catch(() => {});
  fetch('/api/health').then(r => r.json()).then(h => {
    if (h && h.injector_running === false) {
      conn.textContent = 'injector down';
      dot.classList.remove('live');
    } else if (conn.textContent === 'injector down') {
      conn.textContent = 'live';
      dot.classList.add('live');
    }
    if (h && h.last_ts) lastTs = h.last_ts;
  }).catch(() => {});
  tickIdle();
}, 400);

connect();
</script>
</body>
</html>
"""


def ascii608(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in s).strip()


def http_get(url: str, timeout: float = 20.0) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return resp.read(), ctype


def wrap_words(text: str, width: int) -> list[str]:
    """Word-wrap to <= width. Never breaks a word unless word itself > width."""
    width = max(8, min(32, width))
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            lines.append(cur[:width])
            cur = ""

    for w in words:
        w = w.strip()
        if not w:
            continue
        # Oversized token: hard-split only this word
        if len(w) > width:
            flush()
            for i in range(0, len(w), width):
                chunk = w[i : i + width]
                if len(chunk) == width:
                    lines.append(chunk)
                else:
                    cur = chunk
            continue
        trial = w if not cur else f"{cur} {w}"
        if len(trial) <= width:
            cur = trial
        else:
            flush()
            cur = w
    flush()
    # Final safety: drop any accidental mid-cut (should not happen)
    return [ln for ln in lines if ln]


def fit_line(text: str, width: int) -> str:
    """Return one caption line: full words only, <= width."""
    width = max(8, min(32, width))
    text = ascii608(text)
    if len(text) <= width:
        return text
    parts = wrap_words(text, width)
    return parts[0] if parts else text[:width]


def uniq_keep(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        t = ascii608(it)
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def parse_rss_atom(data: bytes) -> list[str]:
    root = ET.fromstring(data)
    titles: list[str] = []
    for item in root.findall(".//item"):
        t = item.findtext("title")
        if t:
            titles.append(t)
    if titles:
        return uniq_keep(titles)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//a:entry", ns):
        t = entry.findtext("a:title", default="", namespaces=ns)
        if t:
            titles.append(t)
    if not titles:
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag == "title" and el.text:
                titles.append(el.text)
        if len(titles) > 1:
            titles = titles[1:]
    return uniq_keep(titles)


def _walk_json_titles(obj: Any, key: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], (str, int, float)):
            out.append(str(obj[key]))
        for v in obj.values():
            _walk_json_titles(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json_titles(v, key, out)


def parse_json(data: bytes, key: str) -> list[str]:
    obj = json.loads(data.decode("utf-8", errors="replace"))
    found: list[str] = []
    _walk_json_titles(obj, key, found)
    if not found and isinstance(obj, list):
        for v in obj:
            if isinstance(v, str):
                found.append(v)
    return uniq_keep(found)


def parse_text(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    return uniq_keep(ascii608(raw) for raw in text.splitlines())


def detect_type(url: str, ctype: str, data: bytes) -> str:
    u = url.lower()
    if "json" in ctype or u.endswith(".json"):
        return "json"
    if "xml" in ctype or "rss" in ctype or "atom" in ctype:
        return "rss"
    if any(x in u for x in ("/rss", "rss.xml", "atom.xml", ".rss", "feeds.")):
        return "rss"
    head = data.lstrip()[:200].lower()
    if head.startswith(b"{") or head.startswith(b"["):
        return "json"
    if head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head:
        return "rss"
    return "text"


def fetch_captions(url: str, typ: str, json_key: str) -> list[str]:
    data, ctype = http_get(url)
    kind = typ if typ != "auto" else detect_type(url, ctype, data)
    if kind in ("rss", "atom"):
        return parse_rss_atom(data)
    if kind == "json":
        return parse_json(data, json_key)
    if kind == "text":
        return parse_text(data)
    raise ValueError(f"unknown type: {kind}")


def send_udp(sock: socket.socket, addr: tuple[str, int], text: str) -> None:
    sock.sendto(text.encode("ascii", errors="ignore"), addr)


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def pick_port(preferred: int) -> int:
    """Return preferred port if bindable, else an ephemeral free port."""
    if preferred > 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", preferred))
            s.close()
            return preferred
        except OSError:
            s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CaptionHub:
    """Fan-out of sent caption lines to SSE browsers."""

    def __init__(self, history: int = 100) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self.history: list[dict] = []
        self._history_max = history
        self.last: dict | None = None

    def publish(self, line: str, display: str = "", *, history: bool = True) -> None:
        """Publish caption event. display must be the exact injector UDP payload."""
        ev = {
            "time": time.strftime("%H:%M:%S"),
            "line": line,
            "display": display if display != "" else line.replace(" | ", "|"),
            "ts": time.time(),
            "history": history,
        }
        with self._lock:
            self.last = ev
            if history:
                self.history.append(ev)
                if len(self.history) > self._history_max:
                    self.history = self.history[-self._history_max :]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(q)
            snap = list(self.history[-40:])
        for ev in snap:
            try:
                q.put_nowait(ev)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


def start_web(hub: CaptionHub, host: str, port: int, config: dict | None = None) -> ThreadingHTTPServer:
    cfg = config or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:
            sys.stderr.write("[web] " + (fmt % a) + "\n")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/demo"):
                self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/config":
                payload = json.dumps(cfg).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            if path == "/api/last":
                payload = json.dumps(hub.last or {"line": ""}).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            if path == "/api/health":
                inj = False
                try:
                    for name in os.listdir("/proc"):
                        if not name.isdigit():
                            continue
                        try:
                            with open(f"/proc/{name}/comm", encoding="utf-8", errors="ignore") as cf:
                                if cf.read().strip() == "cc_injector":
                                    inj = True
                                    break
                        except OSError:
                            continue
                except OSError:
                    inj = False
                last = hub.last or {}
                last_ts = float(last.get("ts") or 0)
                timeout_ms = int(cfg.get("timeout_ms") or 0)
                idle_ms = int((time.time() - last_ts) * 1000) if last_ts else None
                clear_in_ms = None
                if timeout_ms > 0 and last_ts:
                    clear_in_ms = max(0, timeout_ms - int((time.time() - last_ts) * 1000))
                payload = json.dumps(
                    {
                        "ok": True,
                        "injector_running": inj,
                        "last_line": last.get("line", ""),
                        "last_ts": last_ts or None,
                        "idle_ms": idle_ms,
                        "timeout_ms": timeout_ms,
                        "clear_in_ms": clear_in_ms,
                        "config": cfg.get("config"),
                    }
                ).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            if path != "/events":
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q = hub.subscribe()
            try:
                self.wfile.write(b": ok\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15.0)
                        data = json.dumps(ev, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, name="demo-web", daemon=True).start()
    return httpd


def paint_roll(
    sock: socket.socket,
    addr: tuple[str, int],
    stories: list[str],
    *,
    width: int,
    rows: int,
    every: float,
    cps: float,
    uppercase: bool,
    on_send: Callable[[str, str], None] | None = None,
) -> None:
    """Send captions respecting rows/width; every on-air line is whole words."""
    width = max(8, min(32, width))
    rows = max(1, min(4, rows))
    window: list[str] = []
    delay = (1.0 / cps) if cps > 0 else 0.0
    last_published = ""

    def emit(announce: str) -> None:
        nonlocal last_published
        # Send ONE new line (no '|') so injector uses CEA-608 roll-up (CR),
        # not a full-window overwrite — much less flicker on TVs.
        line = fit_line(announce, width)
        if not line:
            return
        send_udp(sock, addr, line)
        display = "|".join(fit_line(x, width) for x in window[-rows:])
        if on_send and line != last_published:
            on_send(line, display)
            last_published = line

    for story in stories:
        text = story.upper() if uppercase else story
        text = ascii608(text)
        pieces = wrap_words(text, width)
        if not pieces:
            continue

        for piece in pieces:
            piece = fit_line(piece, width)
            if not piece:
                continue

            if cps > 0:
                # Type into a new bottom row; overflow wraps by whole words
                if len(window) >= rows:
                    window = window[1:]
                window.append("")
                buf = ""
                for ch in piece:
                    buf += ch
                    # If over width, reflow by words into current + next rows
                    if len(buf) > width:
                        wrapped = wrap_words(buf, width)
                        window[-1] = wrapped[0]
                        emit(window[-1])
                        for extra in wrapped[1:]:
                            if len(window) >= rows:
                                window = window[1:]
                            window.append(extra)
                            emit(extra)
                        buf = window[-1]
                    else:
                        window[-1] = buf
                        emit(window[-1])
                    time.sleep(delay)
            else:
                if len(window) >= rows:
                    window = window[1:]
                window.append(piece)
                emit(piece)
            time.sleep(max(0.05, every))
        time.sleep(max(0.0, every * 0.5))


def main() -> int:
    # Pre-parse --config so JSON defaults feed argparse
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", "-c", default=os.environ.get("CC_CONFIG", "config.json"))
    pre_args, _ = pre.parse_known_args()
    file_cfg: dict = {}
    try:
        from cc_config import load_config

        file_cfg = load_config(pre_args.config)
    except Exception as e:
        print(f"[demo] config load warning ({pre_args.config}): {e}", file=sys.stderr)

    cap = file_cfg.get("captions") or {}
    demo_c = file_cfg.get("demo") or {}
    web_c = file_cfg.get("web") or {}

    ap = argparse.ArgumentParser(
        prog="demo",
        description="Read live text from any internet source → CEA-608 + live web page",
    )
    ap.add_argument("--config", "-c", default=pre_args.config, help="Path to config.json")
    ap.add_argument(
        "host", nargs="?", default=str(demo_c.get("host", "127.0.0.1")),
        help="Injector host",
    )
    ap.add_argument(
        "port", nargs="?", type=int, default=int(demo_c.get("port", 54001)),
        help="Injector UDP port",
    )
    ap.add_argument(
        "--source", "-s", action="append", default=[], metavar="URL",
        help="Internet source URL (repeatable). Default from config.json demo.sources",
    )
    ap.add_argument(
        "--type", "-t",
        choices=["auto", "rss", "atom", "json", "text"],
        default=str(demo_c.get("source_type", "auto")),
        help="Force source type (default: auto-detect)",
    )
    ap.add_argument(
        "--json-key", default=str(demo_c.get("json_key", "title")),
        help="JSON field name for captions",
    )
    ap.add_argument(
        "--rows", type=int, default=int(cap.get("rows", 2)),
        help="Visible caption lines 2..4",
    )
    ap.add_argument(
        "--width", type=int, default=int(cap.get("width", 32)),
        help="Max chars per line (8..32)",
    )
    ap.add_argument(
        "--row", type=int, default=int(cap.get("row", 15)),
        help="Screen location / base row 1..15 (15=bottom) — must match injector",
    )
    ap.add_argument(
        "--timeout-ms", type=int, default=int(cap.get("timeout_ms", 6000)),
        help="Auto-CLEAR after this idle ms (0=never) — must match injector",
    )
    ap.add_argument(
        "--font", type=int, default=int(cap.get("font", 4)),
        help="CEA-708 font 0..7 (needs cc=708/both)",
    )
    ap.add_argument(
        "--pen-size",
        default=str(cap.get("pen_size", "large")),
        choices=["small", "standard", "large"],
        help="CEA-708 pen size",
    )
    ap.add_argument(
        "--color",
        default=str(cap.get("color", "yellow")),
        choices=["white", "green", "blue", "cyan", "red", "yellow", "magenta"],
        help="Caption color",
    )
    ap.add_argument(
        "--cc",
        default=str(cap.get("cc", "both")),
        choices=["608", "708", "both"],
        help="Caption standard (font/pen need 708 or both)",
    )
    ap.add_argument(
        "--every", type=float, default=float(demo_c.get("every", 1.0)),
        help="Seconds to hold each line",
    )
    ap.add_argument(
        "--cps", type=float, default=float(demo_c.get("cps", 0.0)),
        help="Chars/sec typing (0 = instant line, best for live demos)",
    )
    ap.add_argument(
        "--limit", type=int, default=int(demo_c.get("limit", 40)),
        help="Max items per fetch",
    )
    ap.add_argument(
        "--refresh", type=float, default=float(demo_c.get("refresh", 60.0)),
        help="Refetch sources every N sec",
    )
    ap.add_argument("--loop", action="store_true", default=True, help="Loop forever (default)")
    ap.add_argument("--once", action="store_true", help="One pass then exit")
    ap.add_argument(
        "--no-upper", action="store_true",
        help="Keep original case (overrides config demo.uppercase)",
    )
    ap.add_argument(
        "--web-port", type=int, default=int(web_c.get("port", 54003)),
        help="HTTP port for live caption page (0 = pick a free port)",
    )
    ap.add_argument(
        "--web-host", default=str(web_c.get("host", "0.0.0.0")),
        help="HTTP bind address",
    )
    ap.add_argument(
        "--no-web", action="store_true",
        help="Disable the live web page",
    )
    args = ap.parse_args()
    use_upper = bool(demo_c.get("uppercase", True)) and not args.no_upper
    use_web = bool(web_c.get("enabled", True)) and not args.no_web

    cfg_sources = demo_c.get("sources") if isinstance(demo_c.get("sources"), list) else None
    sources = args.source or cfg_sources or DEFAULT_SOURCES
    rows = max(2, min(4, args.rows))
    width = max(8, min(32, args.width))
    row = max(1, min(15, args.row))
    timeout_ms = max(0, args.timeout_ms)
    font = max(0, min(7, args.font))
    addr = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    loop = not args.once

    style_cfg = {
        "config": str(args.config),
        "rows": rows,
        "width": width,
        "row": row,
        "timeout_ms": timeout_ms,
        "font": font,
        "pen_size": args.pen_size,
        "color": args.color,
        "cc": args.cc,
        "every": args.every,
        "sources": sources,
    }

    hub = CaptionHub()
    httpd = None
    web_port = 0
    if use_web:
        web_port = pick_port(args.web_port)
        try:
            httpd = start_web(hub, args.web_host, web_port, style_cfg)
        except OSError:
            web_port = pick_port(0)
            httpd = start_web(hub, args.web_host, web_port, style_cfg)
        ip = local_ip()
        print(f"[demo] web page: http://{ip}:{web_port}/", file=sys.stderr)
        print(f"[demo]          http://127.0.0.1:{web_port}/", file=sys.stderr)

    print(
        f"[demo] → udp://{args.host}:{args.port}  rows={rows} width={width} "
        f"row={row} timeout_ms={timeout_ms} font={font} pen={args.pen_size} "
        f"color={args.color} cc={args.cc} every={args.every}s",
        file=sys.stderr,
    )
    for s in sources:
        print(f"[demo] source: {s}", file=sys.stderr)
    print("[demo] Ctrl+C to stop", file=sys.stderr)

    send_log = str(demo_c.get("send_log") or "").strip()
    if send_log:
        print(f"[demo] send log: {send_log}", file=sys.stderr)

    def on_send(line: str, display: str) -> None:
        hub.publish(line, display)
        print(f"[demo] send: {line}", file=sys.stderr)
        if send_log:
            try:
                with open(send_log, "a", encoding="utf-8") as lf:
                    lf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
            except OSError as e:
                print(f"[demo] send_log write failed: {e}", file=sys.stderr)

    try:
        while True:
            stories: list[str] = []
            for url in sources:
                try:
                    got = fetch_captions(url, args.type, args.json_key)[: args.limit]
                    print(f"[demo] {len(got)} items ← {url}", file=sys.stderr)
                    for t in got:
                        if t not in stories:
                            stories.append(t)
                except (
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    TimeoutError,
                    ValueError,
                    ET.ParseError,
                    json.JSONDecodeError,
                ) as e:
                    print(f"[demo] fail {url}: {e}", file=sys.stderr)

            if not stories:
                print("[demo] no captions — retry in 10s", file=sys.stderr)
                time.sleep(10)
                if not loop:
                    return 1
                continue

            paint_roll(
                sock,
                addr,
                stories,
                width=width,
                rows=rows,
                every=args.every,
                cps=args.cps,
                uppercase=use_upper,
                on_send=on_send,
            )

            if not loop:
                break
            print(f"[demo] cycle done — refresh in {args.refresh:.0f}s…", file=sys.stderr)
            send_udp(sock, addr, "CLEAR")
            hub.publish("CLEAR", "")
            time.sleep(max(1.0, args.refresh))
    except KeyboardInterrupt:
        print("\n[demo] stopped", file=sys.stderr)
    finally:
        send_udp(sock, addr, "CLEAR")
        sock.close()
        if httpd is not None:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Live-caption demo: fetch RSS headlines and type them with Enter (roll-up)."""
from __future__ import annotations

import argparse
import html
import re
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
]


def send_udp(sock: socket.socket, addr: tuple[str, int], text: str) -> None:
    sock.sendto(text.encode("ascii", errors="ignore"), addr)


def strip_tags(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # CEA-608 safe: printable ASCII only
    return "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in s)


def fetch_titles(url: str, limit: int = 40) -> list[str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CC608-RSS-Demo/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    titles: list[str] = []
    # RSS 2.0
    for item in root.findall(".//item"):
        t = item.findtext("title")
        if not t:
            continue
        t = strip_tags(t)
        if t and t not in titles:
            titles.append(t)
        if len(titles) >= limit:
            break
    if titles:
        return titles
    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//a:entry", ns):
        t = entry.findtext("a:title", default="", namespaces=ns)
        t = strip_tags(t)
        if t and t not in titles:
            titles.append(t)
        if len(titles) >= limit:
            break
    return titles


def wrap_words(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(w) > width:
            if cur:
                lines.append(cur)
                cur = ""
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
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def do_enter(lines: list[str], max_rows: int) -> list[str]:
    if lines and len(lines[-1]) == 0:
        return lines
    if len(lines) >= max_rows:
        lines = lines[1:]
    lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="RSS → live caption demo")
    ap.add_argument("host", nargs="?", default="127.0.0.1")
    ap.add_argument("port", nargs="?", type=int, default=54001)
    ap.add_argument("--feed", action="append", default=[], help="RSS/Atom URL (repeatable)")
    ap.add_argument("--rows", type=int, default=2)
    ap.add_argument("--width", type=int, default=32)
    ap.add_argument("--timeout-ms", type=int, default=6000)
    ap.add_argument("--cps", type=float, default=14.0, help="Chars per second (typing speed)")
    ap.add_argument("--pause", type=float, default=1.2, help="Seconds after each Enter")
    ap.add_argument("--loop", action="store_true", help="Refetch and repeat forever")
    ap.add_argument("--limit", type=int, default=30, help="Max headlines per feed fetch")
    args = ap.parse_args()

    max_rows = max(1, min(4, args.rows))
    width = max(8, min(32, args.width))
    feeds = args.feed or DEFAULT_FEEDS
    delay = 1.0 / max(args.cps, 1.0)
    addr = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(
        f"RSS demo → udp://{args.host}:{args.port}  rows={max_rows} width={width} "
        f"cps={args.cps}",
        file=sys.stderr,
    )
    for f in feeds:
        print(f"  feed: {f}", file=sys.stderr)
    print("Ctrl+C to stop", file=sys.stderr)

    try:
        while True:
            headlines: list[str] = []
            for url in feeds:
                try:
                    got = fetch_titles(url, limit=args.limit)
                    print(f"[rss] {len(got)} titles from {url}", file=sys.stderr)
                    for t in got:
                        if t not in headlines:
                            headlines.append(t)
                except Exception as e:
                    print(f"[rss] fail {url}: {e}", file=sys.stderr)

            if not headlines:
                print("[rss] no headlines — retry in 10s", file=sys.stderr)
                time.sleep(10)
                if not args.loop:
                    return 1
                continue

            lines: list[str] = [""]
            for title in headlines:
                for piece in wrap_words(title.upper(), width):
                    # type this screen line
                    for ch in piece:
                        if not lines:
                            lines = [""]
                        lines[-1] += ch
                        if len(lines[-1]) > width:
                            lines[-1] = lines[-1][:width]
                        send_udp(sock, addr, "|".join(lines))
                        time.sleep(delay)
                    # Enter after each wrapped segment
                    lines = do_enter(lines, max_rows)
                    send_udp(sock, addr, "|".join(lines))
                    time.sleep(args.pause)

                # Extra beat between stories
                time.sleep(max(0.3, args.pause * 0.5))

            send_udp(sock, addr, "")  # clear at end of cycle
            if not args.loop:
                break
            print("[rss] cycle done — refetch…", file=sys.stderr)
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        send_udp(sock, addr, "")
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

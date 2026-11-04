#!/usr/bin/env python3
"""Live-type captions — low-latency roll-up with key coalescing."""
from __future__ import annotations

import argparse
import select
import socket
import sys
import termios
import time
import tty


def send_udp(sock: socket.socket, addr: tuple[str, int], text: str) -> None:
    sock.sendto(text.encode("ascii", errors="ignore"), addr)


def redraw(buf: str) -> None:
    parts = buf.split("|")
    sys.stderr.write("\r\033[H\033[J")
    if not parts:
        sys.stderr.write("CC: ")
    else:
        for i, p in enumerate(parts):
            sys.stderr.write(("CC: " if i == 0 else "\r\nCC: ") + p)
    sys.stderr.flush()


def _split_overflow(text: str, width: int) -> tuple[str, str]:
    if len(text) <= width:
        return text, ""
    window = text[:width]
    sp = window.rfind(" ")
    if sp >= 0:
        return text[:sp], text[sp + 1 :]
    return text[:width], text[width:]


def append_smart(buf: str, ch: str, width: int, max_rows: int) -> str:
    lines = buf.split("|") if buf else [""]
    if not lines:
        lines = [""]

    if ch == " " and len(lines[-1]) >= width:
        if len(lines) >= max_rows:
            lines.pop(0)
        lines.append("")
        return "|".join(lines)

    lines[-1] = lines[-1] + ch
    while len(lines[-1]) > width:
        left, right = _split_overflow(lines[-1], width)
        lines[-1] = left
        if len(lines) >= max_rows:
            lines.pop(0)
        lines.append(right)
    if len(lines) > max_rows:
        lines = lines[-max_rows:]
    return "|".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live-type CEA-608 captions (roll-up)")
    ap.add_argument("host", nargs="?", default="127.0.0.1")
    ap.add_argument("port", nargs="?", type=int, default=54001)
    ap.add_argument("--timeout-ms", type=int, default=6000)
    ap.add_argument("--rows", type=int, default=2)
    ap.add_argument("--width", type=int, default=32)
    ap.add_argument("--coalesce-ms", type=int, default=0,
                    help="Batch UDP sends to at most one per N ms (0=every key, live)")
    args = ap.parse_args()

    max_rows = max(1, min(4, args.rows))
    width = max(8, min(32, args.width))
    coalesce_s = max(0, args.coalesce_ms) / 1000.0
    addr = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    timeout_s = max(0, args.timeout_ms) / 1000.0

    if not sys.stdin.isatty():
        print("Need an interactive terminal (TTY).", file=sys.stderr)
        return 1

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf = ""
    last_activity = time.monotonic()
    last_send = 0.0
    pending_send = False

    print(
        f"Live TV typing → udp://{args.host}:{args.port}  "
        f"rows={max_rows} width={width} coalesce={args.coalesce_ms}ms",
        file=sys.stderr,
    )
    print(
        "LIVE type · Enter=new line (rolls) · Backspace · Ctrl+U · Ctrl+C",
        file=sys.stderr,
    )
    redraw(buf)

    def flush_send(force: bool = False) -> None:
        nonlocal last_send, pending_send
        if not pending_send and not force:
            return
        now = time.monotonic()
        if not force and coalesce_s > 0 and (now - last_send) < coalesce_s:
            return
        send_udp(sock, addr, buf if buf else "")
        last_send = now
        pending_send = False

    try:
        tty.setraw(fd)
        while True:
            # Wake often enough for coalesce + idle timeout
            wait = 0.01 if coalesce_s > 0 else (0.05 if timeout_s > 0 else None)
            r, _, _ = select.select([fd], [], [], wait)

            now = time.monotonic()
            if pending_send:
                flush_send(False)

            if timeout_s > 0 and buf and (now - last_activity) >= timeout_s:
                buf = ""
                pending_send = True
                flush_send(True)
                redraw(buf)

            if not r:
                continue

            ch = sys.stdin.read(1)
            if not ch:
                break
            o = ord(ch)
            last_activity = time.monotonic()

            if o in (3, 4, 27):
                buf = ""
                flush_send(True)
                send_udp(sock, addr, "")
                break

            if o == 21:  # Ctrl+U
                buf = ""
                pending_send = True
                flush_send(True)
                redraw(buf)
                continue

            if o in (8, 127):
                if buf:
                    buf = buf[:-1]
                    pending_send = True
                    flush_send(False)
                    redraw(buf)
                continue

            if o in (10, 13):
                lines = buf.split("|") if buf else [""]
                if lines and len(lines[-1]) == 0:
                    continue
                # Live roll-up: always new line; drop oldest when window is full
                if len(lines) >= max_rows:
                    lines.pop(0)
                lines.append("")
                buf = "|".join(lines)
                pending_send = True
                flush_send(True)
                redraw(buf)
                continue

            if 32 <= o <= 126:
                buf = append_smart(buf, ch, width, max_rows)
                pending_send = True
                flush_send(False)
                redraw(buf)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        send_udp(sock, addr, "")
        sock.close()
        print("\ncleared / quit", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

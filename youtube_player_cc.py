#!/usr/bin/env python3
"""
Read the YouTube PLAYER on-screen captions (what your eyes see) and send
them line-by-line to cc_injector.

Uses a real Chromium YouTube player + .ytp-caption-segment DOM — not the
HLS WebVTT feed (which can differ in timing/line breaks from the overlay).

  .venv/bin/python youtube_player_cc.py
  .venv/bin/python youtube_player_cc.py --url 'https://www.youtube.com/watch?v=gCNeDWCI0vo'
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from collections import deque

from cc_config import load_config
from demo import (
    CaptionHub,
    ascii608,
    local_ip,
    pick_port,
    send_udp,
    start_web,
    wrap_words,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "[yt-eye] playwright missing — run:\n"
        "  python3 -m venv .venv && .venv/bin/pip install playwright\n"
        "  .venv/bin/playwright install chromium",
        file=sys.stderr,
    )
    raise SystemExit(1)


READ_JS = r"""
() => {
  const cont = document.querySelector('.ytp-caption-window-container')
    || document.querySelector('#caption-window-1');
  const win = document.querySelector('.ytp-caption-window-bottom')
    || document.querySelector('.ytp-caption-window')
    || (cont && cont.querySelector('.caption-window'))
    || cont;
  const lines = [];
  if (win) {
    const raw = (win.innerText || win.textContent || '').replace(/\r/g, '');
    for (const piece of raw.split(/\n+/)) {
      const ln = piece.replace(/\s+/g, ' ').trim();
      if (ln) lines.push(ln);
    }
  }
  if (!lines.length) {
    let segs = cont
      ? Array.from(cont.querySelectorAll('.ytp-caption-segment'))
      : Array.from(document.querySelectorAll('.ytp-caption-segment'));
    if (segs.length) {
      const rows = new Map();
      for (const el of segs) {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 && rect.height <= 0) continue;
        const key = Math.round(rect.top / 10) * 10;
        const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        if (!t) continue;
        if (!rows.has(key)) rows.set(key, []);
        rows.get(key).push(t);
      }
      for (const top of [...rows.keys()].sort((a, b) => a - b)) {
        const ln = rows.get(top).join(' ').replace(/\s+/g, ' ').trim();
        if (ln) lines.push(ln);
      }
    }
  }
  const uniq = [];
  for (const ln of lines) {
    if (!uniq.length || uniq[uniq.length - 1] !== ln) uniq.push(ln);
  }
  const video = document.querySelector('video');
  const live = document.querySelector('.ytp-live-badge');
  // On YouTube live+DVR, seekable.end-currentTime is NOT latency (can be hours).
  // The LIVE badge is the reliable signal: disabled => at live edge.
  const atLive = !live || live.hasAttribute('disabled');
  return {
    lines: uniq,
    paused: !!(video && video.paused),
    t: video ? video.currentTime : null,
    atLive,
    liveText: live ? (live.innerText || '').trim() : null,
    href: location.href,
    cc: !!document.querySelector('.ytp-caption-segment, .captions-text'),
  };
}
"""

# Jump to the same live edge a normal browser uses (LIVE badge).
SNAP_LIVE_JS = r"""
() => {
  const video = document.querySelector('video');
  if (!video) return { ok: false, reason: 'no-video' };
  video.muted = true;
  if (video.paused) {
    try { video.play(); } catch (e) {}
  }
  const live = document.querySelector('.ytp-live-badge');
  let snapped = false;
  // Only click when badge says we are behind (not disabled).
  if (live && !live.hasAttribute('disabled')) {
    try { live.click(); snapped = true; } catch (e) {}
  }
  return {
    ok: true,
    atLive: !live || live.hasAttribute('disabled'),
    snapped,
    href: location.href,
    liveText: live ? (live.innerText || '').trim() : null,
  };
}
"""

ENABLE_CC_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const video = document.querySelector('video');
  if (video) {
    video.muted = true;
    try { await video.play(); } catch (e) {}
  }
  // Jump to live edge if behind
  const live = document.querySelector('.ytp-live-badge');
  if (live && !/live/i.test(live.getAttribute('disabled') || '')) {
    try { live.click(); } catch (e) {}
    await sleep(200);
  }
  if (document.querySelector('.ytp-caption-segment')) return 'already';

  const ccBtn = document.querySelector('.ytp-subtitles-button');
  if (ccBtn) {
    const pressed = ccBtn.getAttribute('aria-pressed');
    if (pressed !== 'true') {
      ccBtn.click();
      await sleep(500);
    }
  }
  if (!document.querySelector('.ytp-caption-segment')) {
    const settings = document.querySelector('.ytp-settings-button');
    if (settings) {
      settings.click();
      await sleep(350);
      const items = Array.from(document.querySelectorAll('.ytp-menuitem'));
      const sub = items.find(el => /subtitles|captions|\bcc\b/i.test(el.innerText || ''));
      if (sub) {
        sub.click();
        await sleep(350);
        const opts = Array.from(document.querySelectorAll('.ytp-menuitem'));
        const en = opts.find(el => /english.*auto|auto-generated|automatic/i.test(el.innerText || ''))
                || opts.find(el => /english/i.test(el.innerText || ''));
        if (en) en.click();
        await sleep(250);
      }
      const settings2 = document.querySelector('.ytp-settings-button');
      if (settings2 && settings2.getAttribute('aria-expanded') === 'true') {
        settings2.click();
      }
    }
  }
  // Dismiss mid-roll / "skip ad" if present
  for (const sel of ['.ytp-ad-skip-button', '.ytp-ad-skip-button-modern',
                     'button.ytp-skip-ad-button', '.ytp-ad-overlay-close-button']) {
    const b = document.querySelector(sel);
    if (b) { try { b.click(); } catch (e) {} }
  }
  return document.querySelector('.ytp-caption-segment') ? 'on' : 'pending';
}
"""


def _overlay_groups(parts: list[str], width: int) -> list[list[str]]:
    """One group per YouTube on-screen row; word-wrap only single-line cues."""
    width = max(8, min(32, int(width)))
    if len(parts) >= 2:
        return [[p] for p in parts]
    painted: list[str] = []
    for p in parts:
        if len(p) <= width:
            painted.append(p)
        else:
            painted.extend(wrap_words(p, width))
    return [painted] if painted else []


def _pack_row_windows(groups: list[list[str]], rows: int) -> list[list[str]]:
    """Pack wrapped groups into consecutive on-air windows (max `rows` each)."""
    rows = max(2, min(4, int(rows)))
    windows: list[list[str]] = []
    current: list[str] = []
    for group in groups:
        chunk = list(group)
        while chunk:
            space = rows - len(current)
            if space <= 0:
                windows.append(current)
                current = []
                space = rows
            take = min(space, len(chunk))
            current.extend(chunk[:take])
            chunk = chunk[take:]
            if len(current) >= rows:
                windows.append(current)
                current = []
    if current:
        windows.append(current)
    return windows


def paint_lines(
    lines, *, width, rows, uppercase, single_upper: bool = False
) -> list[str]:
    """
    Keep YouTube's on-screen lines as-is (line-by-line).
    Only wrap a line if it exceeds CEA-608 width. Use a 3rd row only
    when that line's wrap needs it. No empty leading line.

    single_upper=True: one line on the upper row only (others cleared).
    """
    width = max(8, min(32, int(width)))  # CEA-608 hard limit
    rows = max(2, min(4, int(rows)))
    parts: list[str] = []
    for ln in lines:
        raw = ln.upper() if uppercase else ln
        raw = ascii608(raw)
        if raw:
            parts.append(raw)
    if not parts:
        return []

    if single_upper:
        text = " ".join(parts)
        wrapped = wrap_words(text, width) if len(text) > width else [text]
        one = (wrapped[-1] if wrapped else "")[:width]
        if not one:
            return []
        out = [""] * rows
        out[0] = one
        return out

    # Preserve YouTube line breaks — do not join/reflow the whole cue.
    if len(parts) >= 2:
        painted = list(parts)
        if len(painted) > rows:
            painted = painted[-rows:]
        return painted
    painted: list[str] = []
    for p in parts:
        if len(p) <= width:
            painted.append(p)
        else:
            painted.extend(wrap_words(p, width))
    if len(painted) > rows:
        painted = painted[-rows:]
    return painted


def paint_windows(
    lines, *, width, rows, uppercase, single_upper: bool = False
) -> list[list[str]]:
    """
    Like paint_lines but never drops text: when a cue wraps to more than
    `rows` lines it becomes several consecutive windows, in order.
    """
    width = max(8, min(32, int(width)))
    rows = max(2, min(4, int(rows)))
    if single_upper:
        one = paint_lines(
            lines, width=width, rows=rows, uppercase=uppercase, single_upper=True
        )
        return [one] if one else []
    parts: list[str] = []
    for ln in lines:
        raw = ln.upper() if uppercase else ln
        raw = ascii608(raw)
        if raw:
            parts.append(raw)
    groups = _overlay_groups(parts, width)
    if not groups:
        return []
    if len(parts) >= 2:
        return _pack_row_windows(groups, rows)
    painted = groups[0]
    return [painted[i : i + rows] for i in range(0, len(painted), rows)]


def emit_painted(
    sock,
    addr,
    painted: list[str],
    on_send,
    *,
    publish: bool = True,
    history: bool = True,
) -> str:
    """Send already-painted rows; web monitor shows the same UDP bytes."""
    if not painted or not any(painted):
        return ""
    payload = "|".join(painted)
    send_udp(sock, addr, payload)
    if publish and on_send:
        on_send(payload.replace("|", " | "), payload, history)
    return payload


def emit_lines(
    sock,
    addr,
    lines,
    *,
    width,
    rows,
    uppercase,
    on_send,
    publish: bool = True,
    history: bool = True,
    single_upper: bool = True,
) -> str:
    """Paint then send; web monitor shows the same bytes."""
    painted = paint_lines(
        lines,
        width=width,
        rows=rows,
        uppercase=uppercase,
        single_upper=single_upper,
    )
    return emit_painted(
        sock, addr, painted, on_send, publish=publish, history=history
    )


def main() -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", "-c", default=os.environ.get("CC_CONFIG", "config.json"))
    pre_args, _ = pre.parse_known_args()
    try:
        cfg = load_config(pre_args.config)
    except Exception as e:
        print(f"[yt-eye] config warning: {e}", file=sys.stderr)
        cfg = {}

    cap = cfg.get("captions") or {}
    demo = cfg.get("demo") or {}
    web = cfg.get("web") or {}
    yt = cfg.get("youtube") or {}

    ap = argparse.ArgumentParser(description="YouTube player on-screen CC → injector")
    ap.add_argument("--config", "-c", default=pre_args.config)
    ap.add_argument(
        "--url",
        default=str(yt.get("url") or "https://www.youtube.com/watch?v=gCNeDWCI0vo"),
    )
    ap.add_argument("--host", default=str(demo.get("host") or "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(demo.get("port") or 54001))
    ap.add_argument("--rows", type=int, default=int(cap.get("rows") or 2))
    ap.add_argument("--width", type=int, default=int(cap.get("width") if cap.get("width") is not None else 31))
    ap.add_argument("--poll", type=float, default=0.25)
    ap.add_argument("--headed", action="store_true", help="Show the browser window")
    ap.add_argument("--no-upper", action="store_true")
    ap.add_argument(
        "--two-lines",
        action="store_true",
        help="Use 2 YouTube lines (default: one upper caption line only)",
    )
    ap.add_argument("--web-port", type=int, default=int(web.get("port") or 54003))
    ap.add_argument("--web-host", default=str(web.get("host") or "0.0.0.0"))
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()

    rows = max(2, min(4, args.rows))
    width = max(8, min(32, args.width))
    single_upper = not bool(args.two_lines) and bool(yt.get("single_upper", True))
    uppercase = False if args.no_upper else bool(yt.get("uppercase", False))
    addr = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    poll = max(0.15, float(args.poll))

    style = {
        "config": args.config,
        "source": "youtube-player-eye",
        "track": "ytp-caption-segment (on-screen)",
        "note": "Scraping YouTube player overlay for gCNeDWCI0vo — snap to live edge.",
        "mode": "youtube-eye-upper1" if single_upper else "youtube-eye",
        "url": args.url,
        "rows": rows,
        "width": width,
        "single_upper": single_upper,
        "timeout_ms": int(cap.get("timeout_ms") or 12000),
        # font 0 is valid ("default") — `or` would turn it into 4
        "font": int(cap["font"]) if cap.get("font") is not None else 0,
        "pen_size": str(cap.get("pen_size") or "standard"),
        "color": str(cap.get("color") or "yellow"),
        "cc": str(cap.get("cc") or "both"),
        "every": poll,
        "at_live": None,
    }

    hub = CaptionHub()
    httpd = None
    if not args.no_web and bool(web.get("enabled", True)):
        port = pick_port(args.web_port)
        try:
            httpd = start_web(hub, args.web_host, port, style)
        except OSError:
            port = pick_port(0)
            httpd = start_web(hub, args.web_host, port, style)
        print(f"[yt-eye] web page: http://{local_ip()}:{port}/", file=sys.stderr)

    def on_send(line: str, display: str, history: bool = True) -> None:
        # display is the exact UDP payload → web "On air" matches captions
        hub.publish(line, display, history=history)
        if history:
            print(f"[yt-eye] send: {line}", file=sys.stderr)

    last_key = ""
    print(f"[yt-eye] opening player: {args.url}", file=sys.stderr)
    print(
        f"[yt-eye] → udp://{args.host}:{args.port} rows={rows} width={width} "
        f"single_upper={single_upper} poll={poll}s",
        file=sys.stderr,
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not args.headed,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-blink-features=AutomationControlled",
                    "--mute-audio",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("video", timeout=60000)
            time.sleep(2.0)

            # Dismiss cookie / consent banners if present
            for sel in (
                "button:has-text('Accept all')",
                "button:has-text('Accept')",
                "button:has-text('I agree')",
                "button:has-text('Reject all')",
            ):
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=800):
                        btn.click(timeout=1000)
                        time.sleep(0.5)
                        break
                except Exception:
                    pass

            # Click player to start, enable CC
            try:
                page.click("video", timeout=3000)
            except Exception:
                pass
            for _ in range(15):
                try:
                    state = page.evaluate(ENABLE_CC_JS)
                except Exception as e:
                    state = f"err:{e}"
                print(f"[yt-eye] captions: {state}", file=sys.stderr)
                if state in ("on", "already"):
                    break
                time.sleep(1.0)

            print("[yt-eye] reading on-screen lines — Ctrl+C to stop", file=sys.stderr)
            seen_since = time.time()  # last time overlay had real text
            last_change = time.time()  # last time caption text changed
            last_udp_hold = 0.0
            last_snap = 0.0
            last_reenable = 0.0
            empty_polls = 0
            last_lines: list[str] = []
            hold_limit = 5.0  # only bridge short gaps; don't freeze forever
            # Capture every distinct overlay (no skip). Debounce half-frame
            # glitches with 2 identical reads, then queue so fast speech is
            # sent line-by-line. CLEAR previous before each new payload.
            pending_key = ""
            pending_lines: list[str] = []
            stable_hits = 0
            stable_need = 2
            tx_queue: deque[list[str]] = deque()
            tx_phase = "idle"  # idle | clearing | showing
            tx_phase_at = 0.0
            tx_pending: list[str] = []
            last_queued_key = ""
            min_dwell = 0.40  # show each cue long enough to read
            clear_gap = 0.18
            queue_warn = 48

            def cue_hold_seconds(prev: list[str], nxt: list[str], backlog: int) -> float:
                prev_n = sum(1 for x in prev if str(x).strip())
                nxt_n = sum(1 for x in nxt if str(x).strip())
                nchars = sum(len(x) for x in prev) + sum(len(x) for x in nxt)
                hold = min_dwell
                if prev_n >= 2 or nxt_n >= 2:
                    hold = max(hold, 0.50)
                hold = max(hold, clear_gap + nchars / 55.0)
                if backlog > 12:
                    hold = max(hold, 0.40)
                elif backlog > 4:
                    hold = max(hold, 0.35)
                return min(hold, 1.1)

            tx_phase_clear_gap = clear_gap

            def recover_player(reason: str) -> None:
                print(f"[yt-eye] recover: {reason}", file=sys.stderr)
                try:
                    page.evaluate(ENABLE_CC_JS)
                    page.evaluate(SNAP_LIVE_JS)
                except Exception as e:
                    print(f"[yt-eye] recover soft fail: {e}", file=sys.stderr)
                # If still dead, full reload.
                time.sleep(0.8)
                try:
                    info2 = page.evaluate(READ_JS)
                    if not (isinstance(info2, dict) and info2.get("lines")):
                        print("[yt-eye] reload player page", file=sys.stderr)
                        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                        time.sleep(2.0)
                        try:
                            page.click("video", timeout=3000)
                        except Exception:
                            pass
                        page.evaluate(ENABLE_CC_JS)
                        page.evaluate(SNAP_LIVE_JS)
                except Exception as e:
                    print(f"[yt-eye] recover reload fail: {e}", file=sys.stderr)

            while True:
                now = time.time()
                # Stay on YouTube LIVE edge (same as browser LIVE badge).
                if now - last_snap >= 3.0:
                    try:
                        snap = page.evaluate(SNAP_LIVE_JS)
                        if isinstance(snap, dict):
                            style["at_live"] = bool(snap.get("atLive"))
                            if snap.get("snapped") or not snap.get("atLive"):
                                print(
                                    f"[yt-eye] live snap={snap.get('snapped')} "
                                    f"atLive={snap.get('atLive')} "
                                    f"badge={snap.get('liveText')!r} "
                                    f"url={snap.get('href')}",
                                    file=sys.stderr,
                                )
                        last_snap = now
                    except Exception as e:
                        print(f"[yt-eye] snap fail: {e}", file=sys.stderr)
                        last_snap = now

                try:
                    info = page.evaluate(READ_JS)
                except Exception as e:
                    print(f"[yt-eye] read fail: {e}", file=sys.stderr)
                    time.sleep(1.0)
                    continue
                if not isinstance(info, dict):
                    info = {"lines": info if isinstance(info, list) else []}
                if info.get("href") and "gCNeDWCI0vo" not in str(info.get("href")):
                    print(
                        f"[yt-eye] WARN wrong url {info.get('href')} — reloading target",
                        file=sys.stderr,
                    )
                    page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(2.0)
                    page.evaluate(ENABLE_CC_JS)
                    continue
                raw_lines = info.get("lines") or []
                lines = [ascii608(str(x)) for x in raw_lines if str(x).strip()]
                if "atLive" in info:
                    style["at_live"] = bool(info.get("atLive"))
                if info.get("paused"):
                    try:
                        page.evaluate(
                            "() => { const v=document.querySelector('video');"
                            " if(v){ v.muted=true; return v.play(); } }"
                        )
                    except Exception:
                        pass
                if lines:
                    empty_polls = 0
                    seen_since = now
                    key = "|".join(lines)
                    if key == pending_key:
                        stable_hits += 1
                    else:
                        pending_key = key
                        pending_lines = list(lines)
                        stable_hits = 1

                    # Queue every new stable overlay — never skip intermediate cues.
                    if stable_hits >= stable_need and key != last_queued_key:
                        windows = paint_windows(
                            pending_lines,
                            width=width,
                            rows=rows,
                            uppercase=uppercase,
                            single_upper=single_upper,
                        )
                        if windows:
                            last_queued_key = key
                            last_key = key
                            last_lines = list(windows[-1])
                            last_change = now
                            print(f"[yt-eye] see: {pending_lines!r}", file=sys.stderr)
                            # Never drop: every window goes on air in order. Under a
                            # deep backlog we only shorten the dwell (below).
                            for w in windows:
                                tx_queue.append(w)
                            if len(tx_queue) >= queue_warn:
                                print(
                                    f"[yt-eye] queue depth {len(tx_queue)} (draining, no drop)",
                                    file=sys.stderr,
                                )
                    elif key == last_key:
                        if (
                            (now - last_change) >= 12.0
                            and not tx_queue
                            and tx_phase == "idle"
                        ):
                            recover_player(f"same caption {now - last_change:.0f}s")
                            last_change = now
                            last_key = ""
                            last_queued_key = ""
                            pending_key = ""
                            stable_hits = 0
                else:
                    pending_key = ""
                    pending_lines = []
                    stable_hits = 0
                    empty_polls += 1
                    if empty_polls == 1 or empty_polls % 20 == 0:
                        print(
                            f"[yt-eye] no overlay (cc={info.get('cc')} "
                            f"paused={info.get('paused')} empty={empty_polls})",
                            file=sys.stderr,
                        )
                    if empty_polls >= 8 and not info.get("cc"):
                        recover_player("captions off (cc=False)")
                        empty_polls = 0
                        seen_since = now
                    gap = now - seen_since
                    if (
                        gap >= hold_limit
                        and last_lines
                        and not tx_queue
                        and tx_phase == "idle"
                    ):
                        print("[yt-eye] gap > hold — CLEAR stale caption", file=sys.stderr)
                        send_udp(sock, addr, "CLEAR")
                        hub.publish("CLEAR", "", history=True)
                        last_lines = []
                        last_key = ""
                        last_queued_key = ""
                    if (now - last_reenable) >= 1.5:
                        try:
                            st = page.evaluate(ENABLE_CC_JS)
                            if st not in ("on", "already") and empty_polls % 20 == 0:
                                print(f"[yt-eye] re-enable: {st}", file=sys.stderr)
                        except Exception as e:
                            print(f"[yt-eye] re-enable fail: {e}", file=sys.stderr)
                        last_reenable = now
                    if gap >= 15.0:
                        recover_player(f"overlay empty {gap:.0f}s")
                        seen_since = now
                        empty_polls = 0

                # Drain queue: CLEAR previous → brief blank → next line(s).
                # Deep backlog (fast speech): shorten dwell instead of dropping cues.
                backlog = len(tx_queue)
                if tx_phase == "idle" and tx_queue and (now - last_udp_hold) >= cue_hold_seconds(
                    last_lines, tx_queue[0], backlog
                ):
                    tx_pending = tx_queue.popleft()
                    send_udp(sock, addr, "CLEAR")
                    hub.publish("CLEAR", "", history=False)
                    tx_phase = "clearing"
                    tx_phase_at = now
                    nchars = sum(len(x) for x in (last_lines or []))
                    nchars += sum(len(x) for x in tx_pending)
                    tx_phase_clear_gap = min(0.55, clear_gap + nchars / 55.0)
                elif tx_phase == "clearing" and (now - tx_phase_at) >= tx_phase_clear_gap:
                    emit_painted(
                        sock,
                        addr,
                        tx_pending,
                        on_send,
                        publish=True,
                        history=True,
                    )
                    last_udp_hold = now
                    last_lines = list(tx_pending)
                    tx_pending = []
                    tx_phase = "idle"
                elif (
                    tx_phase == "idle"
                    and not tx_queue
                    and last_lines
                    and (now - last_udp_hold) >= 3.0
                    and (now - last_change) < hold_limit
                ):
                    emit_painted(
                        sock,
                        addr,
                        last_lines,
                        on_send,
                        publish=True,
                        history=False,
                    )
                    last_udp_hold = now

                time.sleep(poll)
    except KeyboardInterrupt:
        print("\n[yt-eye] stopped", file=sys.stderr)
    finally:
        send_udp(sock, addr, "CLEAR")
        hub.publish("CLEAR", "")
        sock.close()
        if httpd is not None:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

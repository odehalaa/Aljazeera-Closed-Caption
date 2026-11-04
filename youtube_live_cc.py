#!/usr/bin/env python3
"""
Pull live captions from a YouTube live stream (WebVTT) and send to cc_injector.

Requires: yt-dlp, network access.
Only works when the stream exposes caption/automatic-caption tracks.

  ./youtube_live_cc.py
  ./youtube_live_cc.py --url 'https://www.youtube.com/watch?v=gCNeDWCI0vo'
  ./youtube_live_cc.py --config config.json
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from cc_config import load_config
from demo import (
    CaptionHub,
    ascii608,
    fit_line,
    local_ip,
    pick_port,
    send_udp,
    start_web,
    wrap_words,
)

try:
    import yt_dlp
except ImportError:
    print("[yt] yt-dlp is required: pip install yt-dlp", file=sys.stderr)
    raise SystemExit(1)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TAG_RE = re.compile(r"<[^>]+>")
CUE_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)


def _ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def http_get(url: str, timeout: float = 20.0) -> bytes:
    # googlevideo caption segments require browser-like Referer or they 403
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_vtt_playlist(video_url: str, lang: str = "en") -> tuple[str, str, bool]:
    """Return (playlist_url, title, is_live) for automatic/manual captions."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    title = str(info.get("title") or video_url)
    is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")

    # Prefer manual subs, then automatic
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        # exact lang, then prefix match (en-US), then first
        candidates: list[tuple[str, list]] = []
        if lang in tracks:
            candidates.append((lang, tracks[lang]))
        for k, v in tracks.items():
            if k == "live_chat":
                continue
            if k.startswith(lang) or lang.startswith(k):
                candidates.append((k, v))
        if not candidates and tracks:
            for k, v in tracks.items():
                if k != "live_chat":
                    candidates.append((k, v))
                    break
        for _k, formats in candidates:
            for fmt in formats or []:
                if not isinstance(fmt, dict):
                    continue
                url = fmt.get("url") or ""
                ext = (fmt.get("ext") or "").lower()
                proto = (fmt.get("protocol") or "").lower()
                if url and (ext in ("vtt", "srv3", "srv2") or "m3u8" in url or "vtt" in proto or "m3u8" in proto):
                    return url, title, is_live
    raise RuntimeError(
        "No caption track found for this video (need live/automatic captions enabled on the stream)"
    )


def parse_m3u8_segments(playlist_text: str) -> list[str]:
    return [s["url"] for s in parse_m3u8_caption_segments(playlist_text)]


def parse_m3u8_caption_segments(playlist_text: str) -> list[dict]:
    """
    Parse caption HLS playlist into [{url, pdt, dur}, ...] chronologically.

    YouTube usually stamps #EXT-X-PROGRAM-DATE-TIME once, then each #EXTINF
    advances media time — used to pick the same on-screen cue as the player.
    """
    out: list[dict] = []
    pdt: datetime | None = None
    dur = 5.0
    for raw in playlist_text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            stamp = line.split(":", 1)[1].strip()
            try:
                pdt = datetime.fromisoformat(stamp)
                if pdt.tzinfo is None:
                    pdt = pdt.replace(tzinfo=timezone.utc)
            except ValueError:
                pdt = None
            continue
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = 5.0
            continue
        if line.startswith("http") and (
            "webvtt" in line or ".vtt" in line or "captions" in line
        ):
            out.append({"url": line, "pdt": pdt, "dur": float(dur)})
            if pdt is not None:
                pdt = pdt + timedelta(seconds=float(dur))
    return out


def pick_active_cue_at(
    cues_abs: list[dict], media_t: float
) -> list[str] | None:
    """Active replace-style cue at media time t (epoch seconds)."""
    active = [
        c
        for c in cues_abs
        if c["start"] <= media_t + 0.05 and c["end"] >= media_t - 0.05
    ]
    if not active:
        # Prefer the latest cue that already started (player hold).
        started = [c for c in cues_abs if c["start"] <= media_t + 0.05]
        if not started:
            return None
        chosen = max(started, key=lambda c: c["start"])
        return list(chosen["lines"])
    chosen = max(active, key=lambda c: c["start"])
    return list(chosen["lines"])


def _cue_from_buf(buf: list[str]) -> list[str]:
    """Clean one VTT cue into display lines (preserve line breaks)."""
    out: list[str] = []
    for raw in buf:
        t = ascii608(TAG_RE.sub("", raw))
        if t:
            out.append(t)
    return out


def parse_vtt_cues_timed(vtt: str) -> list[dict]:
    """Extract cues with timing + exact VTT lines (YouTube structure)."""
    lines = vtt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[dict] = []
    buf: list[str] = []
    start = end = 0.0
    in_cue = False
    pending_times = False

    def flush() -> None:
        nonlocal buf, pending_times
        body = _cue_from_buf(buf)
        if body:
            cues.append({"start": start, "end": end, "lines": body})
        buf = []
        pending_times = False

    for line in lines:
        if (
            line.startswith("WEBVTT")
            or line.startswith("NOTE")
            or line.startswith("Kind:")
            or line.startswith("X-TIMESTAMP")
        ):
            continue
        m = CUE_TIME_RE.match(line)
        if m:
            if buf or pending_times:
                flush()
            start = _ts_to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
            end = _ts_to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
            in_cue = True
            pending_times = True
            continue
        if in_cue:
            if not line.strip():
                if buf or pending_times:
                    flush()
                in_cue = False
            else:
                buf.append(line.strip())
    if buf or pending_times:
        flush()
    return cues


def parse_vtt_cues(vtt: str) -> list[list[str]]:
    return [c["lines"] for c in parse_vtt_cues_timed(vtt)]


def pick_youtube_on_screen_cue(cues: list[dict]) -> list[str] | None:
    """
    What YouTube shows at the live edge of this segment: the active cue with
    the latest start time (replace-style caption block, not a cross-cue roll).
    """
    if not cues:
        return None
    t = max(c["end"] for c in cues)
    active = [c for c in cues if c["start"] <= t and c["end"] >= t - 0.01]
    if not active:
        active = cues
    chosen = max(active, key=lambda c: c["start"])
    return list(chosen["lines"])


def cue_key(lines: list[str]) -> str:
    return "|".join(lines)


def emit_youtube_as_is(
    sock: socket.socket,
    addr: tuple[str, int],
    cue_lines: list[str],
    *,
    width: int,
    rows: int,
    uppercase: bool,
    on_send,
) -> str:
    """
    Pass the current YouTube on-screen cue AS-IS (NOT local transcription).

    Sends ONE UDP packet: line1|line2|... so the injector replaces the
    caption block with the same lines YouTube shows (no cross-cue roll mix).
    """
    painted: list[str] = []
    for ln in cue_lines:
        raw = ln.upper() if uppercase else ln
        raw = ascii608(TAG_RE.sub("", raw))
        if not raw:
            continue
        if len(raw) > width:
            raw = raw[:width]
        painted.append(raw)
    if not painted:
        return ""
    painted = painted[-rows:]
    # Single packet = exact YouTube cue block (lines separated by |)
    payload = "|".join(painted)
    send_udp(sock, addr, payload)
    on_send(payload.replace("|", " | "), payload)
    return payload


def emit_lines(
    sock: socket.socket,
    addr: tuple[str, int],
    text: str,
    *,
    width: int,
    rows: int,
    uppercase: bool,
    window: list[str],
    on_send,
) -> list[str]:
    raw = text.upper() if uppercase else text
    pieces = wrap_words(raw, width)
    for piece in pieces:
        piece = fit_line(piece, width)
        if not piece:
            continue
        if len(window) >= rows:
            window = window[1:]
        window.append(piece)
        send_udp(sock, addr, piece)
        display = "|".join(window[-rows:])
        on_send(piece, display)
    return window


def main() -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", "-c", default=os.environ.get("CC_CONFIG", "config.json"))
    pre_args, _ = pre.parse_known_args()
    try:
        cfg = load_config(pre_args.config)
    except Exception as e:
        print(f"[yt] config warning: {e}", file=sys.stderr)
        cfg = {}

    cap = cfg.get("captions") or {}
    demo = cfg.get("demo") or {}
    web = cfg.get("web") or {}
    yt = cfg.get("youtube") or {}

    ap = argparse.ArgumentParser(description="YouTube live captions → CEA-608 injector")
    ap.add_argument("--config", "-c", default=pre_args.config)
    ap.add_argument(
        "--url",
        default=str(yt.get("url") or "https://www.youtube.com/watch?v=gCNeDWCI0vo"),
        help="YouTube watch URL or video id",
    )
    ap.add_argument("--lang", default=str(yt.get("lang") or "en"))
    ap.add_argument("--host", default=str(demo.get("host") or "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(demo.get("port") or 54001))
    ap.add_argument("--rows", type=int, default=int(cap.get("rows") or 2))
    ap.add_argument("--width", type=int, default=int(cap.get("width") or 32))
    ap.add_argument("--poll", type=float, default=float(yt.get("poll_sec") or 2.0))
    ap.add_argument(
        "--refresh",
        type=float,
        default=float(yt.get("refresh_playlist_sec") or 240.0),
        help="Refresh yt-dlp playlist URL every N seconds (URLs expire)",
    )
    ap.add_argument(
        "--exact",
        action="store_true",
        default=bool(yt.get("exact", True)),
        help="Pass each YouTube VTT cue line-by-line (closest to player text)",
    )
    ap.add_argument(
        "--roll",
        action="store_true",
        help="Old behaviour: re-wrap and roll lines (less like YouTube)",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=float(yt.get("player_delay_sec") or 8.0),
        help="Seconds behind caption-tip to match YouTube player A/V latency",
    )
    ap.add_argument("--no-upper", action="store_true")
    ap.add_argument("--web-port", type=int, default=int(web.get("port") or 54003))
    ap.add_argument("--web-host", default=str(web.get("host") or "0.0.0.0"))
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()

    exact = bool(args.exact) and not bool(args.roll)
    rows = max(2, min(4, args.rows))
    width = max(8, min(32, args.width))
    player_delay = max(0.0, min(30.0, float(args.delay)))
    if exact:
        uppercase = bool(yt.get("uppercase", False)) and not args.no_upper
    else:
        uppercase = (not args.no_upper) and bool(demo.get("uppercase", True))
    addr = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    poll = max(0.5, float(args.poll))


    style = {
        "config": args.config,
        "source": "youtube-live",
        "track": f"automatic_captions/{args.lang} (WebVTT as-is)",
        "note": "Reading YouTube caption track only — not building transcription/STT.",
        "mode": "youtube-as-is" if exact else "roll",
        "url": args.url,
        "lang": args.lang,
        "player_delay_sec": player_delay,
        "rows": rows,
        "width": width,
        "timeout_ms": int(cap.get("timeout_ms") or 6000),
        "font": int(cap.get("font") or 4),
        "pen_size": str(cap.get("pen_size") or "large"),
        "color": str(cap.get("color") or "yellow"),
        "cc": str(cap.get("cc") or "both"),
        "every": poll,
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
        print(f"[yt] web page: http://{local_ip()}:{port}/", file=sys.stderr)

    def on_send(line: str, display: str) -> None:
        hub.publish(line, display)
        print(f"[yt] send: {line}", file=sys.stderr)

    seen_segs: set[str] = set()
    seen_seg_order: deque[str] = deque(maxlen=400)
    recent_cues: deque[str] = deque(maxlen=40)
    recent_cue_set: set[str] = set()
    vtt_cache: dict[str, tuple[float, list[dict]]] = {}
    window: list[str] = []
    last_text = ""
    last_payload = ""
    last_emit_at = 0.0
    playlist_url = ""
    title = ""
    last_refresh = 0.0
    if exact and poll > 0.8:
        poll = 0.8

    def remember_seg(url: str) -> None:
        if url in seen_segs:
            return
        if len(seen_seg_order) == seen_seg_order.maxlen:
            old = seen_seg_order[0]
            seen_segs.discard(old)
        seen_seg_order.append(url)
        seen_segs.add(url)

    def remember_cue(key: str) -> None:
        if key in recent_cue_set:
            return
        if len(recent_cues) == recent_cues.maxlen:
            old = recent_cues[0]
            recent_cue_set.discard(old)
        recent_cues.append(key)
        recent_cue_set.add(key)

    def load_cues(url: str) -> list[dict] | None:
        hit = vtt_cache.get(url)
        if hit and (time.time() - hit[0]) < 90.0:
            return hit[1]
        try:
            vtt = http_get(url).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            print(f"[yt] segment fail: {e}", file=sys.stderr)
            if e.code in (403, 404):
                return None  # caller may invalidate playlist
            return None
        except Exception as e:
            print(f"[yt] segment fail: {e}", file=sys.stderr)
            return None
        timed = parse_vtt_cues_timed(vtt)
        vtt_cache[url] = (time.time(), timed)
        # Bound cache size
        if len(vtt_cache) > 40:
            for old_url, _ in sorted(vtt_cache.items(), key=lambda kv: kv[1][0])[:15]:
                vtt_cache.pop(old_url, None)
        return timed

    print(f"[yt] video: {args.url}", file=sys.stderr)
    print(
        f"[yt] → udp://{args.host}:{args.port} rows={rows} width={width} "
        f"lang={args.lang} mode={'yt-cue-exact' if exact else 'roll'} "
        f"poll={poll}s delay={player_delay}s",
        file=sys.stderr,
    )
    if exact:
        print(
            "[yt] AS-IS: on-screen YouTube cue at player latency → line1|line2 "
            "(same caption track as the player; not STT).",
            file=sys.stderr,
        )
    print("[yt] Ctrl+C to stop", file=sys.stderr)

    try:
        while True:
            now = time.time()
            if not playlist_url or (now - last_refresh) >= args.refresh:
                try:
                    playlist_url, title, is_live = resolve_vtt_playlist(args.url, args.lang)
                    last_refresh = now
                    print(
                        f"[yt] playlist ok — live={is_live} title={title[:80]}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    print(f"[yt] resolve failed: {e}", file=sys.stderr)
                    time.sleep(5)
                    continue

            try:
                plist = http_get(playlist_url).decode("utf-8", errors="replace")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"[yt] playlist fetch failed ({e}) — refreshing…", file=sys.stderr)
                playlist_url = ""
                time.sleep(2)
                continue

            meta_segs = parse_m3u8_caption_segments(plist)
            segs = [s["url"] for s in meta_segs]
            new_segs = [s for s in segs if s not in seen_segs]
            # DVR playlists are huge — stay near the live tip.
            keep = 4 if exact else 3
            if len(new_segs) > keep:
                for s in new_segs[:-keep]:
                    remember_seg(s)
                new_segs = new_segs[-keep:]

            got_exact = False
            if exact:
                # Match YouTube player: pick the cue active at
                # (caption tip media time − player A/V delay). That is what
                # viewers see with CC on, not the raw undelayed tip.
                if not meta_segs:
                    time.sleep(1.0)
                    continue

                tip = meta_segs[-1]
                if tip["pdt"] is not None:
                    tip_end = tip["pdt"].timestamp() + float(tip["dur"])
                    media_now = tip_end - player_delay
                    # Only fetch segments that can cover media_now (±1 segment).
                    window_meta = []
                    for sm in meta_segs[-12:]:
                        if sm["pdt"] is None:
                            continue
                        s0 = sm["pdt"].timestamp()
                        s1 = s0 + float(sm["dur"])
                        if s1 >= media_now - 1.0 and s0 <= media_now + float(sm["dur"]) + 1.0:
                            window_meta.append(sm)
                    if not window_meta:
                        window_meta = meta_segs[-4:]
                else:
                    media_now = None
                    window_meta = meta_segs[-3:]

                cues_abs: list[dict] = []
                fetch_ok = False
                hard_fail = 0
                for sm in window_meta:
                    url = sm["url"]
                    remember_seg(url)
                    timed = load_cues(url)
                    if timed is None:
                        if url not in vtt_cache:
                            hard_fail += 1
                        continue
                    fetch_ok = True
                    if sm["pdt"] is not None:
                        base = sm["pdt"].timestamp()
                        for c in timed:
                            cues_abs.append(
                                {
                                    "start": base + float(c["start"]),
                                    "end": base + float(c["end"]),
                                    "lines": list(c["lines"]),
                                }
                            )
                    elif url == tip["url"]:
                        edge = pick_youtube_on_screen_cue(timed)
                        if edge and cue_key(edge) != last_text:
                            last_text = cue_key(edge)
                            remember_cue(last_text)
                            print(f"[yt] as-is cue: {edge!r}", file=sys.stderr)
                            emit_youtube_as_is(
                                sock,
                                addr,
                                edge,
                                width=width,
                                rows=rows,
                                uppercase=uppercase,
                                on_send=on_send,
                            )
                            got_exact = True

                if hard_fail and not fetch_ok:
                    print("[yt] segments 403/fail — refreshing playlist…", file=sys.stderr)
                    playlist_url = ""
                    vtt_cache.clear()
                    time.sleep(0.8)
                    continue

                if media_now is not None and cues_abs:
                    cue_lines = pick_active_cue_at(cues_abs, media_now)
                    # Skip useless stub one-liners if a fuller nearby cue exists.
                    if cue_lines and sum(len(x) for x in cue_lines) < 12:
                        richer = pick_active_cue_at(cues_abs, media_now - 1.0)
                        if richer and sum(len(x) for x in richer) >= 12:
                            cue_lines = richer
                    if cue_lines:
                        key = cue_key(cue_lines)
                        if key != last_text:
                            last_text = key
                            remember_cue(key)
                            print(
                                f"[yt] as-is cue (t-{player_delay:.0f}s): {cue_lines!r}",
                                file=sys.stderr,
                            )
                            last_payload = emit_youtube_as_is(
                                sock,
                                addr,
                                cue_lines,
                                width=width,
                                rows=rows,
                                uppercase=uppercase,
                                on_send=on_send,
                            )
                            last_emit_at = time.time()
                        elif last_payload:
                            # Keep injector/web from auto-CLEAR during quiet polls.
                            if (time.time() - last_emit_at) >= max(2.5, player_delay * 0.35):
                                send_udp(sock, addr, last_payload)
                                on_send(
                                    last_payload.replace("|", " | "),
                                    last_payload,
                                )
                                last_emit_at = time.time()
                        got_exact = True

                for s in new_segs:
                    remember_seg(s)
            else:
                for seg in new_segs:
                    remember_seg(seg)
                    try:
                        vtt = http_get(seg).decode("utf-8", errors="replace")
                    except urllib.error.HTTPError as e:
                        print(f"[yt] segment fail: {e}", file=sys.stderr)
                        if e.code in (403, 404):
                            playlist_url = ""
                        continue
                    except Exception as e:
                        print(f"[yt] segment fail: {e}", file=sys.stderr)
                        continue
                    for cue in parse_vtt_cues(vtt):
                        text = " ".join(cue)
                        if not text or text == last_text:
                            continue
                        if last_text and text.startswith(last_text) and len(text) - len(last_text) < 3:
                            continue
                        last_text = text
                        window = emit_lines(
                            sock,
                            addr,
                            text,
                            width=width,
                            rows=rows,
                            uppercase=uppercase,
                            window=window,
                            on_send=on_send,
                        )

            if exact and not got_exact and not playlist_url:
                time.sleep(1.0)
                continue

            if not playlist_url:
                time.sleep(1.0)
                continue
            # Poll often so cue boundaries inside a 5s segment are caught.
            time.sleep(max(0.35, poll if not exact else min(poll, 0.5)))
    except KeyboardInterrupt:
        print("\n[yt] stopped", file=sys.stderr)
    finally:
        send_udp(sock, addr, "CLEAR")
        hub.publish("CLEAR", "")
        sock.close()
        if httpd is not None:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

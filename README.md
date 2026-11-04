# Al Jazeera Closed Caption (CEA-608 / CEA-708 → MPEG-TS)

**Version 1.0.1**

Live closed-caption injector for MPEG-TS H.264. Plain-text captions arrive over UDP and are embedded as **CEA-608 (CC1)** and/or **CEA-708 (DTVCC Service 1)** inside **A/53 GA94 SEI** on the video bitstream. Video and audio are **bitstream-copied** (no re-encode).

Typical use: take a live multicast/unicast TS, inject captions from a demo feed, live keyboard, HTTP API, or **YouTube on-screen captions**, and redistribute another multicast for VLC / STBs / playout.

---

## Table of contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Requirements](#requirements)
4. [Build](#build)
5. [Configuration (`config.json`)](#configuration-configjson)
6. [Run](#run)
7. [Caption sources](#caption-sources)
8. [UDP caption protocol](#udp-caption-protocol)
9. [Watch / test in VLC](#watch--test-in-vlc)
10. [CLI reference (`cc_injector`)](#cli-reference-cc_injector)
11. [Scripts cheat sheet](#scripts-cheat-sheet)
12. [Troubleshooting](#troubleshooting)
13. [License](#license)

---

## Features

| Area | What you get |
|------|----------------|
| Standards | CEA-608 CC1, CEA-708 Service 1, or **both** |
| Transport | MPEG-TS in → MPEG-TS out (UDP file/unicast/multicast) |
| Video | H.264 Annex-B **copy** + A/53 SEI inject |
| Audio | Passthrough when present |
| Layout | 2–4 roll-up rows, PAC row 1–15, width ≤ 32 |
| Style | Color, underline, italics, flash, indent; 708 font + pen size |
| Ingest | UDP text, RSS/JSON demo, live type, HTTP gateway, YouTube eye |
| Clear | Strong CLEAR (EDM + per-row DER, repeated) so leftovers vanish |
| Monitor | Local web page showing exact UDP payloads |

---

## Architecture

```
  ┌─────────────────────┐     UDP text      ┌──────────────────┐
  │ demo / live_type /  │ ───────────────►  │   cc_injector    │
  │ youtube_eye / HTTP  │   :54001          │  (C++ / FFmpeg)  │
  └─────────────────────┘                   └────────┬─────────┘
                                                     │
     MPEG-TS in (e.g. 239.195.1.17:4900) ───────────┤
                                                     ▼
                                            MPEG-TS out + CC
                                            (e.g. 239.195.11.11:4900)
                                                     │
                                                     ▼
                                              VLC / STB (CC1)
```

- **`cc_injector`** — core encoder path: read TS, inject SEI, write TS.
- **`config.json` + `cc_config.py`** — documented JSON; CLI/env override.
- **`demo.py` / `./demo`** — RSS/JSON/text → UDP + optional web UI.
- **`youtube_player_cc.py` / `./youtube_eye`** — Playwright reads **only** the YouTube CC overlay (CC on, no `[Music]` rows). **Finalized** cues go on air when the overlay clears or the next cue starts. Each new cue is preceded by a **full window CLEAR** (all `rows`, e.g. 3) so unused lines are blank; 1–2 line cues only use those rows. **Idle CLEAR** after `timeout_ms` with no overlay text (default **8000** ms).
- **`youtube_live_cc.py` / `./youtube_cc`** — alternate path via yt-dlp WebVTT (can differ from player overlay).
- **`live_type.py`** — keyboard typing into captions.
- **`caption_http.py`** — HTTP → UDP bridge.

---

## Requirements

**Tested on:** Ubuntu 24.04+ (Linux).

```bash
sudo apt update
sudo apt install -y \
  g++ pkg-config \
  libavformat-dev libavcodec-dev libavutil-dev libswresample-dev \
  ffmpeg netcat-openbsd \
  python3 python3-venv python3-pip
```

Optional (YouTube player-eye):

```bash
cd /path/to/Aljazeera-Closed-Caption
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
```

Optional (YouTube WebVTT path): `pip install yt-dlp` (system or venv).

---

## Build

```bash
git clone https://github.com/odehalaa/Aljazeera-Closed-Caption.git
cd Aljazeera-Closed-Caption

g++ -std=c++17 -O2 cc_injector.cpp \
  $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) \
  -o cc_injector

./cc_injector --help
```

`./start_injector.sh` builds automatically if `./cc_injector` is missing.

---

## Configuration (`config.json`)

All runtime knobs live in **`config.json`**. Keys starting with `_` are documentation only and are ignored by the loader.

**Override order (highest wins):** CLI flags → environment variables → `config.json`.

```bash
python3 ./cc_config.py ./config.json --env          # print shell exports
python3 ./cc_config.py ./config.json --env IN       # one value
ROWS=2 ./start_injector.sh                          # env override
```

### `stream` — MPEG-TS path

| Key | Type | Default (example) | Description |
|-----|------|-------------------|-------------|
| `input` | string | `udp://239.195.1.17:4900?localaddr=…` | Source MPEG-TS. For multicast, set `localaddr=` to **this machine’s NIC IP**. |
| `output` | string | `udp://239.195.11.11:4900?pkt_size=1316&ttl=16&localaddr=…` | Destination viewers tune (VLC: `udp://@239.195.11.11:4900`). |
| `caption_udp` | string | `0.0.0.0:54001` | Bind address for UDP caption ingest. |

Edit `localaddr` to match your NIC before production use.

### `captions` — look & behaviour (passed to `cc_injector`)

| Key | Allowed | Default | Description |
|-----|---------|---------|-------------|
| `rows` | 2, 3, 4 | `3` | Max roll-up window. Senders normally use 1–2 lines; 3rd only if needed. |
| `row` | 1..15 | `13` | Base PAC row (15 = bottom). Window grows **upward**. |
| `width` | 8..32 | `31` | Max ASCII chars per line. The 608 row has 32 cells but the colour mid-row code uses one, so **31** is the real printable width — `32` silently drops the last letter on TVs. |
| `timeout_ms` | ≥0 | `8000` | Auto-CLEAR after idle ms with **no on-screen caption text** (`0` = never). Injector uses the same value.
| `color` | white green blue cyan red yellow magenta | `yellow` | Foreground. |
| `cc` | `608` / `708` / `both` | `both` | `608` = universal CC1; `708` adds font/size; `both` recommended. |
| `font` | 0..7 | `0` | CEA-708 only. **0 = default** (most compatible). 1 mono, 2 prop, 3 mono_sans, 4 casual, 5 cursive, 6 small_caps, 7 prop_sans. |
| `pen_size` | small / standard / large | `standard` | CEA-708 size. `standard` safest across players. |
| `bootstrap` | 0 / 1 | `0` | Inject short “CC ONLINE” at start. |
| `sei_every` | 1..30 | `15` | Re-send keep-alive SEI every N video packets while holding. |
| `clear_frames` | 1..120 | `6` | Repeat CLEAR (EDM + row DER) this many packets. VLC often needs ≥4–6. |

### `demo` — internet headline ticker

| Key | Description |
|-----|-------------|
| `host` / `port` | Where to send UDP (`127.0.0.1:54001`). |
| `sources` | List of RSS/Atom/JSON/text URLs. |
| `source_type` | `auto` / `rss` / `atom` / `json` / `text`. |
| `json_key` | Field name when parsing JSON (default `title`). |
| `every` | Seconds each wrapped line stays on air. |
| `cps` | Chars/sec typing effect (`0` = whole line). |
| `limit` | Max items per fetch. |
| `refresh` | Seconds before refetch after a full pass. |
| `uppercase` | Force UPPER CASE. |
| `apply_injector` | Restart local injector from this config before sending. |
| `send_log` | Optional path for send log (empty = off). |

### `web` — live monitor

| Key | Description |
|-----|-------------|
| `enabled` | Serve monitor UI. |
| `host` | Bind (`0.0.0.0` = all interfaces). |
| `port` | HTTP port (default `54003`). Open `http://HOST:54003/`. |

### `youtube` — live YouTube ingest

| Key | Description |
|-----|-------------|
| `url` | Watch URL or video id. |
| `lang` | Preferred caption language. |
| `poll_sec` | Playlist / poll interval. |
| `refresh_playlist_sec` | Re-resolve yt-dlp URL (signed URLs expire). |
| `exact` | Exact cue blocks (WebVTT path). |
| `single_upper` | `youtube_eye`: one upper line only if `true`. |
| `player_delay_sec` | WebVTT vs player latency heuristic. |
| `uppercase` | Force upper in direct/exact mode. |
| `poll_sec_direct` | Faster poll hint for exact mode. |

---

## Run

### A) Production-style: config → injector

```bash
# 1. Edit config.json (input/output/localaddr/rows/row/color/…)

# 2. Start injector
./start_injector.sh ./config.json

# Or manually:
IN=$(python3 ./cc_config.py ./config.json --env IN)
OUT=$(python3 ./cc_config.py ./config.json --env OUT)
CC_UDP=$(python3 ./cc_config.py ./config.json --env CC_UDP)
./cc_injector "$IN" "$OUT" \
  --cc-udp="$CC_UDP" \
  --rows=3 --width=32 --row=13 \
  --timeout_ms=12000 \
  --cc=both --font=0 --pen_size=standard \
  --color=yellow --bootstrap=1 \
  --sei_every=2 --clear_frames=6 --cc_every=1
```

### B) Local loopback test (no multicast)

**Terminal 1 — fake video:**

```bash
ffmpeg -re \
  -f lavfi -i smptebars=size=1280x720:rate=25 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -map 0:v -map 1:a \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 128k \
  -f mpegts "udp://127.0.0.1:5000?pkt_size=1316"
```

**Terminal 2 — injector:**

```bash
./cc_injector \
  "udp://127.0.0.1:5000?fifo_size=1000000&overrun_nonfatal=1&timeout=5000000" \
  "udp://127.0.0.1:5004?pkt_size=1316" \
  --cc-udp=0.0.0.0:54001 \
  --rows=2 --width=32 --row=15 \
  --cc=both --font=0 --pen_size=standard \
  --color=yellow --timeout_ms=8000 --clear_frames=6
```

**Terminal 3 — send text:**

```bash
printf 'HELLO WORLD\n' | nc -u -w0 127.0.0.1 54001
printf 'Line one|Line two\n' | nc -u -w0 127.0.0.1 54001
printf 'CLEAR\n' | nc -u -w0 127.0.0.1 54001
```

**Terminal 4 — VLC:** open `udp://@127.0.0.1:5004` → Subtitle → **Closed Captions / CC1**.

### C) Demo ticker + web monitor

```bash
./demo
# Web UI: http://<host>:54003/
```

### D) YouTube on-screen captions (recommended for “as eyes see”)

```bash
./start_injector.sh ./config.json &
./youtube_eye --config ./config.json \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --poll 0.15 --rows=3
```

Behaviour:

- Scrapes `.ytp-caption-segment` in a headless Chromium player.
- Snaps to YouTube **LIVE** badge.
- Preserves YouTube line breaks (wraps only if &gt; 32 chars).
- Queues cues so fast speech is not skipped.
- Each new finalized cue: **CLEAR** all caption rows (`clear_frames` in MPEG-TS), brief gap, then paint the next 1–2 lines.
- **Idle CLEAR** when the YouTube overlay has been empty for `captions.timeout_ms` (default 8 seconds).

---

## Caption sources

| Tool | Command | Notes |
|------|---------|-------|
| Manual UDP | `nc -u` | Fastest smoke test |
| Random test | `./send_random_captions.sh` | Stress / demo |
| RSS demo | `./demo` | Uses `config.json` → `demo` + `web` |
| Live keyboard | `./run_live_type.sh` or `python3 live_type.py 127.0.0.1 54001` | Enter = new line |
| HTTP bridge | `./run_caption_http.sh` | POST text → UDP |
| YouTube eye | `./youtube_eye` | Player overlay (Playwright) |
| YouTube VTT | `./youtube_cc` | yt-dlp WebVTT (may differ from overlay) |

---

## UDP caption protocol

Send to `--cc-udp` (default port **54001**):

| Payload | Effect |
|---------|--------|
| `Hello captions\n` | One line (auto word-wrap at `--width`) |
| `Line one\|Line two\n` | Multi-line window (`\|` = hard break) |
| `CLEAR` / `CLS` / empty datagram | Erase on-screen captions |
| `[yellow+u]ALERT` | Inline style for that message |

**Inline tags:** colors `w/g/b/c/r/y/m`, `+u` underline, `+i` italics, `+flash`, `+bgNAME`, `+bgtransparent`.

ASCII only (`0x20`–`0x7E`). Max **32** characters per painted line.

---

## Watch / test in VLC

1. Media → Open Network: `udp://@239.195.11.11:4900` (or your `stream.output`).
2. Subtitle menu → **Closed Captions** → **CC1** (608).  
   For 708: DTVCC / Service 1 if listed.
3. If nothing shows: confirm injector log has `[cc] recv: …`, try `--bootstrap=1`, raise `clear_frames`.

---

## CLI reference (`cc_injector`)

```text
./cc_injector [input] [output] [options]
./cc_injector -h | --help
```

### Core

| Flag | Meaning |
|------|---------|
| `--cc-udp=HOST:PORT` | UDP caption listen (required for live text) |
| `--bootstrap=0\|1` | Startup “CC ONLINE” |
| `--rows=N` / `--ru=N` | 2..4 visible roll-up lines |
| `--row=N` / `--base_row=N` | PAC row 1..15 (15 = bottom) |
| `--width=N` / `--cols=N` | Max chars/line (≤32) |
| `--timeout_ms=N` | Auto-CLEAR idle (`0` = never) |
| `--cc_every=N` | Inject on every Nth video packet |
| `--line_gap=N` | Packets between rolled lines |
| `--clear_frames=N` | CLEAR repeat count |
| `--sei_every=N` | Keep-alive SEI spacing |
| `--quiet` | Less logging |
| `--fast` | Low-latency preset (also quieter) |
| `--live` | Force overwrite mode (no roll-up) |

### Format / style

| Flag | Meaning |
|------|---------|
| `--cc=608\|708\|both` | Which standards to embed |
| `--font=0..7` | 708 font tag |
| `--pen_size=small\|standard\|large` | 708 size |
| `--color=NAME` | Foreground |
| `--bg=NAME` | Background (many STBs ignore) |
| `--indent=N` | 0,4,8,…,28 |
| `--underline` / `--italics` / `--flash` | Style |

---

## Scripts cheat sheet

| Script | Purpose |
|--------|---------|
| `./start_injector.sh [config.json]` | Build if needed + start from config |
| `./demo` | RSS/demo + web monitor |
| `./youtube_eye` | YouTube player overlay → UDP |
| `./youtube_cc` | YouTube WebVTT → UDP |
| `./run_live_type.sh` | Keyboard captions |
| `./run_caption_http.sh` | HTTP → UDP |
| `./send_random_captions.sh` | Random test lines |
| `./run_injector.sh` / `./run_injector_fast.sh` | Legacy launcher helpers |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No CC in VLC | Enable **CC1**; use injector **output** URL; try `--bootstrap=1` |
| Leftover characters after CLEAR | Raise `clear_frames` (6–8); ensure CLEAR finishes before next text (current code does) |
| Truncated last letters (`offering`→`offer`) | Fixed in v1.0.1: UDP events are applied in order **after** the previous SEI queue drains, and CC is muxed only on anchor (non-B) frames so TVs see chunks in display order |
| Last letter of a full-width line missing | Use `width: 31` (default since v1.0.1) — the colour code occupies the 32nd cell |
| Whole cues missing when sent fast (`nc` bursts, HTTP) | Fixed in v1.0.1: no more “latest datagram wins”; every datagram is queued FIFO |
| Fast speech lags behind | Expected: nothing is dropped anymore; the eye shortens dwell under backlog and catches up |
| Web ≠ YouTube text | Prefer `./youtube_eye` (overlay) over WebVTT; snap LIVE badge |
| Multicast not received | Set `localaddr=` to correct NIC; check IGMP / firewall |
| Font ignored | `--cc=608` has no font; use `--cc=both` and `--font=0` |
| Flicker | Raise `--sei_every`; avoid spamming identical paints |

Logs:

```bash
tail -f /tmp/cc_injector.log    # if started via tee/start script
# Look for: [cc] recv: …   [cc] CLEAR start …   [cc] queue len=…
```

---

## Project layout

```text
cc_injector.cpp      Core injector (build → cc_injector)
config.json          Documented runtime config
cc_config.py         Config loader / env export
demo.py / demo       Internet caption demo + web UI
youtube_player_cc.py YouTube on-screen eye
youtube_eye          Launcher for player eye
youtube_live_cc.py   YouTube WebVTT ingest
youtube_cc           Launcher for WebVTT path
live_type.py         Keyboard → UDP
caption_http.py      HTTP → UDP
start_injector.sh    Config-driven injector start
AGENTS.md            Deep dive for humans & coding agents
README.md            This file
LICENSE              MIT
```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Release

**v1.0.1** — Validation fixes from live TV and web monitor testing:

- **Injector:** FIFO caption UDP (no “latest datagram wins”); defer new paint/CLEAR until the SEI queue drains; B-frame-safe SEI on forward PTS carriers; pop-on two-line 608 for `|` payloads; resume preamble for long paints; skip 708 on multi-line when `cc=both` (STB line-2 glyph issues); larger caption UDP receive buffer; default printable **width 31** (colour uses the 32nd cell).
- **YouTube eye:** `paint_windows` without dropping queued rows; segment newline handling; hold/backlog tuning; recover when overlay CC is off; width/font defaults aligned with config.
- **Web monitor:** read-only UI (YouTube-only send path).
- **Ops:** `start.sh` / `stop.sh` to run injector + eye from config.

**v1.0** — Live CEA-608/708 MPEG-TS injector with JSON config, demo/web monitor, YouTube eye ingest, strong CLEAR, and dual 608+708 output.

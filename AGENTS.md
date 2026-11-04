# AGENTS.md — Al Jazeera Closed Caption

Complete reference for humans and coding agents working in this repository.

**Repo:** https://github.com/odehalaa/Aljazeera-Closed-Caption  
**Release:** 1.0  
**Primary binary:** `cc_injector` (built from `cc_injector.cpp`)

---

## 1. What this project is

A **live closed-caption pipeline** that:

1. Reads an **MPEG-TS** H.264 stream (UDP multicast/unicast or file).
2. Accepts **plain ASCII caption text** over UDP.
3. Embeds **CEA-608** and/or **CEA-708** as **A/53 GA94 SEI** on video packets.
4. Writes a new MPEG-TS (bitstream **copy** of video/audio — no x264 re-encode of the picture).

It is **not** DVB teletext, not a separate caption PID, and not a WebVTT muxer. Players must decode **CC1 / DTVCC** from the H.264 SEI.

---

## 2. Non-negotiable constraints (CEA-608)

| Constraint | Value | Why it matters |
|------------|-------|----------------|
| Chars per line | ≤ **32** | Hard CEA-608 limit; YouTube overlay also ~32 |
| Visible rows | **2–4** | Roll-up window size (`--rows`) |
| Vertical position | PAC row **1–15** | `--row` / `captions.row`; window grows **upward** |
| A/53 SEI capacity | ≤ **31** cc_data triplets per SEI | Long paints must **queue and drain** across packets |
| Character set | ASCII `0x20`–`0x7E` | Non-ASCII stripped/replaced |
| Font in 608 | **None** | Only 708 carries `--font` / `--pen_size` |

**Agent rule:** Never “fix” truncation by padding with spaces (VLC shows long blank bars). Prefer **DER** (delete-to-end-of-row). Never paint the `|` character — it is only an internal line separator.

---

## 3. Repository map

| Path | Role |
|------|------|
| `cc_injector.cpp` | All injection logic (608 paint, 708 window, SEI, UDP, mux) |
| `cc_injector` | Built binary (gitignored; rebuild with g++) |
| `config.json` | Documented JSON; `_` keys are docs-only |
| `cc_config.py` | Load/validate config; `--env` exports for shells |
| `start_injector.sh` | Build-if-needed + start from config |
| `demo.py` / `demo` | RSS/JSON/text → UDP + web monitor (`:54003`) |
| `youtube_player_cc.py` / `youtube_eye` | Playwright: scrape live player overlay |
| `youtube_live_cc.py` / `youtube_cc` | yt-dlp WebVTT path (can diverge from eyes) |
| `live_type.py` | Interactive keyboard captions |
| `caption_http.py` | HTTP POST → UDP |
| `send_random_captions.sh` | Load test |
| `AGENTS.md` | This file |
| `README.md` | User-facing build/run/config guide |

Ignore: `.venv/`, `__pycache__/`, `*.bak`, built `cc_injector`.

---

## 4. Data flow (end-to-end)

```
Caption producer                Injector                         Viewer
─────────────────               ────────                         ──────
youtube_eye / demo / nc
      │
      │  UDP ASCII  (host:54001)
      │  "line1|line2" or "CLEAR"
      ▼
cc_injector  ◄──── MPEG-TS in (H.264)
      │
      │  build 608 (+ optional 708)
      │  queue → drain ≤31 triplets/SEI
      │  packet_with_a53_sei()
      ▼
MPEG-TS out  ──────────────────►  VLC CC1 / STB
```

Web monitor (`demo.start_web`) shows the **exact UDP payload** (`display` field), not a re-interpreted string. Keep web ≡ injector text.

---

## 5. `cc_injector` internals (agent-critical)

### 5.1 Caption window paint (`build_ru_live_tv`)

- Bottom-aligns up to `st.lines` (`--rows`) under `--row` (base PAC).
- Paints **only changed rows**; skips empty→empty to save triplets.
- Each row: PAC → optional midrow style → text → **DER** (`0x14 0x24`).
- Do **not** space-pad to 32.

### 5.2 SEI queue drain (`cc_tx_queue`)

- A full 2-line paint often needs **>31** triplets → split across video packets.
- **Never** replace the queue with an **empty** rebuild (wipes undrained tail → VLC shows `offer` instead of `offering`).
- **Never** treat “same caption again” as a new inject while the previous queue is still draining.
- New **different** text may replace the queue; CLEAR always replaces.

### 5.3 CLEAR

- `build_cc_clear(base_row, nwin)`: EDM ×N + ENM + **PAC+DER per window row** + final EDM.
- Repeat for `clear_frames` packets (default/config **6**; VLC needs more than 2).
- **Do not** set `clear_frames_left = 0` when new text arrives mid-clear — finish erase, then paint (`caption_pending` waits).

### 5.4 Modes

| Mode | Flag / trigger | Behaviour |
|------|----------------|-----------|
| Roll-up | default | CR rolls lines |
| Live multi-line | UDP contains `\|` | Overwrite whole window |
| Live edit | prefix/backspace same line | Overwrite without duplicate RU rows |
| `--live` | CLI | Force overwrite; often `timeout_ms=0` |
| `--cc=608\|708\|both` | CLI/config | Which standards in SEI |

### 5.5 708

- Font/pen only apply when `cc` includes 708.
- Prefer `--font=0` and `--pen_size=standard` for player compatibility.
- 608 remains the universal fallback (VLC **CC1**).

---

## 6. Configuration contract

### Loader

- `cc_config.load_config(path)` unwraps `{ "value": X, "_description": "…" }` nodes.
- Keys starting with `_` are stripped.
- CLI and env override JSON (`start_injector.sh` uses env from `cc_config.py --env`).

### Sections

1. **`stream`** — `input`, `output`, `caption_udp`
2. **`captions`** — rows, row, width, timeout_ms, color, cc, font, pen_size, bootstrap, sei_every, clear_frames
3. **`demo`** — sources, timing, uppercase, web send target
4. **`web`** — monitor bind/port
5. **`youtube`** — url, lang, polls, exact, single_upper, delays

When changing defaults, update **both** `config.json` descriptions and `README.md` tables.

### Local network

Example multicast uses `localaddr=192.168.1.20` — agents must treat this as **site-specific**. Ask or detect NIC before assuming.

---

## 7. YouTube eye (`youtube_player_cc.py`)

**Purpose:** Match what a human sees on youtube.com CC overlay.

| Piece | Detail |
|-------|--------|
| Browser | Playwright Chromium, muted autoplay |
| DOM | `.ytp-caption-segment` |
| Live sync | Click LIVE badge when not `disabled` |
| Paint | Keep YouTube line breaks; wrap only if line &gt; width; max `rows` |
| Queue | Every stable overlay change enqueued (no skip) |
| On-air | **CLEAR** → short gap → next painted window |
| Debounce | 2 identical polls before enqueue (avoids half-updated overlay) |
| Web | Same UDP string as injector |

**Do not** re-join all overlay lines into one reflowed paragraph unless the user explicitly asks — that desyncs from YouTube line breaks.

**Prefer** `youtube_eye` over `youtube_cc` (VTT) when the user says “as eyes see”.

---

## 8. UDP protocol (producers must match)

```
text\n                 → one line (wrap at width)
line1|line2\n          → explicit multi-line window
CLEAR / CLS / empty    → erase
[yellow+u]hi           → inline style for that message
```

Producers: `demo.py`, `live_type.py`, `caption_http.py`, `youtube_player_cc.emit_painted`, `nc -u`.

---

## 9. Build & run (agent checklist)

```bash
# Deps
sudo apt install -y g++ pkg-config \
  libavformat-dev libavcodec-dev libavutil-dev libswresample-dev ffmpeg

# Build
g++ -std=c++17 -O2 cc_injector.cpp \
  $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) \
  -o cc_injector

# Start from config
./start_injector.sh ./config.json

# YouTube eye (venv + playwright required)
./youtube_eye --config ./config.json --url 'https://www.youtube.com/watch?v=ID' --poll 0.15 --rows=3
```

After editing `cc_injector.cpp`, **always rebuild** before claiming a fix is live.

After editing `youtube_player_cc.py` / `demo.py` HTML, **restart** the Python process (PAGE_HTML is import-time).

---

## 10. Testing recipes

### Smoke (loopback)

1. `ffmpeg` SMPTE bars → `udp://127.0.0.1:5000`
2. `cc_injector` 5000 → 5004, `--cc-udp=54001`
3. `printf 'TEST ONE|TEST TWO\n' | nc -u -w0 127.0.0.1 54001`
4. VLC `udp://@127.0.0.1:5004` → enable CC1
5. `printf 'CLEAR\n' | nc -u -w0 127.0.0.1 54001` — screen must go blank (no leftover glyphs)

### Regression cases agents must not break

| Case | Expected |
|------|----------|
| Long 2-line caption | Full words on VLC (queue drains to `remain=0`) |
| CLEAR then quick next line | No leftover chars from previous |
| Duplicate same UDP while draining | Do not wipe queue |
| Web monitor | Matches UDP `display` exactly; no empty leading `\|` pad |
| `rows=3` but only 2 lines of text | No forced blank third line in UDP payload |

---

## 11. Common failure modes → fixes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| VLC missing last syllables | Queue cleared mid-drain | Keep undrained queue; skip identical re-inject |
| Ghost chars after CLEAR | `clear_frames` too low / clear aborted | PAC+DER clear; finish clear_frames; ≥6 |
| No captions | Wrong VLC track / wrong URL | CC1 on **output** multicast |
| Font unchanged | `cc=608` only | `--cc=both --font=0` |
| Web ahead/behind YouTube | DVR lag / wrong source | LIVE badge snap; use eye not VTT |
| Flicker | EDM every update / low sei_every spam | Paint diffs only; moderate `sei_every` |

---

## 12. Coding guidelines for agents

1. **Prefer small, surgical diffs** in `cc_injector.cpp` and the active Python producer.
2. **Do not** commit `.venv`, `__pycache__`, binaries, or secrets.
3. **Do not** force-push `main` unless the user explicitly demands it.
4. When the user asks for “line by line”, preserve overlay breaks and queue cues; CLEAR between cues if they ask to clear previous.
5. When moving captions “up N lines”, change `captions.row` by **−N** (and restart injector).
6. Document user-facing behaviour in `README.md`; keep agent pitfalls in this file.
7. Rebuild + restart + verify with `pgrep` / log `recv` / `curl :54003/api/last` before declaring done.

---

## 13. Release 1.0 scope

Included:

- CEA-608 + CEA-708 inject into live MPEG-TS
- JSON configuration with inline documentation
- Demo + web monitor
- YouTube player-eye ingest with queue + CLEAR
- Strong CLEAR and SEI queue drain fixes
- Compatible default 708 font (`0`) + `standard` pen

Out of scope for 1.0 (unless later requested): Windows build, non-ASCII 608 extended sets, DVB subtitle PIDs, cloud SaaS control plane.

---

## 14. Quick command card

```bash
# Build
g++ -std=c++17 -O2 cc_injector.cpp $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) -o cc_injector

# Injector from config
./start_injector.sh ./config.json

# Manual caption
printf 'Hello|World\n' | nc -u -w0 127.0.0.1 54001
printf 'CLEAR\n' | nc -u -w0 127.0.0.1 54001

# YouTube eye
./youtube_eye --config ./config.json --url 'URL' --poll 0.15 --rows=3

# Config dump
python3 ./cc_config.py ./config.json --env
```

When in doubt: **read `config.json` `_description` fields** — they are the source of truth for option meaning.

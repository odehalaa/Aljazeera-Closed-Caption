#!/usr/bin/env python3
"""Load CC_608 config.json (skips '_' documentation keys)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / "config.json"


def _unwrap(node: Any) -> Any:
    """Turn {value: X, _description: ...} into X; drop _keys; recurse."""
    if isinstance(node, dict):
        if "value" in node and all(k == "value" or str(k).startswith("_") for k in node):
            return _unwrap(node["value"])
        out = {}
        for k, v in node.items():
            if str(k).startswith("_"):
                continue
            out[k] = _unwrap(v)
        return out
    if isinstance(node, list):
        return [_unwrap(x) for x in node]
    return node


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """Clamp unsafe values in-place. Returns warning messages (never raises)."""
    warnings: list[str] = []
    cap = cfg.setdefault("captions", {})
    demo = cfg.setdefault("demo", {})
    web = cfg.setdefault("web", {})
    stream = cfg.setdefault("stream", {})

    rows = _clamp_int(cap.get("rows", 2), 2, 4, 2)
    if rows != cap.get("rows"):
        warnings.append(f"captions.rows clamped to {rows} (allowed 2..4)")
    cap["rows"] = rows

    row = _clamp_int(cap.get("row", 15), 1, 15, 15)
    if row != cap.get("row"):
        warnings.append(f"captions.row clamped to {row} (allowed 1..15)")
    cap["row"] = row

    width = _clamp_int(cap.get("width", 32), 8, 32, 32)
    if width != cap.get("width"):
        warnings.append(f"captions.width clamped to {width} (allowed 8..32)")
    cap["width"] = width

    try:
        timeout = int(cap.get("timeout_ms", 6000))
    except (TypeError, ValueError):
        timeout = 6000
        warnings.append("captions.timeout_ms invalid — using 6000")
    if timeout < 0:
        timeout = 0
        warnings.append("captions.timeout_ms < 0 — using 0")
    cap["timeout_ms"] = timeout

    colors = {"white", "green", "blue", "cyan", "red", "yellow", "magenta"}
    color = str(cap.get("color", "yellow")).lower()
    if color not in colors:
        warnings.append(f"captions.color '{color}' invalid — using yellow")
        color = "yellow"
    cap["color"] = color

    cc = str(cap.get("cc", "both")).lower()
    if cc not in {"608", "708", "both"}:
        warnings.append(f"captions.cc '{cc}' invalid — using both")
        cc = "both"
    cap["cc"] = cc

    font = _clamp_int(cap.get("font", 4), 0, 7, 4)
    if font != cap.get("font"):
        warnings.append(f"captions.font clamped to {font} (allowed 0..7)")
    cap["font"] = font

    pen = str(cap.get("pen_size", "large")).lower()
    if pen not in {"small", "standard", "large"}:
        warnings.append(f"captions.pen_size '{pen}' invalid — using large")
        pen = "large"
    cap["pen_size"] = pen

    cap["bootstrap"] = 1 if int(cap.get("bootstrap", 0) or 0) else 0
    cap["sei_every"] = _clamp_int(cap.get("sei_every", 15), 1, 30, 15)
    cap["clear_frames"] = _clamp_int(cap.get("clear_frames", 2), 1, 30, 2)

    if not stream.get("input"):
        warnings.append("stream.input is empty")
    if not stream.get("output"):
        warnings.append("stream.output is empty")
    if not stream.get("caption_udp"):
        stream["caption_udp"] = "0.0.0.0:54001"

    try:
        demo["every"] = max(0.05, float(demo.get("every", 1.0)))
    except (TypeError, ValueError):
        demo["every"] = 1.0
        warnings.append("demo.every invalid — using 1.0")
    try:
        demo["cps"] = max(0.0, float(demo.get("cps", 0.0)))
    except (TypeError, ValueError):
        demo["cps"] = 0.0
    demo["limit"] = _clamp_int(demo.get("limit", 40), 1, 500, 40)
    try:
        demo["refresh"] = max(1.0, float(demo.get("refresh", 60.0)))
    except (TypeError, ValueError):
        demo["refresh"] = 60.0
    if not isinstance(demo.get("sources"), list) or not demo.get("sources"):
        warnings.append("demo.sources missing — keep defaults in demo.py")

    web["port"] = _clamp_int(web.get("port", 54003), 0, 65535, 54003)
    web["host"] = str(web.get("host") or "0.0.0.0")
    if "enabled" not in web:
        web["enabled"] = True

    return warnings


def load_config(path: str | Path | None = None, *, validate: bool = True) -> dict[str, Any]:
    p = Path(path) if path else Path(os.environ.get("CC_CONFIG", DEFAULT_PATH))
    if not p.is_file():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = _unwrap(raw)
    if not isinstance(cfg, dict):
        raise ValueError("config root must be an object")
    if validate:
        for w in validate_config(cfg):
            print(f"[config] {w}", file=sys.stderr)
    return cfg


def get_path(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def export_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Flat env map for start_injector.sh / demo wrapper."""
    cap = cfg.get("captions") or {}
    stream = cfg.get("stream") or {}
    demo = cfg.get("demo") or {}
    web = cfg.get("web") or {}
    return {
        "IN": str(stream.get("input", "")),
        "OUT": str(stream.get("output", "")),
        "CC_UDP": str(stream.get("caption_udp", "0.0.0.0:54001")),
        "ROWS": str(cap.get("rows", 2)),
        "ROW": str(cap.get("row", 15)),
        "WIDTH": str(cap.get("width", 32)),
        "TIMEOUT_MS": str(cap.get("timeout_ms", 6000)),
        "COLOR": str(cap.get("color", "yellow")),
        "FONT": str(cap.get("font", 4)),
        "PEN_SIZE": str(cap.get("pen_size", "large")),
        "CC": str(cap.get("cc", "both")),
        "BOOTSTRAP": str(cap.get("bootstrap", 0)),
        "SEI_EVERY": str(cap.get("sei_every", 15)),
        "CLEAR_FRAMES": str(cap.get("clear_frames", 2)),
        "DEMO_HOST": str(demo.get("host", "127.0.0.1")),
        "DEMO_PORT": str(demo.get("port", 54001)),
        "DEMO_EVERY": str(demo.get("every", 1)),
        "DEMO_WEB_PORT": str(web.get("port", 54003)),
        "APPLY_INJECTOR": "1" if demo.get("apply_injector", True) else "0",
    }


def main() -> int:
    """CLI: cc_config.py [config.json] [--env [KEY] | dotted.key]"""
    path = DEFAULT_PATH
    args = sys.argv[1:]
    if args and not args[0].startswith("-") and args[0].endswith(".json"):
        path = Path(args[0])
        args = args[1:]
    cfg = load_config(path)
    if not args:
        json.dump(cfg, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if args[0] == "--env":
        env = export_env(cfg)
        if len(args) == 1:
            for k, v in env.items():
                print(f"{k}={v}")
            return 0
        key = args[1]
        if key not in env:
            print(f"unknown env key: {key}", file=sys.stderr)
            return 1
        print(env[key])
        return 0
    val = get_path(cfg, args[0])
    if isinstance(val, (dict, list)):
        json.dump(val, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif val is None:
        return 1
    else:
        print(val)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

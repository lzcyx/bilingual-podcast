#!/usr/bin/env python3
"""Strict checks for model JSON. Invalid pieces are dropped, not trusted."""
from __future__ import annotations

import re

PALETTE = ["#7eb8ff", "#ffb86b", "#7ddea2", "#ff8fab", "#c3a6ff", "#ffe08a", "#9ad1d4", "#f0a3ff"]
AD_NAMES = {"ad", "ads", "advert", "advertisement", "commercial", "sponsor", "sponsorship", "广告", "广告声"}
SPK_RE = re.compile(r"^S\d+$")


def validate_fixes(obj) -> tuple[list, list[str]]:
    warns = []
    if obj is None:
        return [], warns
    if not isinstance(obj, list):
        return [], ["fixes is not a list; ignored"]
    out = []
    for i, pair in enumerate(obj):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            warns.append(f"fixes[{i}] is not a pair")
            continue
        pat, repl = pair
        if not isinstance(pat, str) or not isinstance(repl, str) or not pat:
            warns.append(f"fixes[{i}] needs two strings")
            continue
        try:
            re.compile(pat)
        except re.error as e:
            warns.append(f"fixes[{i}] bad regex: {e}")
            continue
        out.append([pat, repl])
    return out, warns


def _word_count(line: dict) -> int:
    w = line.get("w")
    if isinstance(w, list) and w:
        return len(w)
    return len((line.get("en") or "").split())


def _split_ok(parts, nwords: int) -> bool:
    if not isinstance(parts, list) or not parts:
        return False
    pos = 0
    for part in parts:
        if isinstance(part, dict):
            n = int(part.get("n", part.get("words", 0)) or 0)
            txt = part.get("en", part.get("text", ""))
        elif isinstance(part, (list, tuple)) and len(part) >= 2:
            n = int(part[0] or 0)
            txt = part[1]
        else:
            return False
        if not isinstance(txt, str) or not txt.strip():
            return False
        take = n if n > 0 else nwords - pos
        if take <= 0:
            return False
        pos += take
    return pos == nwords


def validate_edits(obj, raw_by_id: dict) -> tuple[dict, list[str]]:
    warns = []
    if obj is None:
        return {}, warns
    if not isinstance(obj, dict):
        return {}, ["edits is not an object; ignored"]
    out = {}
    for key, val in obj.items():
        kid = str(key)
        if kid not in raw_by_id:
            warns.append(f"edit {kid}: unknown line id")
            continue
        if not isinstance(val, dict):
            warns.append(f"edit {kid}: not an object")
            continue
        if val.get("drop") is True:
            out[kid] = {"drop": True}
            continue
        if "split" in val:
            if _split_ok(val["split"], _word_count(raw_by_id[kid])):
                out[kid] = {"split": val["split"]}
            else:
                warns.append(f"edit {kid}: split does not cover the line; dropped")
            continue
        row = {}
        if val.get("merge_prev") is True:
            row["merge_prev"] = True
            if isinstance(val.get("en"), str) and val["en"].strip():
                row["en"] = val["en"].strip()
        elif isinstance(val.get("en"), str) and val["en"].strip():
            row["en"] = val["en"].strip()
        spk = val.get("spk")
        if isinstance(spk, str) and SPK_RE.match(spk):
            row["spk"] = spk
        if not row:
            warns.append(f"edit {kid}: nothing usable")
            continue
        out[kid] = row
    return out, warns


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    if name.lower() in AD_NAMES or name in AD_NAMES:
        return "Ad"
    return name


def validate_speakers(obj) -> tuple[dict, list[str]]:
    warns = []
    if obj is None:
        return {}, warns
    if not isinstance(obj, dict):
        return {}, ["speakers is not an object; ignored"]
    out = {}
    for key, val in obj.items():
        kid = str(key)
        if not SPK_RE.match(kid):
            warns.append(f"speaker {kid}: id must look like S0")
            continue
        if isinstance(val, str):
            name, color = val, None
        elif isinstance(val, dict):
            name, color = val.get("name") or "", val.get("color")
        else:
            warns.append(f"speaker {kid}: bad value")
            continue
        if not isinstance(name, str) or not _clean_name(name):
            warns.append(f"speaker {kid}: missing name")
            continue
        row = {"name": _clean_name(name)}
        if isinstance(color, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", color.strip()):
            c = color.strip()
            row["color"] = c if c.startswith("#") else "#" + c
        out[kid] = row
    return out, warns


def paint_speakers(speakers: dict, used_ids: list[str]) -> dict:
    out = dict(speakers)
    for sid in used_ids:
        out.setdefault(sid, {"name": sid})
    # Stable palette, Ad stays grey.
    n = 0
    for sid in sorted(out, key=lambda s: (len(s), s)):
        row = out[sid]
        if row.get("name") == "Ad":
            row.setdefault("color", "#b0b6c0")
            continue
        if "color" not in row:
            row["color"] = PALETTE[n % len(PALETTE)]
            n += 1
        else:
            n += 1
    return out


def validate_chapters(obj, n_lines: int) -> tuple[list, list[str]]:
    warns = []
    rows = obj.get("chapters") if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or not rows:
        raise ValueError("chapters must be a non-empty list")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            warns.append(f"chapter {i} ignored")
            continue
        try:
            idx = int(row.get("i"))
        except (TypeError, ValueError):
            warns.append(f"chapter {i} has no i")
            continue
        tr = (row.get("tr") or row.get("zh") or "").strip()
        en = (row.get("en") or "").strip()
        if not tr or not en:
            warns.append(f"chapter {i} needs tr and en")
            continue
        if not 0 <= idx < n_lines:
            warns.append(f"chapter i={idx} out of range")
            continue
        out.append({"i": idx, "tr": tr[:80], "en": en[:80]})
    out.sort(key=lambda c: c["i"])
    dedup = []
    for c in out:
        if dedup and c["i"] <= dedup[-1]["i"]:
            continue
        dedup.append(c)
    if not dedup or dedup[0]["i"] != 0:
        raise ValueError("first chapter must start at i=0")
    return dedup, warns


def fallback_chapters(lines: list, count: int) -> list:
    if not lines:
        return [{"i": 0, "tr": "开场", "en": "Start"}]
    count = max(1, min(count, len(lines)))
    duration = float(lines[-1].get("e") or 0) or 1.0
    out = []
    for k in range(count):
        t = 0.0 if k == 0 else duration * k / count
        if k == 0:
            idx = 0
        else:
            idx = next((c["i"] for c in lines if float(c["s"]) >= t - 0.05), lines[-1]["i"])
        if out and idx <= out[-1]["i"]:
            idx = out[-1]["i"] + 1
        if idx >= len(lines):
            break
        out.append({"i": idx, "tr": f"章节 {k + 1}", "en": f"Chapter {k + 1}"})
    if not out or out[0]["i"] != 0:
        out.insert(0, {"i": 0, "tr": "开场", "en": "Intro"})
    return out


def chapter_bounds(duration_sec: float) -> tuple[int, int]:
    hours = max(float(duration_sec or 0), 60.0) / 3600.0
    lo = max(4, round(8 * hours))
    hi = max(lo, round(20 * hours))
    return lo, min(hi, 30)

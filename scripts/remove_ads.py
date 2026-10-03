#!/usr/bin/env python3
"""Remove confidently identified ad-speaker ranges from offline DAI episodes.

Runs after proofreading/apply_edits, when speakers.json may name an advertising
voice exactly "Ad". The original episode.mp3 and pre-removal line/speaker files
are preserved. On success:
  - writes episode_clean.mp3
  - removes Ad lines from lines.json and compresses the remaining timestamps
  - removes Ad speaker entries from speakers.json
  - writes ad_ranges.json / ad_removed_lines.json / ad_removal_report.json

Only dynamic_ads=true + audio_mode=offline episodes are eligible. Host-read ads
are intentionally kept unless they have their own speaker named exactly "Ad".
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ad_speaker_ids(speakers: dict) -> set[str]:
    out = set()
    for sid, value in (speakers or {}).items():
        name = value.get("name") if isinstance(value, dict) else value
        if str(name or "").strip() == "Ad":
            out.add(str(sid))
    return out


def find_ad_ranges(lines: list[dict], ad_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Return consecutive Ad-speaker ranges and the removed lines."""
    ranges = []
    removed = []
    cur = None
    for line in lines:
        is_ad = str(line.get("spk") or "") in ad_ids
        if is_ad:
            removed.append(dict(line))
            if cur is None:
                cur = {
                    "s": float(line["s"]),
                    "e": float(line["e"]),
                    "speaker_ids": [str(line.get("spk") or "")],
                    "line_ids": [str(line.get("id") or line.get("i") or "")],
                }
            else:
                cur["e"] = max(cur["e"], float(line["e"]))
                sid = str(line.get("spk") or "")
                if sid and sid not in cur["speaker_ids"]:
                    cur["speaker_ids"].append(sid)
                cur["line_ids"].append(str(line.get("id") or line.get("i") or ""))
        elif cur is not None:
            ranges.append(cur)
            cur = None
    if cur is not None:
        ranges.append(cur)

    for r in ranges:
        r["s"] = round(r["s"], 3)
        r["e"] = round(r["e"], 3)
        r["duration"] = round(r["e"] - r["s"], 3)
    return ranges, removed


def removed_before(t: float, ranges: list[dict]) -> float:
    total = 0.0
    for r in ranges:
        s, e = float(r["s"]), float(r["e"])
        if t >= e:
            total += e - s
        elif t > s:
            total += t - s
            break
        else:
            break
    return total


def remap_lines(lines: list[dict], ranges: list[dict], ad_ids: set[str]) -> list[dict]:
    out = []
    for line in lines:
        if str(line.get("spk") or "") in ad_ids:
            continue
        row = dict(line)
        s, e = float(row["s"]), float(row["e"])
        row["s"] = round(max(0.0, s - removed_before(s, ranges)), 2)
        row["e"] = round(max(row["s"], e - removed_before(e, ranges)), 2)
        row["i"] = len(out)
        out.append(row)
    for a, b in zip(out, out[1:]):
        if a["e"] > b["s"]:
            a["e"] = b["s"]
    return out


def ffprobe_duration(path: str) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    ).strip()
    return float(out)


def keep_ranges(duration: float, ads: list[dict]) -> list[tuple[float, float]]:
    keep = []
    pos = 0.0
    for r in ads:
        s = max(pos, min(duration, float(r["s"])))
        e = max(s, min(duration, float(r["e"])))
        if s - pos > 0.01:
            keep.append((pos, s))
        pos = e
    if duration - pos > 0.01:
        keep.append((pos, duration))
    return keep


def cut_audio(src: str, dst: str, ads: list[dict], duration: float):
    keep = keep_ranges(duration, ads)
    if not keep:
        raise RuntimeError("ad ranges would remove the entire audio file")
    filters = []
    labels = []
    for i, (s, e) in enumerate(keep):
        label = f"a{i}"
        filters.append(
            f"[0:a:0]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filters.append(f"{labels[0]}anull[outa]")
    else:
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[outa]")
    tmp = dst + ".tmp.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", src,
            "-filter_complex", ";".join(filters),
            "-map", "[outa]", "-vn", "-map_metadata", "-1",
            "-codec:a", "libmp3lame", "-q:a", "3", tmp,
        ],
        check=True,
    )
    os.replace(tmp, dst)


def write_lines_tsv(path: str, lines: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        for c in lines:
            spk = c.get("spk") or ""
            f.write(
                f"{c['i']}\t{c.get('id', '')}\t"
                f"{int(c['s'] // 60)}:{c['s'] % 60:05.2f}\t{spk}\t{c.get('en', '')}\n"
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    a = ap.parse_args()
    wd = a.workdir

    meta = load_json(os.path.join(wd, "meta.json"), {}) or {}
    draft = load_json(os.path.join(wd, "config.draft.json"), {}) or {}
    report_path = os.path.join(wd, "ad_removal_report.json")
    report = {
        "eligible": bool(meta.get("dynamic_ads")) and draft.get("audio_mode") == "offline",
        "applied": False,
        "segments": 0,
        "removed_lines": 0,
        "removed_seconds": 0.0,
        "reason": "",
    }

    if not report["eligible"]:
        report["reason"] = "not an offline dynamic-ads episode"
        dump_json(report_path, report)
        print("remove_ads: skipped (not offline dynamic ads)")
        return

    speakers_path = os.path.join(wd, "speakers.json")
    lines_path = os.path.join(wd, "lines.json")
    speakers = load_json(speakers_path, {}) or {}
    lines = load_json(lines_path, []) or []
    ad_ids = ad_speaker_ids(speakers)
    if not ad_ids:
        report["reason"] = 'no speaker named exactly "Ad"'
        dump_json(report_path, report)
        print('remove_ads: no speaker named exactly "Ad"; keeping audio unchanged')
        return

    ads, removed = find_ad_ranges(lines, ad_ids)
    if not ads:
        report["reason"] = "Ad speaker has no final lines"
        dump_json(report_path, report)
        print("remove_ads: Ad speaker has no final lines; keeping audio unchanged")
        return

    src = os.path.join(wd, "episode.mp3")
    if not os.path.exists(src):
        raise SystemExit(f"remove_ads: missing source audio {src}")
    duration = ffprobe_duration(src)
    total_removed = sum(float(r["duration"]) for r in ads)
    if total_removed <= 0.05:
        report["reason"] = "identified ad ranges are empty"
        dump_json(report_path, report)
        print("remove_ads: identified ranges are empty; keeping audio unchanged")
        return
    if total_removed >= duration - 1:
        raise SystemExit("remove_ads: refusing to remove nearly the entire episode")

    clean = os.path.join(wd, "episode_clean.mp3")
    cut_audio(src, clean, ads, duration)
    clean_duration = ffprobe_duration(clean)
    remapped = remap_lines(lines, ads, ad_ids)

    shutil.copy2(lines_path, os.path.join(wd, "lines_with_ads.json"))
    shutil.copy2(speakers_path, os.path.join(wd, "speakers_with_ads.json"))
    dump_json(lines_path, remapped)
    write_lines_tsv(os.path.join(wd, "lines.tsv"), remapped)

    cleaned_speakers = {k: v for k, v in speakers.items() if str(k) not in ad_ids}
    dump_json(speakers_path, cleaned_speakers)
    dump_json(os.path.join(wd, "ad_ranges.json"), ads)
    dump_json(os.path.join(wd, "ad_removed_lines.json"), removed)

    report.update({
        "applied": True,
        "segments": len(ads),
        "removed_lines": len(removed),
        "removed_seconds": round(total_removed, 3),
        "original_duration": round(duration, 3),
        "clean_duration": round(clean_duration, 3),
        "audio_file": os.path.abspath(clean),
        "ad_speaker_ids": sorted(ad_ids),
        "reason": "removed exact Ad-speaker ranges",
    })
    dump_json(report_path, report)
    print(
        f"remove_ads: removed {len(ads)} segment(s), {len(removed)} line(s), "
        f"{total_removed:.1f}s; {duration:.1f}s -> {clean_duration:.1f}s"
    )


if __name__ == "__main__":
    main()

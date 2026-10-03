#!/usr/bin/env python3
"""Remove only a leading pre-roll Ad-speaker block from offline DAI episodes.

Runs after proofreading/apply_edits, when speakers.json may name an advertising
voice exactly "Ad". The original episode.mp3 and pre-removal line/speaker files
are preserved. On success:
  - writes episode_clean.mp3
  - removes Ad lines from lines.json and compresses the remaining timestamps
  - keeps later Ad speakers/segments untouched
  - writes ad_ranges.json / ad_removed_lines.json / ad_removal_report.json

Only dynamic_ads=true + audio_mode=offline episodes are eligible. Removal happens
only when the first spoken subtitle line is from a speaker named exactly "Ad".
Once program speech begins, all later ads are intentionally kept.
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


def find_leading_ad_range(lines: list[dict], ad_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Return only the pre-roll Ad block, and only when the first spoken line is Ad.

    Once any non-Ad program speech begins, later Ad speakers are never removed.
    """
    if not lines or str(lines[0].get("spk") or "") not in ad_ids:
        return [], []

    removed = []
    start = float(lines[0]["s"])
    end = start
    speaker_ids = []
    line_ids = []
    for line in lines:
        sid = str(line.get("spk") or "")
        if sid not in ad_ids:
            break
        removed.append(dict(line))
        end = max(end, float(line["e"]))
        if sid and sid not in speaker_ids:
            speaker_ids.append(sid)
        line_ids.append(str(line.get("id") or line.get("i") or ""))

    r = {
        "s": round(start, 3),
        "e": round(end, 3),
        "duration": round(end - start, 3),
        "speaker_ids": speaker_ids,
        "line_ids": line_ids,
    }
    return [r], removed


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


def remap_lines(lines: list[dict], ranges: list[dict]) -> list[dict]:
    out = []
    for line in lines:
        s0, e0 = float(line["s"]), float(line["e"])
        remove = any(s0 >= float(r["s"]) - 1e-6 and e0 <= float(r["e"]) + 1e-6 for r in ranges)
        if remove:
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

    ads, removed = find_leading_ad_range(lines, ad_ids)
    if not ads:
        report["reason"] = 'first spoken line is not "Ad"'
        dump_json(report_path, report)
        print('remove_ads: first spoken line is not "Ad"; keeping all ads unchanged')
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
    remapped = remap_lines(lines, ads)

    shutil.copy2(lines_path, os.path.join(wd, "lines_with_ads.json"))
    shutil.copy2(speakers_path, os.path.join(wd, "speakers_with_ads.json"))
    dump_json(lines_path, remapped)
    write_lines_tsv(os.path.join(wd, "lines.tsv"), remapped)

    # Keep Ad speaker metadata because later mid-roll Ad segments remain in the episode.
    dump_json(speakers_path, speakers)
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
        "reason": "removed leading pre-roll Ad block only",
    })
    dump_json(report_path, report)
    print(
        f"remove_ads: removed {len(ads)} segment(s), {len(removed)} line(s), "
        f"{total_removed:.1f}s; {duration:.1f}s -> {clean_duration:.1f}s"
    )


if __name__ == "__main__":
    main()

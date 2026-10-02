#!/usr/bin/env python3
"""Write chapters.json (8–20 chapters per hour) and accept it only if build.py --check passes."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import Client, InfraError, LLMError, parse_json_content  # noqa: E402
from schema import chapter_bounds, fallback_chapters, validate_chapters  # noqa: E402

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEM = """You split one podcast episode into chapters for a bilingual player.
Return JSON: {{"chapters": [{{"i": 0, "tr": "开场：……", "en": "Intro: ..."}}]}}
Rules:
- "i" is the index of the FIRST subtitle line of that chapter (the number in the first column). The first chapter MUST be i=0.
- Indexes strictly increase and stay inside the episode.
- Count: between {lo} and {hi} chapters. Each is usually 1–6 minutes. Cover the whole episode, including ads only as their own short chapter if they are obvious.
- Titles are short and specific (topic, game, or person). Simplified Chinese in "tr", English in "en".
- Official Chinese game/film titles in 《》. Host names stay in English.
"""


def _condensed(lines):
    rows = []
    for c in lines:
        text = (c.get("en") or "").replace("\t", " ")[:90]
        m, s = divmod(float(c["s"]), 60)
        rows.append(f"{c['i']}\t{int(m)}:{s:04.1f}\t{c.get('spk') or ''}\t{text}")
    return "\n".join(rows)


def _check(config) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "build.py"), config, "--check"],
        capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    wd = a.workdir
    lines = json.load(open(os.path.join(wd, "lines.json"), encoding="utf-8"))
    if not lines:
        sys.exit("lines.json is empty")
    duration = float(lines[-1]["e"])
    lo, hi = chapter_bounds(duration)
    target = max(lo, min(hi, max(lo, round(duration / 300))))
    body = _condensed(lines)
    # Very long episodes: keep every line's index and time, but the text is already clipped.
    client = Client(usage_path=os.path.join(wd, "usage.jsonl"))
    last = ""
    chosen = None
    try:
        system = SYSTEM.format(lo=lo, hi=hi)
    except Exception as e:
        # A prompt bug must not throw away a finished transcription.
        print(f"chapters: prompt error: {e}", flush=True)
        system = ""
    if system:
        for attempt in range(1, 4):
            user = f"Episode length: {duration / 60:.1f} min. Aim for {lo}–{hi} chapters (around {target}).\n\nindex, time, speaker, text\n{body}"
            if last:
                user += f"\n\nPrevious chapters failed validation:\n{last[:2500]}\nReturn a corrected full list."
            try:
                text = client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    step="chapters", temperature=0.3, json_mode=True, max_tokens=4096,
                    extra={"attempt": attempt})
                chapters, warns = validate_chapters(parse_json_content(text), len(lines))
                for w in warns[:15]:
                    print("  warn:", w, flush=True)
                if not (lo <= len(chapters) <= hi) and not (len(lines) < lo):
                    last = f"got {len(chapters)} chapters, need {lo}–{hi}"
                    print(f"  chapters attempt {attempt}: {last}", flush=True)
                    continue
                path = os.path.join(wd, "chapters.json")
                json.dump(chapters, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                rc, log = _check(a.config)
                print(log, flush=True)
                if rc == 0:
                    chosen = chapters
                    break
                last = log
            except InfraError:
                raise
            except (LLMError, ValueError, Exception) as e:
                last = str(e)
                print(f"  chapters attempt {attempt}: {e}", flush=True)
    if chosen is None:
        print("chapters: model output did not pass --check; using even fallback chapters", flush=True)
        chosen = fallback_chapters(lines, target)
        json.dump(chosen, open(os.path.join(wd, "chapters.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        rc, log = _check(a.config)
        print(log, flush=True)
        if rc != 0:
            raise SystemExit(f"build.py --check failed even with fallback chapters\n{log}")
        open(os.path.join(wd, "chapters_fallback.txt"), "w", encoding="utf-8").write(last[:4000])
    print(f"chapters: {len(chosen)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InfraError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(2)

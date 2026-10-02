#!/usr/bin/env python3
"""Translate tr/bNN.src.txt blocks. Each block is checked with tr_check.py --only (max 3 tries)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import Client, InfraError, LLMError, parse_json_content  # noqa: E402

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import tr_check  # noqa: E402


SYSTEM = """You translate one block of an English podcast into spoken simplified Chinese.
The user message contains source groups. A key is a line index (17) or a range (12-14).
The source text joins lines of one sentence with " | ".
Return JSON only: {"groups": [{"key": "12-14", "parts": ["……", "……", "……"]}]}
Rules:
- Every source key appears exactly once. A group of n lines has exactly n parts, in order. No part may be empty.
- Put the Chinese that lines up with each English piece in that part. You may move a word across the break so each part still matches what is said there, but do not drop or summarise.
- Spoken, natural 简体中文. Keep fillers light (嗯、对、就是).
- Official Chinese titles of games, films, shows and products go in 《》. If there is no official title, keep the original inside 《》.
- Host and guest names stay in English. Brands (PlayStation, DualSense, PS5, Xbox, Nintendo, Steam) stay as-is.
- Follow the glossary exactly when it applies. Do not use the half-width character | inside a part.
- Read # speaker and # context lines first; they are not keys.
"""


def _marker(src_map: dict) -> str:
    lines = []
    for key in src_map:
        a, b = (key.split("-") + [key])[:2]
        n = int(b) - int(a) + 1
        lines.append(key + "\t" + "｜".join(["［未译］"] * n))
    return "\n".join(lines) + "\n"


def _render(obj, src_map: dict) -> str:
    if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
        raise LLMError("response needs {\"groups\": [{\"key\", \"parts\"}]}")
    got = {}
    for g in obj["groups"]:
        if not isinstance(g, dict):
            continue
        key = str(g.get("key") or "").strip()
        parts = g.get("parts")
        if key:
            got[key] = parts
    errs = []
    missing = [k for k in src_map if k not in got]
    extra = [k for k in got if k not in src_map]
    if missing:
        errs.append("missing keys " + ", ".join(missing[:12]))
    if extra:
        errs.append("unexpected keys " + ", ".join(extra[:12]))
    out = []
    for key in src_map:
        lo, hi = (key.split("-") + [key])[:2]
        need = int(hi) - int(lo) + 1
        parts = got.get(key)
        if not isinstance(parts, list):
            continue
        parts = [str(p).strip().replace("|", "／") for p in parts]
        if len(parts) != need:
            errs.append(f"{key}: {len(parts)} parts, need {need}")
            continue
        if any(not p for p in parts):
            errs.append(f"{key}: empty part")
            continue
        out.append(key + "\t" + "｜".join(parts))
    if errs or len(out) != len(src_map):
        raise LLMError("; ".join(errs[:24]) or "incomplete groups")
    return "\n".join(out) + "\n"


def _check(workdir, stem) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "tr_check.py"), "--workdir", workdir, "--only", stem],
        capture_output=True, text=True)
    log = (p.stdout or "") + (p.stderr or "")
    return p.returncode, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--lang", default="zh")
    a = ap.parse_args()
    wd = a.workdir
    srcs = sorted(glob.glob(os.path.join(wd, "tr", "b*.src.txt")))
    if not srcs:
        sys.exit("no translation blocks — run tr_split.py first")
    glossary = ""
    gpath = os.path.join(wd, "glossary.txt")
    if os.path.exists(gpath):
        glossary = open(gpath, encoding="utf-8").read()[:8000]
    client = Client(usage_path=os.path.join(wd, "usage.jsonl"))
    failed_blocks = []
    real_blocks = 0
    for sp in srcs:
        stem = os.path.basename(sp).replace(".src.txt", "")
        src_map, _ = tr_check.parse(sp)
        body = open(sp, encoding="utf-8").read()
        dest = sp.replace(".src.txt", f".{a.lang}.txt")
        errors = ""
        ok = False
        for attempt in range(1, 4):
            user = f"Glossary:\n{glossary or '(none)'}\n\nSource block:\n{body}"
            if errors:
                user += f"\n\nYour previous output failed checks. Fix every error and return the full JSON again:\n{errors[:2500]}"
            try:
                text = client.chat(
                    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                    step="translate", temperature=0.2, json_mode=True, max_tokens=8192,
                    extra={"block": stem, "attempt": attempt})
                rendered = _render(parse_json_content(text), src_map)
                open(dest, "w", encoding="utf-8").write(rendered)
                rc, log = _check(wd, stem)
                print(log, end="" if log.endswith("\n") or not log else "\n", flush=True)
                if rc == 0:
                    ok = True
                    real_blocks += 1
                    break
                errors = log
            except InfraError:
                raise
            except (LLMError, Exception) as e:
                errors = str(e)
                print(f"  {stem} attempt {attempt}: {e}", flush=True)
        if not ok:
            print(f"  {stem}: still failing after 3 tries; marking lines ［未译］", flush=True)
            open(dest, "w", encoding="utf-8").write(_marker(src_map))
            rc, log = _check(wd, stem)
            print(log, flush=True)
            if rc != 0:
                raise SystemExit(f"{stem}: marker file still fails tr_check\n{log}")
            failed_blocks.append(stem)
    rc, log = _check_all(wd)
    print(log, flush=True)
    if rc != 0:
        raise SystemExit(f"tr_check failed\n{log}")
    report = {
        "blocks": len(srcs),
        "blocks_translated": real_blocks,
        "failed_blocks": failed_blocks,
    }
    # tr_check already printed the line count; record failed blocks for the episode report.
    json.dump(report, open(os.path.join(wd, "translate_report.json"), "w", encoding="utf-8"), indent=2)
    print(f"translate: {real_blocks}/{len(srcs)} blocks clean, failed={failed_blocks or 'none'}", flush=True)


def _check_all(workdir) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "tr_check.py"), "--workdir", workdir],
        capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


if __name__ == "__main__":
    try:
        main()
    except InfraError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(2)

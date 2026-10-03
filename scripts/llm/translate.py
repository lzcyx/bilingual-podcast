#!/usr/bin/env python3
"""Translate tr/bNN.src.txt blocks. Each block is checked with tr_check.py --only (max 3 tries)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import Client, InfraError, LLMError, parse_json_content  # noqa: E402

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import tr_check  # noqa: E402

MAX_WORKERS = 4


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
        lines.append(key + "\t" + "｜".join(["［未译］"] * _need(key)))
    return "\n".join(lines) + "\n"


def _need(key: str) -> int:
    a, b = (key.split("-") + [key])[:2]
    return int(b) - int(a) + 1


def _groups(obj) -> dict:
    got = {}
    if not isinstance(obj, dict):
        return got
    for g in obj.get("groups") or []:
        if not isinstance(g, dict):
            continue
        key = str(g.get("key") or "").strip()
        if key:
            got[key] = g.get("parts")
    return got


def _salvage(obj, src_map: dict) -> str:
    """Keep every good line. A bad line becomes ［未译］, not the whole block."""
    got = _groups(obj)
    out = []
    for key in src_map:
        need = _need(key)
        raw = got.get(key)
        parts = [str(p).strip().replace("|", "／") for p in raw] if isinstance(raw, list) else []
        if len(parts) != need:
            parts = ["［未译］"] * need
        else:
            parts = [p if p else "［未译］" for p in parts]
        out.append(key + "\t" + "｜".join(parts))
    return "\n".join(out) + "\n"


def _render(obj, src_map: dict) -> str:
    if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
        raise LLMError("response needs {\"groups\": [{\"key\", \"parts\"}]}")
    got = _groups(obj)
    errs = []
    missing = [k for k in src_map if k not in got]
    extra = [k for k in got if k not in src_map]
    if missing:
        errs.append("missing keys " + ", ".join(missing[:12]))
    if extra:
        errs.append("unexpected keys " + ", ".join(extra[:12]))
    out = []
    for key in src_map:
        need = _need(key)
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


def _translate_block(client, wd, sp, lang, glossary):
    stem = os.path.basename(sp).replace(".src.txt", "")
    src_map, _ = tr_check.parse(sp)
    body = open(sp, encoding="utf-8").read()
    dest = sp.replace(".src.txt", f".{lang}.txt")
    errors = ""
    last_obj = None
    for attempt in range(1, 4):
        user = f"Glossary:\n{glossary or '(none)'}\n\nSource block:\n{body}"
        if errors:
            user += f"\n\nYour previous output failed checks. Fix every error and return the full JSON again:\n{errors[:2500]}"
        try:
            text = client.chat(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                step="translate", temperature=0.2, json_mode=True, max_tokens=8192,
                extra={"block": stem, "attempt": attempt})
            obj = parse_json_content(text)
            last_obj = obj
            rendered = _render(obj, src_map)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(rendered)
            rc, log = _check(wd, stem)
            print(log, end="" if log.endswith("\n") or not log else "\n", flush=True)
            if rc == 0:
                return {"stem": stem, "ok": True}
            errors = log
        except InfraError:
            raise
        except (LLMError, Exception) as e:
            errors = str(e)
            print(f"  {stem} attempt {attempt}: {e}", flush=True)
    rendered = _salvage(last_obj, src_map) if last_obj is not None else _marker(src_map)
    blank = rendered.count("［未译］")
    print(f"  {stem}: keeping partial translation, {blank} line(s) left ［未译］", flush=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(rendered)
    rc, log = _check(wd, stem)
    print(log, flush=True)
    if rc != 0:
        raise SystemExit(f"{stem}: marker file still fails tr_check\n{log}")
    return {"stem": stem, "ok": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--jobs", type=int, default=MAX_WORKERS)
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
    workers = max(1, min(a.jobs, MAX_WORKERS))
    failed_blocks = []
    real_blocks = 0
    outcomes = {}
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(_translate_block, client, wd, sp, a.lang, glossary): sp for sp in srcs}
        for fut in as_completed(futs):
            try:
                item = fut.result()
            except InfraError:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            outcomes[item["stem"]] = item
    finally:
        ex.shutdown(wait=True)
    for sp in srcs:
        stem = os.path.basename(sp).replace(".src.txt", "")
        item = outcomes.get(stem)
        if item and item["ok"]:
            real_blocks += 1
        elif item:
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
    with open(os.path.join(wd, "translate_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
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

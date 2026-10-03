#!/usr/bin/env python3
"""Translate tr/bNN.src.txt blocks. Each block is checked with tr_check.py --only (max 3 tries)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
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
- Every source key appears exactly once. A group split by " | " into n pieces must return exactly n non-empty parts, in order. Never return "" and never add or drop a part.
- Put the Chinese that lines up with each English piece in that part. You may move a word across the break so each part still matches what is said there, but do not drop or summarise.
- Spoken, natural 简体中文. Keep fillers light (嗯、对、就是).
- Official Chinese titles of games, films, shows and products go in 《》. If there is no official title, keep the original inside 《》.
- Host and guest names stay in English. Brands (PlayStation, DualSense, PS5, Xbox, Nintendo, Steam) stay as-is.
- Follow the glossary exactly when it applies. Do not use the half-width character | inside a part.
- Read # speaker and # context lines first; they are not keys.
"""


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


def _fit_parts(raw) -> list[str]:
    """Normalize model parts without guessing, merging, or splitting translated text."""
    if not isinstance(raw, list):
        return []
    return [str(p).strip().replace("|", "／").replace("｜", "") for p in raw]


def _usable(parts: list[str], need: int) -> bool:
    return len(parts) == need and all(p and p != "［未译］" for p in parts)


def _salvage(obj, src_map: dict) -> str:
    """Keep structurally valid groups; never invent subtitle boundaries locally."""
    got = _groups(obj)
    out = []
    for key in src_map:
        need = _need(key)
        parts = _fit_parts(got.get(key))
        if not _usable(parts, need):
            parts = ["［未译］"] * need
        out.append(key + "\t" + "｜".join(parts))
    return "\n".join(out) + "\n"


def _render(obj, src_map: dict) -> str:
    if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
        raise LLMError("response needs {\"groups\": [{\"key\", \"parts\"}]}")
    got = _groups(obj)
    missing = [k for k in src_map if k not in got]
    errs = []
    if missing:
        errs.append("missing keys " + ", ".join(missing[:12]))
    out = []
    for key in src_map:
        if key not in got:
            continue
        need = _need(key)
        parts = _fit_parts(got.get(key))
        if len(parts) != need:
            errs.append(f"{key}: {len(parts)} parts, need {need}")
            continue
        if not _usable(parts, need):
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


def _source_subset(body: str, src_map: dict, keys) -> str:
    """Keep context plus the speaker immediately associated with each selected group."""
    wanted = set(keys)
    headers = []
    selected = []
    current_speaker = None
    emitted_speaker = None
    for ln in body.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith("# speaker:"):
            current_speaker = stripped
            continue
        if stripped.startswith("# context:") or stripped.startswith("# block"):
            headers.append(stripped)
            continue
        if stripped.startswith("#"):
            continue
        key = stripped.split("\t", 1)[0].split(None, 1)[0]
        if key not in wanted:
            continue
        if current_speaker and current_speaker != emitted_speaker:
            selected.append(current_speaker)
            emitted_speaker = current_speaker
        selected.append(key + "\t" + src_map[key])
    return "\n".join(headers + selected)


def _take(obj, src_map: dict, keys):
    got = _groups(obj)
    good, bad = {}, []
    for key in keys:
        if key not in got:
            bad.append(key)
            continue
        need = _need(key)
        parts = _fit_parts(got.get(key))
        if _usable(parts, need):
            good[key] = parts
        else:
            bad.append(key)
    return good, bad


def _format(src_map: dict, parts_by_key: dict) -> str:
    return "\n".join(key + "\t" + "｜".join(parts_by_key[key]) for key in src_map) + "\n"


def _bad_keys_from_check(log: str, src_map: dict) -> list[str]:
    bad_lines = set()
    for a, b in re.findall(r"ERROR: .*?line (\d+)(?:-(\d+))?", log):
        lo, hi = int(a), int(b or a)
        bad_lines.update(range(lo, hi + 1))
    bad = []
    for key in src_map:
        lo, hi = tr_check.rng(key)
        if any(lo <= i <= hi for i in bad_lines):
            bad.append(key)
    return bad


def _translate_block(client, wd, sp, lang, glossary):
    stem = os.path.basename(sp).replace(".src.txt", "")
    src_map, _ = tr_check.parse(sp)
    body = open(sp, encoding="utf-8").read()
    dest = sp.replace(".src.txt", f".{lang}.txt")
    errors = ""
    accepted: dict = {}
    for attempt in range(1, 4):
        pending = [k for k in src_map if k not in accepted]
        user = f"Glossary:\n{glossary or '(none)'}\n\nSource block:\n{_source_subset(body, src_map, pending)}"
        if errors:
            user += (
                "\n\nYour previous output failed checks. Return JSON for every key above, "
                f"with one non-empty part per ' | ' piece:\n{errors[:2500]}"
            )
        try:
            text = client.chat(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                step="translate", temperature=0.2, json_mode=True, max_tokens=8192,
                extra={"block": stem, "attempt": attempt})
            obj = parse_json_content(text)
            good, bad = _take(obj, src_map, pending)
            accepted.update(good)
            if bad:
                errors = "still missing or empty: " + ", ".join(bad[:12])
                print(f"  {stem} attempt {attempt}: {errors}", flush=True)
                continue
            rendered = _format(src_map, accepted)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(rendered)
            rc, log = _check(wd, stem)
            print(log, end="" if log.endswith("\n") or not log else "\n", flush=True)
            if rc == 0:
                return {"stem": stem, "ok": True}
            errors = log
            bad_quality = _bad_keys_from_check(log, src_map)
            if attempt < 3:
                if bad_quality:
                    for key in bad_quality:
                        accepted.pop(key, None)
                else:
                    accepted = {}
        except InfraError:
            raise
        except (LLMError, Exception) as e:
            errors = str(e)
            print(f"  {stem} attempt {attempt}: {e}", flush=True)
    missing = [key for key in src_map if key not in accepted]
    raise SystemExit(f"{stem}: translation failed quality checks after 3 attempts; pending={missing[:12]}; {errors[:1200]}")


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

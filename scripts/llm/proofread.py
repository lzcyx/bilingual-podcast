#!/usr/bin/env python3
"""Proofread lines_raw into fixes.json, edits.json and speakers.json via the LLM."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import Client, InfraError, LLMError, parse_json_content  # noqa: E402
from schema import paint_speakers, validate_edits, validate_fixes, validate_speakers  # noqa: E402


GLOBAL_SYSTEM = """You proofread an English podcast transcript before it is translated into simplified Chinese.
Return JSON: {"fixes": [["regex", "replacement"]], "speakers": {"S0": {"name": "Real Name"}}, "glossary_lines": ["Term -> 中文"]}
fixes: Python regular expressions applied to EVERY line. Use them only for systematic mis-hearings of names and titles (word boundaries). Do not "fix" grammar.
speakers: map every speaker id you are given (S0, S1, …) to a real name from the show notes or from how people introduce themselves. The advertising voice (pre-roll / mid-roll, sponsor reads that are not the hosts) must be named exactly "Ad". If you truly cannot tell, keep the id as the name. Optional "color" is "#rrggbb".
glossary_lines: source term -> simplified Chinese. Official game/film/show titles in 《》. People's names stay in English. One item per array entry, not a paragraph.
Do not return line-by-line edits here.
"""

CHUNK_SYSTEM = """You correct one slice of an English podcast transcript. Return JSON: {"edits": {"LINE_ID": {...}}}
Only include lines that need a change. An empty edits object is fine.
Allowed values:
- {"drop": true}  hallucination: repeated phrases in silence, "thanks for watching", music-only, subtitle credits
- {"en": "corrected full line"}  one bad name or a clearly wrong word. Keep it spoken English. Do not rewrite style.
- {"spk": "S1"}  wrong speaker id
- {"merge_prev": true}  this fragment belongs on the previous line
- {"split": [[6, "first six words.", "", "S0"], [0, "rest.", "", "S1"]]}  only when two speakers share one line. Word counts must cover the line exactly (0 means the rest).
Never invent ids. Never return fixes or speakers. Be conservative: if a line is already fine, omit it.
"""


def _chunk(lines, size):
    for i in range(0, len(lines), size):
        yield lines[i:i + size]


def _samples(lines):
    by = {}
    for ln in lines:
        spk = ln.get("spk") or "?"
        by.setdefault(spk, [])
        if len(by[spk]) < 6:
            by[spk].append(ln.get("en", "")[:180])
    return by


def _tsv(rows):
    out = []
    for ln in rows:
        n = len(ln.get("w") or []) or len((ln.get("en") or "").split())
        out.append(f"{ln['id']}\t{ln.get('spk') or ''}\t{n}\t{ln.get('en', '')}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--chunk", type=int, default=50)
    a = ap.parse_args()
    wd = a.workdir
    raw = json.load(open(os.path.join(wd, "lines_raw.json"), encoding="utf-8"))
    by_id = {ln["id"]: ln for ln in raw}
    page = ""
    page_path = os.path.join(wd, "page.txt")
    if os.path.exists(page_path):
        page = open(page_path, encoding="utf-8").read()[:12000]
    glossary = ""
    gpath = os.path.join(wd, "glossary.txt")
    if os.path.exists(gpath):
        glossary = open(gpath, encoding="utf-8").read()[:6000]
    used = sorted({ln.get("spk") for ln in raw if ln.get("spk")}, key=lambda s: (len(s), s))
    client = Client(usage_path=os.path.join(wd, "usage.jsonl"))

    fixes, speakers, extra_gloss = [], {}, []
    try:
        user = (
            f"Speaker ids in this episode: {', '.join(used) or '(none)'}\n\n"
            f"Sample lines per speaker:\n{json.dumps(_samples(raw), ensure_ascii=False)}\n\n"
            f"Existing glossary:\n{glossary or '(none)'}\n\nShow notes:\n{page or '(none)'}"
        )
        text = client.chat(
            [{"role": "system", "content": GLOBAL_SYSTEM}, {"role": "user", "content": user}],
            step="proofread", temperature=0.2, json_mode=True, max_tokens=4096)
        obj = parse_json_content(text)
        fixes, w1 = validate_fixes(obj.get("fixes"))
        speakers, w2 = validate_speakers(obj.get("speakers"))
        for w in w1 + w2:
            print("  warn:", w, flush=True)
        gl = obj.get("glossary_lines")
        if isinstance(gl, list):
            extra_gloss = [str(x).strip() for x in gl if isinstance(x, str) and "->" in x]
    except InfraError:
        raise
    except (LLMError, Exception) as e:
        print(f"proofread: global pass failed, continuing without regex fixes ({e})", flush=True)

    speakers = paint_speakers(speakers, used)
    json.dump(fixes, open(os.path.join(wd, "fixes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(speakers, open(os.path.join(wd, "speakers.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if extra_gloss:
        with open(gpath, "a", encoding="utf-8") as f:
            if glossary and not glossary.endswith("\n"):
                f.write("\n")
            for line in extra_gloss:
                f.write(line + "\n")

    edits = {}
    for n, rows in enumerate(_chunk(raw, a.chunk), 1):
        chunk_ids = {ln["id"] for ln in rows}
        user_base = "id, speaker, word_count, text\n" + _tsv(rows)
        errors = ""
        part = {}
        for attempt in range(1, 4):
            user = user_base
            if errors:
                user += "\n\nYour previous JSON used line ids that are not in this slice. Return edits only for these ids:\n" + errors
            try:
                text = client.chat(
                    [{"role": "system", "content": CHUNK_SYSTEM}, {"role": "user", "content": user}],
                    step="proofread", temperature=0.2, json_mode=True, max_tokens=4096,
                    extra={"chunk": n, "attempt": attempt})
                obj = parse_json_content(text)
                edits_obj = obj.get("edits") if isinstance(obj, dict) else None
                if not isinstance(edits_obj, dict):
                    errors = "edits must be an object keyed by the line ids in this slice"
                    print(f"  proofread chunk {n} attempt {attempt}: {errors}", flush=True)
                    continue
                unknown = [str(k) for k in edits_obj if str(k) not in chunk_ids]
                if unknown:
                    errors = "unknown line ids: " + ", ".join(unknown[:20])
                    print(f"  proofread chunk {n} attempt {attempt}: {errors}", flush=True)
                    if attempt < 3:
                        continue
                part, warns = validate_edits(edits_obj, {i: by_id[i] for i in chunk_ids})
                for w in warns[:20]:
                    print("  warn:", w, flush=True)
                break
            except InfraError:
                raise
            except (LLMError, Exception) as e:
                errors = str(e)
                print(f"proofread: chunk {n} attempt {attempt} failed ({e})", flush=True)
        else:
            print(f"proofread: chunk {n} left unchanged", flush=True)
        edits.update(part)
    json.dump(edits, open(os.path.join(wd, "edits.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"proofread: {len(fixes)} fixes, {len(edits)} edits, {len(speakers)} speakers", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InfraError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(2)

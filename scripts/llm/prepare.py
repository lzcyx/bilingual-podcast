#!/usr/bin/env python3
"""Before transcription: Whisper prompt, glossary, and cast size (for diarize --num-speakers)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import Client, InfraError, LLMError, parse_json_content  # noqa: E402


SYSTEM = """You prepare one podcast episode for English ASR and Chinese translation.
Return a JSON object with exactly these keys:
{"confident": true, "hosts": ["Full Name"], "guests": ["Full Name"], "prompt": "...", "glossary": "..."}
Rules:
- hosts / guests: real people named in the show notes. Full names, English spelling. Do not invent people. Do not include advertisers or unnamed "host".
- confident: true only when the notes actually name the cast. Otherwise false and empty arrays.
- prompt: 1 to 3 spoken English sentences a listener might hear, naming the show, the hosts, and the main game/film titles. No more than 400 characters. This is a spelling hint for Whisper, not a summary essay.
- glossary: one "English term -> 简体中文" per line. Official Chinese titles of games, films, shows and products go in 《》. Host and guest names stay in English (write "Name -> Name"). Brands such as PlayStation, DualSense, PS5, Xbox, Nintendo stay as-is. Skip lines you are not sure about.
"""


def _read(path, limit):
    if not os.path.exists(path):
        return ""
    return open(path, encoding="utf-8").read()[:limit]


def _heuristic(show, title, page):
    bits = [b for b in (show, title) if b]
    prompt = ("Welcome to " + ", ".join(bits) + ".")[:400]
    return prompt


def _people(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for name in raw:
        if not isinstance(name, str):
            continue
        name = re.sub(r"\s+", " ", name).strip()
        if not (2 <= len(name) <= 48) or not re.search(r"[A-Za-z]", name):
            continue
        if name.lower() not in {n.lower() for n in out}:
            out.append(name)
    return out[:8]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--show", default="")
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    wd = a.workdir
    page = _read(os.path.join(wd, "page.txt"), 12000)
    show, title = a.show, a.title
    cast = {"hosts": [], "guests": [], "confident": False, "num_speakers": None}
    prompt = _heuristic(show, title, page)
    glossary = ""
    try:
        client = Client(usage_path=os.path.join(wd, "usage.jsonl"))
        user = f"Show: {show}\nEpisode: {title}\n\nShow notes:\n{page or '(none)'}"
        text = client.chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            step="prepare", temperature=0.2, json_mode=True, max_tokens=2048)
        obj = parse_json_content(text)
        hosts, guests = _people(obj.get("hosts")), _people(obj.get("guests"))
        confident = bool(obj.get("confident")) and bool(hosts or guests)
        num = (len(hosts) + len(guests) + 1) if confident and 1 <= len(hosts) + len(guests) <= 8 else None
        cast = {"hosts": hosts, "guests": guests, "confident": confident, "num_speakers": num}
        p = obj.get("prompt")
        if isinstance(p, str) and p.strip():
            prompt = re.sub(r"\s+", " ", p).strip()[:500]
        g = obj.get("glossary")
        if isinstance(g, str):
            glossary = g.strip()
    except InfraError:
        raise
    except (LLMError, Exception) as e:
        print(f"prepare: model step failed, using a plain Whisper prompt ({e})", flush=True)
    open(os.path.join(wd, "prompt.txt"), "w", encoding="utf-8").write(prompt + "\n")
    if glossary:
        open(os.path.join(wd, "glossary.txt"), "w", encoding="utf-8").write(glossary.rstrip() + "\n")
    json.dump(cast, open(os.path.join(wd, "cast.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"prepare: num_speakers={cast['num_speakers']} confident={cast['confident']} "
          f"hosts={cast['hosts']} guests={cast['guests']}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except InfraError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(2)

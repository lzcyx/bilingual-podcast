#!/usr/bin/env python3
"""Build one bilingual player. Spec is the JSON object from the check-job matrix (env EPISODE_SPEC)."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable


def redact(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", s)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-[redacted]", s)
    s = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", s)
    return s[:800]


class StepError(RuntimeError):
    def __init__(self, message, infra=False):
        super().__init__(message)
        self.infra = infra


def run(cmd):
    print("+ " + " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    if rc != 0:
        raise StepError(f"exit {rc}: {os.path.basename(cmd[1] if len(cmd) > 1 else cmd[0])}", infra=(rc == 2))


def load_spec() -> dict:
    raw = os.environ.get("EPISODE_SPEC")
    if not raw and len(sys.argv) > 1:
        raw = open(sys.argv[1], encoding="utf-8").read()
    if not raw:
        sys.exit("EPISODE_SPEC is empty")
    spec = json.loads(raw)
    for k in ("show_id", "guid", "audio_url", "rss", "key", "slug"):
        if not spec.get(k):
            sys.exit(f"episode spec missing {k}")
    return spec


def speaker_names(path: str) -> list:
    if not os.path.exists(path):
        return []
    raw = json.load(open(path, encoding="utf-8"))
    def sk(k):
        m = re.fullmatch(r"S(\d+)", str(k))
        return (0, int(m.group(1))) if m else (1, str(k))
    names = []
    for k in sorted(raw, key=sk):
        v = raw[k]
        name = v.get("name") if isinstance(v, dict) else str(v)
        if name and name not in names:
            names.append(name)
    return names


def probe_duration(path: str):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            text=True)
        return int(round(float(json.loads(out)["format"]["duration"])))
    except Exception as e:
        print("ffprobe failed:", e, flush=True)
        return None


def parse_test_json(text: str):
    text = text or ""
    start = text.rfind("\n{")
    blob = text[start + 1:] if start >= 0 else text
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        i = text.find("{")
        j = text.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                return None
        return None


def _usage_line(usage: dict) -> str:
    if not usage or not usage.get("calls"):
        return "tokens: 没有调用 DeepSeek"
    sys.path.insert(0, SCRIPTS)
    from llm.client import format_usage
    return "tokens: " + format_usage(usage)


def write_summary(report: dict):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    usage = report.get("usage") or {}
    lines = [
        f"## {report.get('show_id')} — {report.get('title')}",
        "",
        f"- audio_mode: `{report.get('audio_mode')}`  dynamic_ads: `{report.get('dynamic_ads')}`",
        f"- total: **{report.get('total_seconds')}s**",
        f"- {_usage_line(usage)}",
        "",
        "| step | seconds |",
        "|---|---:|",
    ]
    for s in report.get("steps") or []:
        lines.append(f"| {s['name']} | {s['seconds']} |")
    tr = report.get("translation") or {}
    if tr:
        lines.append("")
        lines.append(f"Translation blocks clean: {tr.get('blocks_translated')}/{tr.get('blocks')}"
                     + (f"  failed: {', '.join(tr.get('failed_blocks') or [])}" if tr.get("failed_blocks") else ""))
    if report.get("error"):
        lines.append("")
        lines.append(f"Error: {report['error']}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    os.chdir(ROOT)
    spec = load_spec()
    runs = os.path.join(ROOT, "runs")
    out = os.path.join(runs, "artifact")
    wd = os.path.join(runs, "work")
    os.makedirs(wd, exist_ok=True)
    os.makedirs(os.path.join(out, "shots"), exist_ok=True)
    result_path = os.path.join(out, "result.json")
    report_path = os.path.join(out, "report.json")
    t0 = time.time()
    steps = []

    def dump(ok, infra, error, summary=True, **extra):
        usage = {}
        try:
            sys.path.insert(0, SCRIPTS)
            from llm.client import load_llm_config, usage_summary
            usage = usage_summary(os.path.join(wd, "usage.jsonl"), load_llm_config(ROOT))
        except Exception:
            usage = {}
        report = {
            "show_id": spec.get("show_id"),
            "guid": spec.get("guid"),
            "title": extra.get("title") or spec.get("title"),
            "slug": spec.get("slug"),
            "audio_mode": extra.get("audio_mode"),
            "dynamic_ads": extra.get("dynamic_ads"),
            "steps": steps,
            "total_seconds": round(time.time() - t0, 1),
            "usage": usage,
            "translation": extra.get("translation"),
            "test": extra.get("test"),
            "error": redact(error) if error else None,
        }
        body = {
            "ok": ok,
            "infra_error": bool(infra),
            "error": report["error"],
            "key": spec["key"],
            "show_id": spec["show_id"],
            "show": extra.get("show") or spec.get("show") or spec["show_id"],
            "guid": spec["guid"],
            "title": report["title"],
            "episode": str(extra.get("episode") or spec.get("episode") or ""),
            "pub_date": spec.get("pub_date") or "",
            "slug": spec["slug"],
            "duration_sec": extra.get("duration_sec") or spec.get("duration_sec") or 0,
            "audio_mode": extra.get("audio_mode") or "",
            "dynamic_ads": bool(extra.get("dynamic_ads")),
            "speakers": extra.get("speakers") or [],
            "size_bytes": extra.get("size_bytes") or 0,
            "cover_file": extra.get("cover_file") or "",
            "html_file": "player.html" if ok else "",
            "report": report,
        }
        for path, obj in ((result_path, body), (report_path, report)):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        if summary:
            write_summary(report)
        return body

    # If the runner kills us, this counts as a real failed attempt (not an infra skip).
    dump(False, False, "build did not finish (timeout or crash)", summary=False)

    def step(name, fn):
        print(f"\n===== {name} =====", flush=True)
        st = time.time()
        try:
            return fn()
        finally:
            dt = round(time.time() - st, 1)
            steps.append({"name": name, "seconds": dt})
            print(f"===== {name} {dt}s =====", flush=True)

    try:
        def fetch():
            cmd = [PY, os.path.join(SCRIPTS, "fetch_episode.py"),
                   "--mp3", spec["audio_url"], "--rss", spec["rss"],
                   "--workdir", wd, "--audio-mode", "auto"]
            if spec.get("page_url"):
                cmd += ["--page", spec["page_url"]]
            run(cmd)
        step("fetch", fetch)

        meta = json.load(open(os.path.join(wd, "meta.json"), encoding="utf-8"))
        draft = json.load(open(os.path.join(wd, "config.draft.json"), encoding="utf-8"))
        show = draft.get("show") or spec.get("show") or spec["show_id"]
        title = draft.get("title") or spec.get("title") or ""

        def prepare():
            run([PY, os.path.join(SCRIPTS, "llm", "prepare.py"),
                 "--workdir", wd, "--show", show, "--title", title])
        step("prepare", prepare)
        cast = {}
        cast_path = os.path.join(wd, "cast.json")
        if os.path.exists(cast_path):
            cast = json.load(open(cast_path, encoding="utf-8"))

        def transcribe():
            cmd = [PY, os.path.join(SCRIPTS, "transcribe.py"), os.path.join(wd, "episode.mp3"),
                   "--workdir", wd, "--model", "large-v3-turbo", "--jobs", "2", "--lang", "en"]
            prompt = os.path.join(wd, "prompt.txt")
            if os.path.exists(prompt):
                cmd += ["--prompt-file", prompt]
            run(cmd)
        step("transcribe", transcribe)

        def diarize():
            cmd = [PY, os.path.join(SCRIPTS, "diarize.py"), "--workdir", wd]
            n = cast.get("num_speakers")
            if isinstance(n, int) and n >= 2:
                cmd += ["--num-speakers", str(n)]
            run(cmd)
        step("diarize", diarize)

        step("segment", lambda: run([PY, os.path.join(SCRIPTS, "segment.py"), "--workdir", wd]))
        step("proofread", lambda: run([PY, os.path.join(SCRIPTS, "llm", "proofread.py"), "--workdir", wd]))
        step("apply_edits", lambda: run([PY, os.path.join(SCRIPTS, "apply_edits.py"), "--workdir", wd]))
        step("tr_split", lambda: run([PY, os.path.join(SCRIPTS, "tr_split.py"), "--workdir", wd, "--block", "40"]))
        step("translate", lambda: run([PY, os.path.join(SCRIPTS, "llm", "translate.py"), "--workdir", wd]))

        config_path = os.path.join(wd, "config.json")
        draft["speakers"] = os.path.abspath(os.path.join(wd, "speakers.json"))
        draft["output"] = os.path.abspath(os.path.join(out, "player.html"))
        draft["cues"] = os.path.abspath(os.path.join(wd, "cues.json"))
        draft["chapters"] = os.path.abspath(os.path.join(wd, "chapters.json"))
        draft["workdir"] = os.path.abspath(wd)
        # Offline embed is 32 kbps mono (~16 MB per hour). Online episodes ignore this.
        draft["embed_kbps"] = 32
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
            f.write("\n")

        step("chapters", lambda: run([PY, os.path.join(SCRIPTS, "llm", "chapters.py"),
                                      "--workdir", wd, "--config", config_path]))
        # Follow config audio_mode. Do not pass --offline / --online.
        step("build", lambda: run([PY, os.path.join(SCRIPTS, "build.py"), config_path]))

        mode = draft.get("audio_mode") or "online"
        html = draft["output"]
        if not os.path.exists(html):
            raise StepError(f"build did not write {html}")

        def test():
            cmd = [PY, os.path.join(SCRIPTS, "test_player.py"), html,
                   "--shots", os.path.join(out, "shots", "d")]
            if mode == "online":
                cmd += ["--local-audio", os.path.join(wd, "episode.mp3")]
            print("+ " + " ".join(cmd), flush=True)
            p = subprocess.run(cmd, capture_output=True, text=True)
            sys.stdout.write(p.stdout or "")
            sys.stderr.write(p.stderr or "")
            if p.returncode != 0:
                tail = redact((p.stderr or p.stdout or "")[-500:])
                raise StepError(f"test_player.py failed {tail}")
            return parse_test_json(p.stdout)
        test_res = step("test", test)

        covers = sorted(glob.glob(os.path.join(wd, "cover.*")))
        cover_file = ""
        if covers:
            ext = os.path.splitext(covers[0])[1].lower()
            if ext == ".jpeg":
                ext = ".jpg"
            if ext not in (".jpg", ".png", ".webp"):
                ext = ".jpg"
            cover_file = "cover" + ext
            shutil.copyfile(covers[0], os.path.join(out, cover_file))

        translation = {}
        tr_path = os.path.join(wd, "translate_report.json")
        if os.path.exists(tr_path):
            translation = json.load(open(tr_path, encoding="utf-8"))
        duration = probe_duration(os.path.join(wd, "episode.mp3")) or spec.get("duration_sec") or 0
        dump(True, False, None,
             show=show, title=title, episode=draft.get("episode") or spec.get("episode") or "",
             audio_mode=mode, dynamic_ads=bool(meta.get("dynamic_ads")),
             duration_sec=duration, speakers=speaker_names(os.path.join(wd, "speakers.json")),
             size_bytes=os.path.getsize(html), cover_file=cover_file,
             translation=translation, test=test_res)
        print(f"OK {html} mode={mode} dynamic_ads={bool(meta.get('dynamic_ads'))} "
              f"{os.path.getsize(html) / 1e6:.2f} MB", flush=True)
    except StepError as e:
        print("FAILED:", redact(e), flush=True)
        dump(False, e.infra, str(e))
        sys.exit(2 if e.infra else 1)
    except Exception:
        err = traceback.format_exc()
        print(redact(err), flush=True)
        dump(False, False, err)
        sys.exit(1)


if __name__ == "__main__":
    main()

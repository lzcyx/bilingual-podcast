#!/usr/bin/env python3
"""Upload finished players to R2 and update index.json / seen.json / failed.json.

HTML and covers are uploaded with a long cache. index.json is no-cache.
A guid is marked seen only after its HTML and the updated index are both on R2.
Infrastructure errors (missing secrets, R2 down) do not increment the failure count.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

PUBLIC_FIELDS = (
    "show_id", "guid", "show", "title", "episode", "pub_date", "duration_sec",
    "audio_mode", "dynamic_ads", "html_url", "cover_url", "size_bytes", "speakers", "created_at",
)
HTML_CACHE = "public, max-age=31536000, immutable"
INDEX_CACHE = "no-cache"
MAX_ATTEMPTS = 3


class InfraError(RuntimeError):
    pass


def redact(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", s)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-[redacted]", s)
    s = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", s)
    return s[:800]


def classify_results(plan: dict, results_by_key: dict, now=None) -> dict:
    now_s = (now or datetime.now(timezone.utc)).isoformat()
    seen = {"bootstrapped": bool(plan["seen"].get("bootstrapped")),
            "items": dict(plan["seen"].get("items") or {})}
    failed = {"items": dict((plan.get("failed") or {}).get("items") or {})}
    ready, problems = [], []
    for ep in plan.get("queue") or []:
        res = results_by_key.get(ep["key"])
        if res is None:
            res = {"ok": False, "infra_error": False,
                   "error": "build job produced no result (timeout or crash)"}
        if res.get("ok"):
            ready.append((ep, res))
            continue
        if res.get("infra_error"):
            problems.append({
                "show_id": ep["show_id"], "guid": ep["guid"], "title": ep.get("title") or "",
                "infra": True, "error": redact(res.get("error") or "infra"),
            })
            continue
        key = ep["key"]
        rec = dict(failed["items"].get(key) or {})
        rec.update({
            "show_id": ep["show_id"], "guid": ep["guid"],
            "title": ep.get("title") or "", "pub_date": ep.get("pub_date") or "",
        })
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["abandoned"] = rec["attempts"] >= MAX_ATTEMPTS
        rec["last_error"] = redact(res.get("error") or "failed")
        rec["last_at"] = now_s
        failed["items"][key] = rec
        problems.append({
            "show_id": ep["show_id"], "guid": ep["guid"], "title": ep.get("title") or "",
            "infra": False, "attempts": rec["attempts"], "abandoned": rec["abandoned"],
            "error": rec["last_error"],
        })
    return {"seen": seen, "failed": failed, "ready": ready, "problems": problems, "now": now_s}


def mark_published(seen, failed, ep, now_s, slug):
    seen["items"][ep["key"]] = {
        "show_id": ep["show_id"], "guid": ep["guid"],
        "title": ep.get("title") or "", "pub_date": ep.get("pub_date") or "",
        "reason": "published", "marked_at": now_s, "slug": slug,
    }
    failed["items"].pop(ep["key"], None)


def merge_index(index, entries):
    index = [dict(ep) for ep in (index or [])]
    pos = {}
    for i, ep in enumerate(index):
        if ep.get("guid"):
            pos[(ep.get("show_id"), ep.get("guid"))] = i
    for ent in entries:
        k = (ent.get("show_id"), ent.get("guid"))
        if k in pos and k[1]:
            old = index[pos[k]]
            merged = dict(ent)
            merged["created_at"] = old.get("created_at") or ent["created_at"]
            index[pos[k]] = merged
        else:
            index.append(dict(ent))
            pos[k] = len(index) - 1
    index.sort(key=lambda e: e.get("pub_date") or "", reverse=True)
    return index


def load_results(results_dir: str) -> dict:
    found = {}
    if not results_dir or not os.path.isdir(results_dir):
        return found
    for dirpath, _, files in os.walk(results_dir):
        if "result.json" not in files:
            continue
        with open(os.path.join(dirpath, "result.json"), encoding="utf-8") as f:
            res = json.load(f)
        res["_dir"] = dirpath
        if res.get("key"):
            found[res["key"]] = res
    return found


def r2_config():
    names = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL"]
    missing = [n for n in names if not (os.environ.get(n) or "").strip()]
    if missing:
        raise InfraError("Missing environment variables: " + ", ".join(missing))
    import boto3
    from botocore.config import Config
    account = os.environ["R2_ACCOUNT_ID"].strip()
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    )
    base = os.environ["R2_PUBLIC_BASE_URL"].strip().rstrip("/")
    return client, os.environ["R2_BUCKET"].strip(), base


def _upload(client, bucket, path, key, content_type, cache):
    print(f"upload {key} ({os.path.getsize(path)} bytes)", flush=True)
    client.upload_file(path, bucket, key, ExtraArgs={"ContentType": content_type, "CacheControl": cache})


def _ctype(ext: str) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")


def build_entry(ep, res, base, created_at):
    slug = res.get("slug") or ep["slug"]
    show_id = ep["show_id"]
    html_key = f"episodes/{show_id}/{slug}.html"
    cover_file = res.get("cover_file") or ""
    ext = os.path.splitext(cover_file)[1].lower()
    cover_key = f"covers/{show_id}/{slug}{ext}" if ext else ""
    mode = res.get("audio_mode") if res.get("audio_mode") in ("online", "offline") else "online"
    entry = {
        "show_id": show_id,
        "guid": ep["guid"],
        "show": res.get("show") or ep.get("show") or show_id,
        "title": res.get("title") or ep.get("title") or "",
        "episode": str(res.get("episode") or ep.get("episode") or ""),
        "pub_date": ep.get("pub_date") or res.get("pub_date") or "",
        "duration_sec": int(res.get("duration_sec") or ep.get("duration_sec") or 0),
        "audio_mode": mode,
        "dynamic_ads": bool(res.get("dynamic_ads")),
        "html_url": f"{base}/{html_key}",
        "cover_url": f"{base}/{cover_key}" if cover_key else "",
        "size_bytes": int(res.get("size_bytes") or 0),
        "speakers": list(res.get("speakers") or []),
        "created_at": created_at,
    }
    return entry, html_key, cover_key


def dump_state(path, obj):
    if isinstance(obj, dict) and "items" in obj:
        obj = {**obj, "items": dict(sorted(obj["items"].items()))}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def summarise_reports(ready):
    pt = ct = 0
    cost = 0.0
    rows = []
    for _ep, res in ready:
        usage = ((res.get("report") or {}).get("usage") or {})
        pt += int(usage.get("prompt_tokens") or 0)
        ct += int(usage.get("completion_tokens") or 0)
        cost += float(usage.get("cost_cny") or 0)
        rep = res.get("report") or {}
        rows.append((res.get("show_id"), rep.get("total_seconds"), usage))
    return pt, ct, round(cost, 4), rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="run/plan/plan.json")
    ap.add_argument("--results", default="run/results")
    ap.add_argument("--seen", default="state/seen.json")
    ap.add_argument("--failed", default="state/failed.json")
    ap.add_argument("--index", default="state/index.json")
    a = ap.parse_args()
    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    classified = classify_results(plan, load_results(a.results))
    seen, failed = classified["seen"], classified["failed"]
    ready, problems = classified["ready"], classified["problems"]
    now_s = classified["now"]
    entries = []

    if ready:
        try:
            client, bucket, base = r2_config()
            staged = []
            for ep, res in ready:
                folder = res.get("_dir") or ""
                html_path = os.path.join(folder, res.get("html_file") or "player.html")
                if not os.path.isfile(html_path):
                    raise InfraError(f"missing player html for {ep['show_id']} {ep['guid']}")
                entry, html_key, cover_key = build_entry(ep, res, base, now_s)
                cover_path = os.path.join(folder, res["cover_file"]) if res.get("cover_file") else ""
                if not (cover_key and cover_path and os.path.isfile(cover_path)):
                    entry["cover_url"] = ""
                    cover_key, cover_path = "", ""
                staged.append((ep, res, entry, html_path, html_key, cover_path, cover_key))
            for _ep, _res, _entry, html_path, html_key, cover_path, cover_key in staged:
                _upload(client, bucket, html_path, html_key, "text/html; charset=utf-8", HTML_CACHE)
                if cover_key:
                    _upload(client, bucket, cover_path, cover_key, _ctype(os.path.splitext(cover_key)[1]), HTML_CACHE)
            index = []
            if os.path.exists(a.index):
                with open(a.index, encoding="utf-8") as f:
                    index = json.load(f)
            entries = [{k: entry[k] for k in PUBLIC_FIELDS} for _ep, _res, entry, *_rest in staged]
            index = merge_index(index, entries)
            fd, tmp = tempfile.mkstemp(suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                _upload(client, bucket, tmp, "index.json", "application/json; charset=utf-8", INDEX_CACHE)
            finally:
                os.remove(tmp)
            # Seen only after index.json is on R2. A crash before this retries the episode.
            for ep, res, _entry, *_rest in staged:
                mark_published(seen, failed, ep, now_s, res.get("slug") or ep.get("slug"))
            dump_state(a.index, index)
        except Exception as e:
            print("ERROR:", redact(e), file=sys.stderr)
            problems.append({"show_id": "*", "guid": "", "title": "publish", "infra": True, "error": redact(e)})
            entries = []

    state_changed = bool(plan.get("bootstrapped_now") or entries or any(not p.get("infra") for p in problems))
    # Failure bookkeeping (non-infra) is already in `failed`, including when upload was skipped.
    if state_changed or any(not p.get("infra") for p in problems):
        dump_state(a.seen, seen)
        dump_state(a.failed, failed)

    pt, ct, cost, _rows = summarise_reports(ready if entries else [])
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        lines = ["## Publish", ""]
        if plan.get("bootstrapped_now"):
            lines.append(f"First run marked **{len(plan['seen']['items'])}** existing episodes as seen (not built).")
        lines.append(f"Published **{len(entries)}**. Problems: **{len(problems)}**.")
        lines.append(f"Tokens (published): in {pt} / out {ct} / est. ¥{cost} off-peak.")
        for ent in entries:
            lines.append(
                f"- `{ent['show_id']}` {ent['audio_mode']} ads={ent['dynamic_ads']} "
                f"{ent['size_bytes']} bytes — {str(ent['title']).replace('|', '/')}"
            )
            lines.append(f"  {ent['html_url']}")
        for prob in problems:
            kind = "infra (will retry without counting)" if prob.get("infra") else (
                f"attempt {prob.get('attempts')}" + (" abandoned" if prob.get("abandoned") else ""))
            lines.append(f"- FAIL `{prob.get('show_id')}` {prob.get('title')}: {kind} — {prob.get('error')}")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    print(f"published={len(entries)} problems={len(problems)} bootstrapped_now={plan.get('bootstrapped_now')}")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Stage a GitHub Pages site and update seen / failed / index state.

The site is a directory (default site-dist/). The workflow pushes it to the
gh-pages branch as an orphan commit. A guid is marked seen only by `ack`,
which runs after that push succeeds.

Offline HTML older than keep_days (shows.yaml, default 60) is deleted.
The index row stays with archived=true. Online HTML is kept.
The whole site is kept under 1 GiB by archiving the oldest offline players.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PUBLIC_FIELDS = (
    "show_id", "guid", "show", "title", "episode", "pub_date", "duration_sec",
    "audio_mode", "dynamic_ads", "html_url", "cover_url", "size_bytes", "speakers",
    "created_at", "archived",
)
MAX_ATTEMPTS = 3
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_SITE_BYTES = 1024 ** 3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InfraError(RuntimeError):
    pass


def redact(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", s)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-[redacted]", s)
    s = re.sub(r"\d{6,}:[A-Za-z0-9_-]{20,}", "[redacted]", s)
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
        _note_failure(failed, ep, res.get("error") or "failed", now_s)
        rec = failed["items"][ep["key"]]
        problems.append({
            "show_id": ep["show_id"], "guid": ep["guid"], "title": ep.get("title") or "",
            "infra": False, "attempts": rec["attempts"], "abandoned": rec["abandoned"],
            "error": rec["last_error"],
        })
    return {"seen": seen, "failed": failed, "ready": ready, "problems": problems, "now": now_s}


def _note_failure(failed, ep, error, now_s):
    key = ep["key"]
    rec = dict(failed["items"].get(key) or {})
    rec.update({
        "show_id": ep["show_id"], "guid": ep["guid"],
        "title": ep.get("title") or "", "pub_date": ep.get("pub_date") or "",
    })
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    rec["abandoned"] = rec["attempts"] >= MAX_ATTEMPTS
    rec["last_error"] = redact(error)
    rec["last_at"] = now_s
    failed["items"][key] = rec


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
            merged["created_at"] = old.get("created_at") or ent.get("created_at")
            if "archived" not in merged:
                merged["archived"] = bool(old.get("archived"))
            index[pos[k]] = merged
        else:
            index.append(dict(ent))
            pos[k] = len(index) - 1
    index.sort(key=lambda e: e.get("pub_date") or "", reverse=True)
    return index


def rel_under(url: str, marker: str) -> str:
    if not url:
        return ""
    i = url.find(marker)
    if i < 0:
        return ""
    return url[i:].split("?", 1)[0].split("#", 1)[0]


def entry_time(entry) -> datetime:
    for key in ("pub_date", "created_at"):
        raw = entry.get(key) or ""
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.min.replace(tzinfo=timezone.utc)


def apply_retention(index, files, *, keep_days, now, max_bytes=MAX_SITE_BYTES):
    """Return (index, deleted relative paths).

    files maps a site-relative path to its size in bytes.
    Offline HTML older than keep_days, already archived, or already missing
    is marked archived and removed. If the remaining tree is still over
    max_bytes, the oldest offline HTML files are archived until it fits.
    """
    index = [dict(e) for e in index]
    deleted = set()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    for entry in index:
        rel = rel_under(entry.get("html_url") or "", "episodes/")
        offline = entry.get("audio_mode") == "offline"
        if not offline:
            entry["archived"] = False
            continue
        age_days = (now - entry_time(entry)).total_seconds() / 86400
        too_old = keep_days > 0 and age_days > keep_days
        missing = bool(rel) and rel not in files
        if too_old or missing or entry.get("archived"):
            entry["archived"] = True
            if rel and rel in files:
                deleted.add(rel)
        else:
            entry["archived"] = False

    def live_size():
        return sum(sz for path, sz in files.items() if path not in deleted)

    if live_size() > max_bytes:
        victims = []
        for entry in index:
            rel = rel_under(entry.get("html_url") or "", "episodes/")
            if entry.get("audio_mode") == "offline" and not entry.get("archived") and rel in files:
                victims.append(entry)
        victims.sort(key=entry_time)
        for entry in victims:
            if live_size() <= max_bytes:
                break
            rel = rel_under(entry.get("html_url") or "", "episodes/")
            entry["archived"] = True
            deleted.add(rel)
    return index, sorted(deleted)


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


def load_keep_days(path: str) -> int:
    if not os.path.exists(path):
        return 60
    try:
        import yaml
        cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
        return int(cfg.get("keep_days", 60))
    except Exception:
        return 60


def pages_base() -> str:
    return (os.environ.get("PAGES_BASE_URL") or "https://lzcyx.github.io/bilingual-podcast").strip().rstrip("/")


def _ctype(ext: str) -> str:
    return {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}.get(ext, ".jpg")


def public_entry(ep, res, base, created_at):
    slug = res.get("slug") or ep["slug"]
    show_id = ep["show_id"]
    html_rel = f"episodes/{show_id}/{slug}.html"
    cover_file = res.get("cover_file") or ""
    ext = _ctype(os.path.splitext(cover_file)[1].lower()) if cover_file else ""
    cover_rel = f"covers/{show_id}/{slug}{ext}" if ext else ""
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
        "html_url": f"{base}/{html_rel}",
        "cover_url": f"{base}/{cover_rel}" if cover_rel else "",
        "size_bytes": int(res.get("size_bytes") or 0),
        "speakers": list(res.get("speakers") or []),
        "created_at": created_at,
        "archived": False,
    }
    return entry, html_rel, cover_rel


def project_public(entry: dict) -> dict:
    out = {}
    for key in PUBLIC_FIELDS:
        if key == "archived":
            out[key] = bool(entry.get(key))
        elif key == "speakers":
            out[key] = list(entry.get(key) or [])
        elif key == "dynamic_ads":
            out[key] = bool(entry.get(key))
        elif key in ("duration_sec", "size_bytes"):
            out[key] = int(entry.get(key) or 0)
        else:
            out[key] = entry.get(key) if entry.get(key) is not None else ""
    return out


def dump_state(path, obj):
    if isinstance(obj, dict) and "items" in obj:
        obj = {**obj, "items": dict(sorted(obj["items"].items()))}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def copy_tree(src: str, dst: str):
    if not os.path.isdir(src):
        return
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel = os.path.relpath(dirpath, src)
        if rel == ".":
            rel = ""
        for name in filenames:
            source = os.path.join(dirpath, name)
            target = os.path.join(dst, rel, name) if rel else os.path.join(dst, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)


def dir_files(root: str) -> dict:
    found = {}
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            found[rel] = os.path.getsize(path)
    return found


def fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def github_output(**pairs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, val in pairs.items():
            f.write(f"{key}={val}\n")


def reject_huge(results: dict):
    for res in results.values():
        if not res.get("ok"):
            continue
        html = os.path.join(res.get("_dir") or "", res.get("html_file") or "player.html")
        if os.path.isfile(html) and os.path.getsize(html) >= MAX_FILE_BYTES:
            res["ok"] = False
            res["infra_error"] = False
            res["error"] = f"player html is {os.path.getsize(html)} bytes; GitHub Pages files must be under 100 MB"


def write_summary(text: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def notify_telegram(messages: list[str]):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        print("telegram skipped (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for text in messages:
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": "false",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            print("telegram sent", flush=True)
        except Exception as e:
            print("telegram failed:", redact(e), flush=True)


def telegram_text(entry: dict, base: str) -> str:
    minutes = int(entry.get("duration_sec") or 0) // 60
    mode = "离线" if entry.get("audio_mode") == "offline" else "在线"
    title = entry.get("title") or ""
    show = entry.get("show") or entry.get("show_id") or ""
    return (
        f"{show}\n"
        f"{title}\n"
        f"{minutes} 分钟 · {mode}\n"
        f"{entry.get('html_url') or ''}\n"
        f"列表 {base}/"
    )


def stage(args):
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    results = load_results(args.results)
    reject_huge(results)
    classified = classify_results(plan, results)
    seen, failed = classified["seen"], classified["failed"]
    ready, problems = classified["ready"], classified["problems"]
    now_s = classified["now"]
    now = datetime.now(timezone.utc)
    base = pages_base()
    keep_days = load_keep_days(args.shows)

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)
    copy_tree(args.prev, args.out)

    index = merge_index(load_json(args.index, []), load_json(os.path.join(args.prev, "index.json"), []))
    staged = []
    for ep, res in ready:
        folder = res.get("_dir") or ""
        html_path = os.path.join(folder, res.get("html_file") or "player.html")
        if not os.path.isfile(html_path):
            problems.append({
                "show_id": ep["show_id"], "guid": ep["guid"], "title": ep.get("title") or "",
                "infra": True, "error": "missing player html",
            })
            continue
        entry, html_rel, cover_rel = public_entry(ep, res, base, now_s)
        entry["size_bytes"] = os.path.getsize(html_path)
        dest_html = os.path.join(args.out, *html_rel.split("/"))
        os.makedirs(os.path.dirname(dest_html), exist_ok=True)
        shutil.copy2(html_path, dest_html)
        cover_path = os.path.join(folder, res["cover_file"]) if res.get("cover_file") else ""
        if cover_rel and cover_path and os.path.isfile(cover_path):
            dest_cover = os.path.join(args.out, *cover_rel.split("/"))
            os.makedirs(os.path.dirname(dest_cover), exist_ok=True)
            shutil.copy2(cover_path, dest_cover)
        else:
            entry["cover_url"] = ""
            cover_rel = ""
        staged.append((ep, res, entry))

    index = merge_index(index, [entry for _ep, _res, entry in staged])
    files = dir_files(args.out)
    index, deleted = apply_retention(index, files, keep_days=keep_days, now=now)
    for rel in deleted:
        path = os.path.join(args.out, *rel.split("/"))
        if os.path.isfile(path):
            os.remove(path)
            print(f"archived {rel}", flush=True)

    shell = os.path.join(ROOT, "web")
    copy_tree(shell, args.out)
    open(os.path.join(args.out, ".nojekyll"), "w", encoding="utf-8").close()
    public = [project_public(e) for e in index]
    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(public, f, ensure_ascii=False, indent=2)
        f.write("\n")

    changed = bool(staged or deleted or not os.path.isdir(args.prev))
    # A first site with only the shell (no episodes yet) is not worth a branch.
    if not staged and not deleted and not os.path.isfile(os.path.join(args.prev, "index.json")):
        changed = False
    sizes = dir_files(args.out)
    total = sum(sizes.values())
    over = total > MAX_SITE_BYTES

    if plan.get("bootstrapped_now"):
        dump_state(args.seen, seen)
    if any(not p.get("infra") for p in problems):
        dump_state(args.failed, failed)

    pending = {
        "base": base,
        "now": now_s,
        "index": public,
        "published": [
            {
                "key": ep["key"], "slug": res.get("slug") or ep.get("slug"),
                "show_id": ep["show_id"], "guid": ep["guid"],
                "title": ep.get("title") or "", "pub_date": ep.get("pub_date") or "",
                "entry": project_public(entry),
            }
            for ep, res, entry in staged
            if not entry.get("archived")
        ],
    }
    os.makedirs(os.path.dirname(args.pending) or ".", exist_ok=True)
    dump_state(args.pending, pending)

    lines = ["## 发布到 GitHub Pages", ""]
    if plan.get("bootstrapped_now"):
        lines.append(f"第一次运行：已把 **{len(plan['seen']['items'])}** 期现有节目标为已处理（没有补做）。")
    lines.append(f"本次上架 **{len(staged)}** 期。归档删除 **{len(deleted)}** 个离线文件。问题 **{len(problems)}**。")
    lines.append(f"站点大小 **{fmt_bytes(total)}**（上限 1 GB，单文件 < 100 MB）。保留天数 {keep_days}。")
    if over:
        lines.append("警告：删完过期离线版之后站点仍然超过 1 GB。")
    lines.append(f"站点根：{base}/")
    for _ep, _res, entry in staged:
        lines.append(
            f"- `{entry['show_id']}` {entry['audio_mode']} "
            f"{fmt_bytes(entry['size_bytes'])} — {str(entry['title']).replace('|', '/')}"
        )
        lines.append(f"  {entry['html_url']}")
    for prob in problems:
        kind = "配置问题，不计入失败次数" if prob.get("infra") else (
            f"第 {prob.get('attempts')} 次" + ("，已放弃" if prob.get("abandoned") else ""))
        lines.append(f"- 失败 `{prob.get('show_id')}` {prob.get('title')}: {kind} — {prob.get('error')}")
    write_summary("\n".join(lines) + "\n")
    github_output(changed="true" if changed else "false", problems="true" if problems or over else "false",
                  site_bytes=str(total))
    print(f"staged={len(staged)} archived={len(deleted)} problems={len(problems)} "
          f"changed={changed} site={fmt_bytes(total)}")


def ack(args):
    pending = load_json(args.pending, {})
    published = pending.get("published") or []
    if not published:
        print("nothing to ack")
        return
    seen = load_json(args.seen, {"bootstrapped": True, "items": {}})
    failed = load_json(args.failed, {"items": {}})
    now_s = pending.get("now") or datetime.now(timezone.utc).isoformat()
    for item in published:
        ep = {
            "key": item["key"], "show_id": item.get("show_id"), "guid": item.get("guid"),
            "title": item.get("title") or "", "pub_date": item.get("pub_date") or "",
        }
        mark_published(seen, failed, ep, now_s, item.get("slug") or "")
    dump_state(args.seen, seen)
    dump_state(args.failed, failed)
    dump_state(args.index, pending.get("index") or [])
    base = pending.get("base") or pages_base()
    messages = [telegram_text(item["entry"], base) for item in published if item.get("entry")]
    notify_telegram(messages)
    print(f"acked {len(published)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--plan", default="run/plan/plan.json")
    common.add_argument("--results", default="run/results")
    common.add_argument("--seen", default="state/seen.json")
    common.add_argument("--failed", default="state/failed.json")
    common.add_argument("--index", default="state/index.json")
    common.add_argument("--shows", default="shows.yaml")
    common.add_argument("--prev", default="site-prev")
    common.add_argument("--out", default="site-dist")
    common.add_argument("--pending", default="run/pending_publish.json")
    sub.add_parser("stage", parents=[common])
    sub.add_parser("ack", parents=[common])
    args = ap.parse_args()
    if args.cmd == "ack":
        ack(args)
    else:
        stage(args)


if __name__ == "__main__":
    main()

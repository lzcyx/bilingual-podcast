#!/usr/bin/env python3
"""Check show RSS feeds against state/seen.json and write a build plan.

First run (seen.bootstrapped == false) marks every current episode as seen and
does not queue them. Pass --show-id and --guid (or SHOW_ID / EPISODE_GUID) to
backfill one episode; that guid is left unseen and queued even on the first run.

  python scripts/check_feeds.py --list 5
  python scripts/check_feeds.py --show-id ignuk --guid <guid>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feeds  # noqa: E402


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_github_output(plan):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    delim = "EOF_" + uuid.uuid4().hex
    matrix = json.dumps(plan["queue"], ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"has_work={'true' if plan['queue'] else 'false'}\n")
        f.write(f"bootstrap={'true' if plan['bootstrapped_now'] else 'false'}\n")
        f.write(f"matrix<<{delim}\n{matrix}\n{delim}\n")


def write_summary(plan):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Feed check", ""]
    if plan["bootstrapped_now"]:
        n = len(plan["seen"]["items"])
        lines.append(f"First run: marked **{n}** existing episodes as seen (no backfill).")
    lines.append(f"Queued **{len(plan['queue'])}** episode(s).")
    if plan["queue"]:
        lines.append("")
        lines.append("| show | date | min | title |")
        lines.append("|---|---|---:|---|")
        for ep in plan["queue"]:
            mins = "" if ep.get("duration_sec") is None else f"{ep['duration_sec'] / 60:.0f}"
            title = (ep.get("title") or "").replace("|", "/")
            lines.append(f"| `{ep['show_id']}` | {ep.get('pub_date', '')[:10]} | {mins} | {title} |")
    short = [s for s in plan["skipped"] if s.get("reason") in ("short", "video", "abandoned")]
    if short:
        lines.append("")
        lines.append(f"Skipped {len(short)} item(s) (short / video / abandoned).")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_list(shows, items_by_show, n, only):
    for show in shows:
        if only and show["id"] != only:
            continue
        rows = items_by_show.get(show["id"]) or []
        print(f"\n== {show['id']}  {show.get('name') or ''}  ({len(rows)} in feed) ==")
        for it in rows[:n]:
            dur = it.get("duration_sec")
            mins = f"{dur / 60:.0f}m" if dur else "?"
            flag = " video" if it.get("video") else ""
            print(f"{it.get('pub_date', '')[:10]}  {mins:>5}{flag}  {it.get('episode') or '-':>5}  {it['guid']}")
            print(f"    {it.get('title')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shows", default="shows.yaml")
    ap.add_argument("--seen", default="state/seen.json")
    ap.add_argument("--failed", default="state/failed.json")
    ap.add_argument("--index", default="state/index.json")
    ap.add_argument("--out", default="run/plan.json")
    ap.add_argument("--show-id", default=os.environ.get("SHOW_ID", ""))
    ap.add_argument("--guid", default=os.environ.get("EPISODE_GUID", ""))
    ap.add_argument("--list", type=int, default=0, help="print N latest items per show and exit")
    a = ap.parse_args()

    with open(a.shows, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    shows = cfg.get("shows") or []
    if not shows:
        sys.exit("shows.yaml has no shows")
    for s in shows:
        if not re_id(s.get("id")) or not s.get("rss"):
            sys.exit(f"bad show entry: {s!r}")
    min_dur = int(cfg.get("min_duration_sec") or 600)

    items_by_show = {}
    for show in shows:
        if a.list and a.show_id and show["id"] != a.show_id.strip():
            continue
        print(f"fetch {show['id']} {show['rss']}", flush=True)
        try:
            raw = feeds.download(show["rss"])
        except Exception as e:
            sys.exit(f"{show['id']}: failed to download RSS: {e}")
        items = feeds.parse_rss(raw, show["id"], show.get("name") or show["id"], show["rss"])
        items_by_show[show["id"]] = items
        print(f"  {len(items)} items", flush=True)

    if a.list:
        print_list(shows, items_by_show, a.list, a.show_id.strip())
        return

    seen = load_json(a.seen, {"bootstrapped": False, "items": {}})
    failed = load_json(a.failed, {"items": {}})
    index = load_json(a.index, [])
    try:
        plan = feeds.build_plan(
            shows, seen, failed, index, items_by_show,
            show_id=a.show_id, guid=a.guid, min_duration_sec=min_dur)
    except feeds.FeedError as e:
        sys.exit(str(e))

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write("\n")
    write_github_output(plan)
    write_summary(plan)
    print(f"bootstrapped_now={plan['bootstrapped_now']} queued={len(plan['queue'])} "
          f"skipped={len(plan['skipped'])} seen={len(plan['seen']['items'])}")
    for ep in plan["queue"]:
        print(f"  QUEUE {ep['show_id']} {ep.get('pub_date', '')[:10]} {ep['guid']}  {ep['title']}")
    print(f"wrote {a.out}")


def re_id(value) -> bool:
    import re
    return bool(value) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", value) is not None


if __name__ == "__main__":
    main()

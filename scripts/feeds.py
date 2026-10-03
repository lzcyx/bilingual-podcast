#!/usr/bin/env python3
"""RSS parsing and the new-episode plan (no network unless download() is called)."""
from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "bilingual-podcast/1.0 (+https://github.com/lzcyx/bilingual-podcast)"}


class FeedError(Exception):
    pass


def item_key(show_id: str, guid: str) -> str:
    return f"{show_id}\t{guid}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag else ""


def _children(el, name: str):
    return [c for c in list(el) if _local(c.tag) == name]


def _text(el, name: str) -> str:
    for c in _children(el, name):
        if c.text and c.text.strip():
            return re.sub(r"\s+", " ", c.text).strip()
    return ""


def _enclosure(el):
    for c in _children(el, "enclosure"):
        url = (c.get("url") or "").strip()
        if url:
            return url, (c.get("type") or "").strip()
    return "", ""


def parse_duration(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if ":" not in s:
        try:
            return int(float(s))
        except ValueError:
            return None
    parts = s.split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return None


def parse_pubdate(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def episode_number(title: str) -> str:
    m = re.search(r"(?:episode|ep\.?)\s*#?\s*(\d{1,5})\b", title or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{2,4})\s*[:：]", title or "")
    return m.group(1) if m else ""


def is_video(enc_type: str, url: str) -> bool:
    t = (enc_type or "").lower().split(";")[0].strip()
    if t.startswith("video/"):
        return True
    if t.startswith("audio/"):
        return False
    path = urllib.parse.urlparse(url or "").path.lower()
    return path.endswith((".mp4", ".mov", ".m4v", ".webm", ".mkv"))


def parse_rss(xml_bytes: bytes | str, show_id: str, show_name: str, rss_url: str) -> list[dict]:
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FeedError(f"{show_id}: invalid RSS ({e})") from e
    channel = next((el for el in root.iter() if _local(el.tag) == "channel"), None)
    if channel is None:
        raise FeedError(f"{show_id}: RSS has no channel")
    feed_name = ""
    for c in list(channel):
        if _local(c.tag) == "title" and c.text:
            feed_name = re.sub(r"\s+", " ", c.text).strip()
            break
    name = show_name or feed_name or show_id
    items = []
    for it in _children(channel, "item"):
        title = _text(it, "title")
        audio_url, enc_type = _enclosure(it)
        guid = _text(it, "guid") or audio_url
        pub = parse_pubdate(_text(it, "pubDate"))
        ep = _text(it, "episode") or episode_number(title)
        items.append({
            "show_id": show_id,
            "show": name,
            "rss": rss_url,
            "guid": guid,
            "title": title,
            "episode": ep,
            "page_url": _text(it, "link"),
            "audio_url": audio_url,
            "enclosure_type": enc_type,
            "pub_date": pub.isoformat() if pub else "",
            "duration_sec": parse_duration(_text(it, "duration")),
            "video": is_video(enc_type, audio_url),
        })
    return items


def download(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def slugify(title: str, pub_date: str) -> str:
    day = ""
    if pub_date:
        try:
            day = datetime.fromisoformat(pub_date).astimezone(CST).strftime("%Y-%m-%d")
        except ValueError:
            day = pub_date[:10]
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:70].strip("-") or "episode"
    return f"{day}-{base}" if day else base


def artifact_name(show_id: str, guid: str) -> str:
    digest = hashlib.sha1(guid.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^A-Za-z0-9_-]", "", show_id)[:24] or "show"
    return f"ep-{safe}-{digest}"


def _seen_record(it: dict, reason: str, now: datetime) -> dict:
    return {
        "show_id": it["show_id"],
        "guid": it["guid"],
        "title": it.get("title") or "",
        "pub_date": it.get("pub_date") or "",
        "reason": reason,
        "marked_at": now.isoformat(),
    }


def _brief(it: dict) -> dict:
    return {"show_id": it["show_id"], "guid": it.get("guid") or "", "title": it.get("title") or ""}


def _unique_slug(it: dict, used: set) -> str:
    base = slugify(it.get("title") or "episode", it.get("pub_date") or "")
    slug = base
    n = 2
    while (it["show_id"], slug) in used:
        slug = f"{base}-{n}"
        n += 1
    used.add((it["show_id"], slug))
    return slug


def _index_slugs(index) -> set:
    used = set()
    for ep in index or []:
        url = ep.get("html_url") or ""
        m = re.search(r"episodes/([^/]+)/([^/]+)\.html$", url)
        if m:
            used.add((m.group(1), urllib.parse.unquote(m.group(2))))
        elif ep.get("show_id") and ep.get("slug"):
            used.add((ep["show_id"], ep["slug"]))
    return used


def _published_slug(index, seen, show_id: str, guid: str) -> str:
    """Reuse the published URL when manually rebuilding an existing GUID."""
    for ep in index or []:
        if ep.get("show_id") != show_id or ep.get("guid") != guid:
            continue
        if ep.get("slug"):
            return str(ep["slug"])
        url = ep.get("html_url") or ""
        m = re.search(r"episodes/([^/]+)/([^/]+)\.html$", url)
        if m and m.group(1) == show_id:
            return urllib.parse.unquote(m.group(2))
    rec = ((seen or {}).get("items") or {}).get(item_key(show_id, guid)) or {}
    return str(rec.get("slug") or "")


def build_plan(shows, seen, failed, index, items_by_show, *, show_id="", guid="",
               min_duration_sec=600, now=None) -> dict:
    """Decide bootstrap / queue / skip. `seen` and `failed` are not mutated in place."""
    now = now or datetime.now(timezone.utc)
    seen = {
        "bootstrapped": bool((seen or {}).get("bootstrapped")),
        "items": dict((seen or {}).get("items") or {}),
    }
    failed = {"items": dict((failed or {}).get("items") or {})}
    show_id = (show_id or "").strip()
    guid = (guid or "").strip()
    if bool(show_id) ^ bool(guid):
        raise FeedError("manual backfill needs both show_id and guid")

    known = {s["id"] for s in shows}
    flat = []
    for show in shows:
        flat.extend(items_by_show.get(show["id"]) or [])

    forced = None
    if show_id:
        if show_id not in known:
            raise FeedError(f"unknown show_id {show_id!r} — add it to shows.yaml first")
        matches = [it for it in flat if it["show_id"] == show_id and it["guid"] == guid]
        if not matches:
            raise FeedError(f"guid not in the current RSS for {show_id}: {guid}")
        forced = matches[0]
        if not forced.get("audio_url"):
            raise FeedError(f"{show_id} item has no audio enclosure: {guid}")
        if forced.get("video"):
            raise FeedError(f"{show_id} item is video-only, refusing to build: {forced.get('title')}")

    bootstrapped_now = False
    if not seen["bootstrapped"]:
        for it in flat:
            if not it.get("guid"):
                continue
            if forced and it["show_id"] == forced["show_id"] and it["guid"] == forced["guid"]:
                continue
            key = item_key(it["show_id"], it["guid"])
            seen["items"].setdefault(key, _seen_record(it, "bootstrap", now))
        seen["bootstrapped"] = True
        bootstrapped_now = True

    used = _index_slugs(index)
    skipped = []
    queue = []

    def consider(it, force=False):
        if not it.get("guid"):
            skipped.append({**_brief(it), "reason": "no-guid"})
            return
        key = item_key(it["show_id"], it["guid"])
        if not force and key in seen["items"]:
            return
        if not force:
            rec = failed["items"].get(key) or {}
            if rec.get("abandoned") or int(rec.get("attempts") or 0) >= 3:
                skipped.append({**_brief(it), "reason": "abandoned"})
                return
        if not it.get("audio_url"):
            skipped.append({**_brief(it), "reason": "no-audio"})
            return
        if it.get("video"):
            skipped.append({**_brief(it), "reason": "video"})
            return
        dur = it.get("duration_sec")
        if not force and dur is not None and dur < min_duration_sec:
            skipped.append({**_brief(it), "reason": "short", "duration_sec": dur})
            return
        # A forced rebuild of an already-published GUID must keep its public URL.
        # Only allocate a new unique slug when this GUID has never been published.
        slug = _published_slug(index, seen, it["show_id"], it["guid"]) if force else ""
        if slug:
            used.add((it["show_id"], slug))
        else:
            slug = _unique_slug(it, used)
        queue.append({
            **it,
            "slug": slug,
            "artifact": artifact_name(it["show_id"], it["guid"]),
            "key": key,
            "force": bool(force),
        })

    if forced:
        consider(forced, force=True)
    else:
        fresh = []
        for it in flat:
            if not it.get("guid"):
                continue
            if item_key(it["show_id"], it["guid"]) in seen["items"]:
                continue
            fresh.append(it)
        fresh.sort(key=lambda it: it.get("pub_date") or "", reverse=True)
        for it in fresh:
            consider(it, force=False)

    return {
        "bootstrapped_now": bootstrapped_now,
        "seen": seen,
        "failed": failed,
        "queue": queue,
        "skipped": skipped,
        "generated_at": now.isoformat(),
    }

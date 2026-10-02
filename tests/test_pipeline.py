#!/usr/bin/env python3
"""Offline tests for feed planning, edit schema, publish bookkeeping, and tr_check --only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.feeds import build_plan, item_key, parse_duration, parse_rss, slugify  # noqa: E402
from scripts.llm.schema import (  # noqa: E402
    chapter_bounds, fallback_chapters, validate_chapters, validate_edits, validate_fixes, validate_speakers,
)
from scripts.publish import classify_results, merge_index  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>IGN UK Podcast</title>
<item>
  <title>IGN UK Podcast 867: The Big Silent Hill: Townfall Chat</title>
  <guid>b394abe4-b8ee-11f1-bb43-0b13ebfe32cf</guid>
  <pubDate>Fri, 25 Sep 2026 14:45:00 -0000</pubDate>
  <itunes:duration>3512</itunes:duration>
  <enclosure url="https://example.test/867.mp3" type="audio/mpeg"/>
</item>
<item>
  <title>Short news</title>
  <guid>short-1</guid>
  <pubDate>Thu, 24 Sep 2026 10:00:00 -0000</pubDate>
  <itunes:duration>180</itunes:duration>
  <enclosure url="https://example.test/short.mp3" type="audio/mpeg"/>
</item>
<item>
  <title>Video recap</title>
  <guid>vid-1</guid>
  <enclosure url="https://example.test/v.mp4" type="video/mp4"/>
  <itunes:duration>1200</itunes:duration>
</item>
<item>
  <title>Brand new hour</title>
  <guid>new-1</guid>
  <pubDate>Fri, 02 Oct 2026 12:00:00 -0000</pubDate>
  <itunes:duration>1:05:00</itunes:duration>
  <enclosure url="https://example.test/new.mp3" type="audio/mpeg"/>
</item>
</channel></rss>
"""

SHOWS = [{"id": "ignuk", "name": "IGN UK Podcast", "rss": "https://example.test/rss"}]


def items():
    return {"ignuk": parse_rss(RSS, "ignuk", "IGN UK Podcast", SHOWS[0]["rss"])}


class FeedTests(unittest.TestCase):
    def test_parse_duration_and_slug_timezone(self):
        self.assertEqual(parse_duration("3512"), 3512)
        self.assertEqual(parse_duration("1:05:00"), 3900)
        self.assertEqual(parse_duration("12:03"), 723)
        # 20:30 UTC is 04:30 next day in Asia/Shanghai
        self.assertTrue(slugify("Hello World!", "2026-09-25T20:30:00+00:00").startswith("2026-09-26-hello-world"))
        self.assertTrue(slugify("IGN UK Podcast 867: Townfall", "2026-09-25T14:45:00+00:00").startswith("2026-09-25-"))

    def test_rss_fields(self):
        rows = items()["ignuk"]
        by = {r["guid"]: r for r in rows}
        self.assertEqual(by["b394abe4-b8ee-11f1-bb43-0b13ebfe32cf"]["duration_sec"], 3512)
        self.assertEqual(by["b394abe4-b8ee-11f1-bb43-0b13ebfe32cf"]["episode"], "867")
        self.assertTrue(by["vid-1"]["video"])
        self.assertFalse(by["new-1"]["video"])
        self.assertEqual(by["new-1"]["duration_sec"], 3900)

    def test_bootstrap_does_not_queue_history(self):
        plan = build_plan(SHOWS, {"bootstrapped": False, "items": {}}, {"items": {}}, [], items())
        self.assertTrue(plan["bootstrapped_now"])
        self.assertEqual(plan["queue"], [])
        self.assertIn(item_key("ignuk", "short-1"), plan["seen"]["items"])
        self.assertEqual(plan["seen"]["items"][item_key("ignuk", "b394abe4-b8ee-11f1-bb43-0b13ebfe32cf")]["reason"], "bootstrap")

    def test_forced_guid_is_queued_and_not_marked_seen(self):
        plan = build_plan(
            SHOWS, {"bootstrapped": False, "items": {}}, {"items": {}}, [], items(),
            show_id="ignuk", guid="b394abe4-b8ee-11f1-bb43-0b13ebfe32cf")
        self.assertEqual([q["guid"] for q in plan["queue"]], ["b394abe4-b8ee-11f1-bb43-0b13ebfe32cf"])
        self.assertNotIn(item_key("ignuk", "b394abe4-b8ee-11f1-bb43-0b13ebfe32cf"), plan["seen"]["items"])
        self.assertIn(item_key("ignuk", "short-1"), plan["seen"]["items"])
        self.assertTrue(plan["queue"][0]["artifact"].startswith("ep-ignuk-"))

    def test_later_run_queues_only_new_long_audio(self):
        first = build_plan(SHOWS, {"bootstrapped": False, "items": {}}, {"items": {}}, [], items())
        seen = first["seen"]
        # These three were not in the feed at bootstrap time.
        for guid in ("new-1", "short-1", "vid-1"):
            del seen["items"][item_key("ignuk", guid)]
        plan = build_plan(SHOWS, seen, {"items": {}}, [], items())
        self.assertFalse(plan["bootstrapped_now"])
        self.assertEqual([q["guid"] for q in plan["queue"]], ["new-1"])
        reasons = {s["guid"]: s["reason"] for s in plan["skipped"]}
        self.assertEqual(reasons.get("short-1"), "short")
        self.assertEqual(reasons.get("vid-1"), "video")

    def test_abandoned_is_not_retried(self):
        seen = {"bootstrapped": True, "items": {
            item_key("ignuk", "b394abe4-b8ee-11f1-bb43-0b13ebfe32cf"): {"reason": "bootstrap"},
        }}
        failed = {"items": {item_key("ignuk", "new-1"): {"attempts": 3, "abandoned": True}}}
        plan = build_plan(SHOWS, seen, failed, [], items())
        self.assertEqual(plan["queue"], [])
        self.assertTrue(any(s["guid"] == "new-1" and s["reason"] == "abandoned" for s in plan["skipped"]))

    def test_missing_guid_errors(self):
        with self.assertRaises(Exception):
            build_plan(SHOWS, {"bootstrapped": True, "items": {}}, {"items": {}}, [], items(),
                       show_id="ignuk", guid="does-not-exist")


class SchemaTests(unittest.TestCase):
    def test_fixes_and_speakers(self):
        fixes, warns = validate_fixes([[r"\bSid Schuman\b", "Sid Shuman"], ["(", "x"], "nope"])
        self.assertEqual(fixes, [[r"\bSid Schuman\b", "Sid Shuman"]])
        self.assertTrue(warns)
        speakers, _ = validate_speakers({"S0": {"name": "Matt"}, "S1": "广告", "nope": "x"})
        self.assertEqual(speakers["S0"]["name"], "Matt")
        self.assertEqual(speakers["S1"]["name"], "Ad")
        self.assertNotIn("nope", speakers)

    def test_edits_drop_unknown_and_bad_splits(self):
        raw = {"01-000": {"id": "01-000", "en": "one two three", "w": [[0, 1, "one"], [1, 2, "two"], [2, 3, "three"]]}}
        edits, warns = validate_edits({
            "01-000": {"drop": True},
            "missing": {"en": "nope"},
            "01-000-ignored": {"split": [[1, "only"]]},
        }, raw)
        self.assertEqual(edits, {"01-000": {"drop": True}})
        self.assertTrue(any("unknown" in w for w in warns))

    def test_chapters_scale_and_fallback(self):
        self.assertEqual(chapter_bounds(3512), (8, 20))
        lines = [{"i": i, "s": i * 10, "e": i * 10 + 9} for i in range(6)]
        ch = fallback_chapters(lines, 4)
        self.assertEqual(ch[0]["i"], 0)
        self.assertTrue(all(b["i"] > a["i"] for a, b in zip(ch, ch[1:])))
        good, _ = validate_chapters({"chapters": [
            {"i": 0, "tr": "开场", "en": "Intro"},
            {"i": 3, "tr": "中段", "en": "Middle"},
        ]}, 6)
        self.assertEqual([c["i"] for c in good], [0, 3])
        with self.assertRaises(ValueError):
            validate_chapters([{"i": 2, "tr": "x", "en": "y"}], 6)


class PublishTests(unittest.TestCase):
    def test_attempts_infra_and_index_order(self):
        ep = {"show_id": "ignuk", "guid": "g", "title": "T", "pub_date": "2026-09-25T00:00:00+00:00",
              "key": item_key("ignuk", "g"), "slug": "s"}
        plan = {"seen": {"bootstrapped": True, "items": {}}, "failed": {"items": {}}, "queue": [ep]}
        out = classify_results(plan, {ep["key"]: {"ok": False, "error": "boom", "infra_error": False}},
                               now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(out["failed"]["items"][ep["key"]]["attempts"], 1)
        self.assertFalse(out["failed"]["items"][ep["key"]]["abandoned"])
        plan["failed"] = out["failed"]
        out2 = classify_results(plan, {}, now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertEqual(out2["failed"]["items"][ep["key"]]["attempts"], 2)
        plan["failed"] = out2["failed"]
        out3 = classify_results(plan, {ep["key"]: {"ok": False, "infra_error": True, "error": "missing DEEPSEEK_API_KEY"}})
        self.assertEqual(out3["failed"]["items"][ep["key"]]["attempts"], 2)
        self.assertTrue(out3["problems"][0]["infra"])
        plan["failed"] = out3["failed"]
        out4 = classify_results(plan, {})
        self.assertTrue(out4["failed"]["items"][ep["key"]]["abandoned"])
        index = merge_index(
            [{"show_id": "opp", "guid": "z", "pub_date": "2026-10-01T00:00:00+00:00", "title": "old", "created_at": "a"}],
            [{"show_id": "ignuk", "guid": "g", "pub_date": "2026-09-01T00:00:00+00:00", "title": "older", "created_at": "b"},
             {"show_id": "opp", "guid": "z", "pub_date": "2026-10-01T00:00:00+00:00", "title": "new", "created_at": "c"}],
        )
        self.assertEqual([e["guid"] for e in index], ["z", "g"])
        self.assertEqual(index[0]["title"], "new")
        self.assertEqual(index[0]["created_at"], "a")


class TrCheckTests(unittest.TestCase):
    def test_only_does_not_require_other_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "tr"))
            json.dump([
                {"i": 0, "s": 0, "e": 1, "en": "Hello there friends today."},
                {"i": 1, "s": 1, "e": 2, "en": "More words follow now."},
            ], open(os.path.join(td, "lines.json"), "w"))
            open(os.path.join(td, "tr", "b01.src.txt"), "w", encoding="utf-8").write(
                "# block\n0-1\tHello there friends today. | More words follow now.\n")
            zh = os.path.join(td, "tr", "b01.zh.txt")
            open(zh, "w", encoding="utf-8").write("0-1\t你好朋友们。｜后面还有。\n")
            script = os.path.join(ROOT, "scripts", "tr_check.py")
            ok = subprocess.run([sys.executable, script, "--workdir", td, "--only", "b01"], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertFalse(os.path.exists(os.path.join(td, "cues.json")))
            open(zh, "w", encoding="utf-8").write("0-1\t只有一段\n")
            bad = subprocess.run([sys.executable, script, "--workdir", td, "--only", "b01"], capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            open(zh, "w", encoding="utf-8").write("0-1\t你好朋友们。｜后面还有。\n")
            full = subprocess.run([sys.executable, script, "--workdir", td], capture_output=True, text=True)
            self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
            cues = json.load(open(os.path.join(td, "cues.json"), encoding="utf-8"))
            self.assertEqual([c["tr"] for c in cues], ["你好朋友们。", "后面还有。"])


if __name__ == "__main__":
    unittest.main()

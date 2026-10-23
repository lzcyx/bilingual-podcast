#!/usr/bin/env python3
"""Cheap offline preflight for publishing. No LLM, audio download, or network."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import publish


class PublishPreflightTests(unittest.TestCase):
    def test_multi_provider_usage_no_legacy_price_note(self):
        results = {
            "a": {"report": {"usage": {
                "calls": 2, "prompt_tokens": 100, "completion_tokens": 30,
                "cost_cny": 0.1, "providers": ["gpt", "deepseek"],
            }}},
            "b": {"report": {"usage": {
                "calls": 1, "prompt_tokens": 20, "completion_tokens": 4,
                "providers": ["grok"],
            }}},
        }
        line = publish._run_usage_line(results)
        self.assertIn("LLM：", line)
        self.assertIn("120 in / 34 out", line)
        self.assertIn("deepseek/gpt/grok", line)
        self.assertIn("¥0.1000", line)

    def make_fixture(self, td, *, replay=False):
        def write(path, value):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(value, f)
        key = "unlocked\tguid-one"
        episode = {
            "key": key, "show_id": "unlocked", "guid": "guid-one",
            "slug": "test-episode", "show": "IGN Unlocked",
            "title": "Test episode", "pub_date": "2026-10-22T00:00:00+00:00",
        }
        source = os.path.join(td, "results", "ep-unlocked")
        os.makedirs(source, exist_ok=True)
        write(os.path.join(source, "result.json"), {
            "ok": True, "key": key, "show_id": "unlocked", "guid": "guid-one",
            "slug": "test-episode", "title": "Test episode",
            "html_file": "player.html", "audio_mode": "online",
            "report": {"usage": {"calls": 1, "prompt_tokens": 10,
                                  "completion_tokens": 2, "providers": ["gpt"]}},
        })
        with open(os.path.join(source, "player.html"), "w", encoding="utf-8") as f:
            f.write("<!doctype html><title>Player</title>")
        plan = {
            "seen": {"bootstrapped": True, "items": {}},
            "failed": {"items": {}}, "queue": [episode], "bootstrapped_now": False,
        }
        write(os.path.join(td, "plan.json"), plan)
        write(os.path.join(td, "seen.json"), {"bootstrapped": True, "items": {
            "unlocked\tolder": {"reason": "published"},
        }})
        write(os.path.join(td, "failed.json"), {"items": {}})
        write(os.path.join(td, "index.json"), [])
        os.makedirs(os.path.join(td, "prev"), exist_ok=True)
        write(os.path.join(td, "prev", "index.json"), [])
        return SimpleNamespace(
            plan=os.path.join(td, "plan.json"),
            results=os.path.join(td, "results"),
            seen=os.path.join(td, "seen.json"),
            failed=os.path.join(td, "failed.json"),
            index=os.path.join(td, "index.json"),
            prev=os.path.join(td, "prev"),
            out=os.path.join(td, "site-dist"),
            pending=os.path.join(td, "pending.json"),
            shows=os.path.join(td, "no-shows.yaml"),
            replay=replay,
        )

    def test_accounting_exception_does_not_block_publish(self):
        with tempfile.TemporaryDirectory() as td:
            args = self.make_fixture(td, replay=True)
            with patch.object(publish, "_run_usage_line",
                              side_effect=RuntimeError("pricing schema changed")):
                publish.stage(args)
            publish.validate(args)
            self.assertTrue(os.path.isfile(os.path.join(
                args.out, "episodes", "unlocked", "test-episode.html")))
            pending = publish.load_json(args.pending, {})
            self.assertEqual(len(pending["published"]), 1)
            self.assertNotIn("unlocked\tguid-one",
                             publish.load_json(args.seen, {})["items"])

    def test_validate_detects_missing_player(self):
        with tempfile.TemporaryDirectory() as td:
            args = self.make_fixture(td)
            publish.stage(args)
            os.remove(os.path.join(args.out, "episodes", "unlocked", "test-episode.html"))
            with self.assertRaisesRegex(ValueError, "missing or oversized"):
                publish.validate(args)

    def test_replay_skips_already_published(self):
        with tempfile.TemporaryDirectory() as td:
            args = self.make_fixture(td, replay=True)
            seen = publish.load_json(args.seen, {})
            seen["items"]["unlocked\tguid-one"] = {"reason": "published"}
            publish.dump_state(args.seen, seen)
            publish.stage(args)
            publish.validate(args)
            self.assertEqual(publish.load_json(args.pending, {})["published"], [])
            self.assertIn("unlocked\tolder",
                          publish.load_json(args.seen, {})["items"])


if __name__ == "__main__":
    unittest.main()

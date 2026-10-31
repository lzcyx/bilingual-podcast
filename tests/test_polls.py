#!/usr/bin/env python3
"""Regression tests for official poll URLs in podcast show notes (no network)."""
import unittest

from scripts.polls import find_poll_url, normalize_url
from scripts.feeds import parse_rss


class PollLinkTests(unittest.TestCase):
    def test_plain_text_and_html_vote_links(self):
        self.assertEqual(
            find_poll_url("Vote in our listener poll: https://example.org/weekly/42"),
            "https://example.org/weekly/42")
        self.assertEqual(
            find_poll_url('<p>New episode!</p><a href="https://example.com/vote/42">Vote now</a>'),
            "https://example.com/vote/42")

    def test_direct_poll_hosts_and_path(self):
        self.assertEqual(
            find_poll_url("Find us at https://strawpoll.com/abc123"),
            "https://strawpoll.com/abc123")
        self.assertEqual(
            find_poll_url("https://example.com/polls/episode-4"),
            "https://example.com/polls/episode-4")

    def test_no_unrelated_links_and_unlinked_poll_mentions(self):
        self.assertEqual(find_poll_url("Our poll is live! Follow our podcast."), "")
        self.assertEqual(find_poll_url("Visit https://example.com/home for news."), "")
        self.assertEqual(find_poll_url("Vote later. Ad choices https://megaphone.fm/adchoices"), "")
        self.assertEqual(normalize_url("javascript:alert(1)"), "")
        self.assertEqual(normalize_url("https://me@example.org/vote"), "")

    def test_rss_poll_from_show_notes(self):
        raw = """<rss><channel><title>Podcast</title>
          <item><title>Episode 3</title><guid>3</guid>
            <link>https://example.org/episodes/3</link>
            <enclosure url="https://example.org/3.mp3" type="audio/mpeg"/>
            <description><![CDATA[
              <p>Let's play!</p>
              <p><a href="https://example.org/surveys/episode3">Participate in the survey</a></p>
            ]]></description>
          </item>
        </channel></rss>"""
        items = parse_rss(raw, "test", "Test Show", "https://example.org/feed")
        self.assertEqual(items[0]["poll_url"], "https://example.org/surveys/episode3")
        self.assertEqual(items[0]["show"], "Test Show")


if __name__ == "__main__":
    unittest.main()

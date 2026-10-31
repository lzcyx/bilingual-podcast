"""Find an explicitly linked episode poll in RSS show notes or an episode page.

No LLM/web search: a poll mention without a verifiable URL is not a link.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
POLL_WORDS = re.compile(r'\b(?:polls?|vot(?:e|es|ing)|survey|surveys|ballot)\b|投票|问卷|参与调查', re.I)
DIRECT_PATH = re.compile(r'/(?:polls?|votes?|surveys?|forms?)(?:/|$|[-?])', re.I)
DIRECT_HOSTS = ('poll.fm', 'strawpoll.com', 'poll-maker.com', 'forms.gle',
                'surveymonkey.com', 'typeform.com', 'pollunit.com')
IGNORE_HOSTS = ('megaphone.fm',)
IGNORE_PATH = re.compile(r'(?:adchoices|privacy-policy|terms-of-service)', re.I)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.active_url = ''
        self.active_text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.active_url = dict(attrs).get('href') or ''
            self.active_text = []

    def handle_data(self, data):
        if self.active_url:
            self.active_text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.active_url:
            self.links.append((self.active_url, ' '.join(self.active_text)))
            self.active_url = ''
            self.active_text = []


def normalize_url(value: str) -> str:
    url = html.unescape(str(value or '')).strip().rstrip('.,;!?)]}>')
    if not url or len(url) > 2048:
        return ''
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            return ''
        if parts.username or parts.password:
            return ''
        return url
    except ValueError:
        return ''


def _is_poll_destination(url: str) -> bool:
    parts = urlsplit(url)
    hostname = (parts.hostname or '').lower()
    path = parts.path.lower()
    if any(hostname == h or hostname.endswith('.' + h) for h in IGNORE_HOSTS):
        return False
    if IGNORE_PATH.search(path):
        return False
    # A generic home page is not a link to this episode's poll.
    if path in ('', '/') and not parts.query:
        return False
    return bool(DIRECT_PATH.search(path) or any(
        hostname == h or hostname.endswith('.' + h) for h in DIRECT_HOSTS))


def find_poll_url(*sources: str) -> str:
    """Return a direct HTTP(S) poll/vote link; empty when the text only mentions a poll."""
    best = ('', 0)
    for source in sources:
        source = html.unescape(source or '')
        if not source:
            continue
        parser = Links()
        parser.feed(source)
        for href, label in parser.links:
            url = normalize_url(href)
            if not url:
                continue
            score = 3 if _is_poll_destination(url) else (2 if POLL_WORDS.search(label) else 0)
            if score > best[1]:
                best = (url, score)
        # Plain-text URLs (or Markdown-style [vote here](https://...)).
        for match in URL_RE.finditer(source):
            url = normalize_url(match.group())
            if not url:
                continue
            prefix = re.sub('<[^>]*>', ' ', source[max(0, match.start() - 100):match.start()])
            score = 3 if _is_poll_destination(url) else (1 if POLL_WORDS.search(prefix) else 0)
            if score > best[1]:
                best = (url, score)
    return best[0]

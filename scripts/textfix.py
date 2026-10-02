"""Shared text clean-up: generic whitespace/punctuation repair + optional per-episode regex fixes.

fixes.json = [["\\bSid Schuman\\b", "Sid Shuman"], ["\\bDon Walker\\b", "Dawnwalker"], ...]  (Python regex, replacement)
"""
import json, os, re

_GENERIC = [(r' \.(?=[A-Za-z])', '.'), (r' -(?=[a-z])', '-'), (r'\s+([,.?!;:])', r'\1'), (r'\s{2,}', ' ')]


def load_fixes(path):
    if path and os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return [(re.compile(a), b) for a, b in json.load(f)]
    return []


def apply_fixes(s, fixes):
    for a, b in _GENERIC:
        s = re.sub(a, b, s)
    for rx, b in fixes:
        s = rx.sub(b, s)
    return s.strip()

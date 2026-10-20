#!/usr/bin/env python3
"""Fail fast when Actions secrets are missing. Prints names only, never values.

Writes runs/result.json with infra_error so publish does not burn a retry.
"""
from __future__ import annotations

import json
import os
import sys

# A provider is usable only when both its key and base URL are set.
# At least one pair must be present. DeepSeek is a fallback, not required.
PAIRS = (
    ("XAI_API_KEY", "XAI_BASE_URL"),
    ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
)


def _set(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def main():
    ready = [key for key, base in PAIRS if _set(key) and _set(base)]
    if ready:
        print("provider secrets set: " + ", ".join(ready))
        return
    incomplete = [base for key, base in PAIRS if _set(key) and not _set(base)]
    missing = incomplete or [key for key, _base in PAIRS]
    spec = {}
    raw = os.environ.get("EPISODE_SPEC") or ""
    if raw:
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            spec = {}
    os.makedirs(os.path.join("runs", "artifact"), exist_ok=True)
    error = "Missing environment variables: " + ", ".join(missing)
    body = {
        "ok": False,
        "infra_error": True,
        "error": error,
        "key": spec.get("key"),
        "show_id": spec.get("show_id"),
        "guid": spec.get("guid"),
        "title": spec.get("title") or "",
        "slug": spec.get("slug") or "",
        "show": spec.get("show") or "",
        "pub_date": spec.get("pub_date") or "",
    }
    with open(os.path.join("runs", "artifact", "result.json"), "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(error, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()

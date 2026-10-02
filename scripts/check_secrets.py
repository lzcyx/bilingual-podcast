#!/usr/bin/env python3
"""Fail fast when Actions secrets are missing. Prints names only, never values.

Writes runs/result.json with infra_error so publish does not burn a retry.
"""
from __future__ import annotations

import json
import os
import sys

REQUIRED = (
    "DEEPSEEK_API_KEY",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
)


def main():
    missing = [name for name in REQUIRED if not (os.environ.get(name) or "").strip()]
    if not missing:
        print("required secrets are set")
        return
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

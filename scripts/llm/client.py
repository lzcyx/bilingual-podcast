#!/usr/bin/env python3
"""OpenAI-compatible DeepSeek client. Thinking mode is off. Secrets stay out of logs."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml


class InfraError(RuntimeError):
    """Missing key, auth failure, or other config problem. Do not burn a retry attempt."""


class LLMError(RuntimeError):
    pass


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_llm_config(root: str | None = None) -> dict:
    root = root or repo_root()
    cfg = {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "temperature": 0.2,
        "thinking": "disabled",
        "price_input_per_mtok": 1.0,
        "price_output_per_mtok": 4.0,
    }
    path = os.path.join(root, "llm.yaml")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k in list(cfg):
            if k in raw and raw[k] is not None:
                cfg[k] = raw[k]
    if os.environ.get("DEEPSEEK_BASE_URL"):
        cfg["base_url"] = os.environ["DEEPSEEK_BASE_URL"].strip()
    if os.environ.get("DEEPSEEK_MODEL"):
        cfg["model"] = os.environ["DEEPSEEK_MODEL"].strip()
    return cfg


def endpoint(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def parse_json_content(text: str):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = raw.find(a), raw.rfind(b)
        if i >= 0 and j > i:
            try:
                return json.loads(raw[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("model did not return JSON")


def _thinking_variants(thinking: str) -> list[dict]:
    """Payload fragments. Disabled is required; fall back if the server rejects a field."""
    mode = (thinking or "disabled").strip().lower()
    if mode in ("enabled", "on", "true"):
        return [{"thinking": {"type": "enabled"}}]
    return [
        {"thinking": {"type": "disabled"}, "reasoning_effort": "none"},
        {"thinking": {"type": "disabled"}},
        {"reasoning_effort": "none"},
    ]


class Client:
    def __init__(self, root: str | None = None, usage_path: str | None = None):
        self.root = root or repo_root()
        self.cfg = load_llm_config(self.root)
        self.key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        self.usage_path = usage_path
        if not self.key:
            raise InfraError("Missing environment variable DEEPSEEK_API_KEY")

    def chat(self, messages, *, step: str, temperature: float | None = None, json_mode: bool = False,
             max_tokens: int = 4096, extra: dict | None = None) -> str:
        temp = self.cfg["temperature"] if temperature is None else temperature
        variants = _thinking_variants(str(self.cfg.get("thinking") or "disabled"))
        last_err = None
        for variant in variants:
            try:
                return self._once(messages, step=step, temperature=temp, json_mode=json_mode,
                                  max_tokens=max_tokens, extra=extra, thinking_fields=variant, allow_400=True)
            except _RetryVariant as e:
                last_err = e
                print(f"  llm: server rejected thinking fields {list(variant)}; trying a smaller set", flush=True)
                continue
        raise LLMError(f"DeepSeek rejected the request: {last_err}")

    def _once(self, messages, *, step, temperature, json_mode, max_tokens, extra, thinking_fields, allow_400):
        url = endpoint(str(self.cfg["base_url"]))
        body = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        body.update(thinking_fields)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode("utf-8")
        delays = (2, 5, 10, 20, 40)
        last = None
        for attempt in range(len(delays) + 1):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:600].decode("utf-8", "replace")
                if e.code in (401, 403):
                    raise InfraError(f"DeepSeek rejected the API key (HTTP {e.code}). Check DEEPSEEK_API_KEY.") from None
                if e.code == 400 and allow_400 and attempt == 0 and _looks_like_thinking_reject(detail):
                    raise _RetryVariant(detail[:240]) from None
                last = f"HTTP {e.code}: {detail[:240]}"
                if e.code in (400, 404, 422):
                    raise LLMError(f"DeepSeek request failed: {last}") from None
                if attempt >= len(delays):
                    raise LLMError(f"DeepSeek request failed: {last}") from None
                print(f"  llm retry {attempt + 1} after {last}", flush=True)
                time.sleep(delays[attempt])
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = str(e)
                if attempt >= len(delays):
                    raise LLMError(f"DeepSeek request failed: {last}") from None
                print(f"  llm retry {attempt + 1} after {last}", flush=True)
                time.sleep(delays[attempt])
        else:
            raise LLMError(f"DeepSeek request failed: {last}")

        usage = payload.get("usage") or {}
        self._record(step, usage, extra)
        choice = (payload.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{step}: response truncated (finish_reason=length); raise max_tokens or shrink the input")
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if not content.strip():
            raise LLMError(f"{step}: empty model content")
        return content

    def _record(self, step, usage, extra):
        row = {
            "step": step,
            "model": self.cfg["model"],
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            row.update(extra)
        if self.usage_path:
            os.makedirs(os.path.dirname(self.usage_path) or ".", exist_ok=True)
            with open(self.usage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        pt, ct = row["prompt_tokens"], row["completion_tokens"]
        print(f"  tokens {step}: in {pt} out {ct}", flush=True)


class _RetryVariant(Exception):
    pass


def _looks_like_thinking_reject(detail: str) -> bool:
    d = detail.lower()
    return "thinking" in d or "reasoning_effort" in d or "reasoning" in d


def usage_summary(path: str | None, prices: dict | None = None) -> dict:
    prices = prices or load_llm_config()
    rows = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    pt = sum(int(r.get("prompt_tokens") or 0) for r in rows)
    ct = sum(int(r.get("completion_tokens") or 0) for r in rows)
    pin = float(prices.get("price_input_per_mtok") or 0)
    pout = float(prices.get("price_output_per_mtok") or 0)
    by = {}
    for r in rows:
        b = by.setdefault(r.get("step") or "?", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        b["calls"] += 1
        b["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
        b["completion_tokens"] += int(r.get("completion_tokens") or 0)
    return {
        "calls": len(rows),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cost_cny": round(pt / 1e6 * pin + ct / 1e6 * pout, 4),
        "price_input_per_mtok_cny": pin,
        "price_output_per_mtok_cny": pout,
        "price_note": "off-peak estimate (闲时)",
        "model": prices.get("model"),
        "by_step": by,
    }

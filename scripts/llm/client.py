#!/usr/bin/env python3
"""OpenAI-compatible multi-provider client with fallback.

Providers are tried in the order listed in llm.yaml. Base URLs and keys come from env.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REQUEST_TIMEOUT = 600
MAX_ATTEMPTS = 5
RETRY_WAITS = (2, 4, 8, 16)


class InfraError(RuntimeError):
    """Missing key, auth failure, or other config problem. Do not burn a retry attempt."""


class LLMError(RuntimeError):
    pass


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_llm_config(root: str | None = None) -> dict:
    root = root or repo_root()
    cfg = {
        "temperature": 0.2,
        "thinking": "disabled",
        "providers": [],
    }
    path = os.path.join(root, "llm.yaml")
    if os.path.exists(path):
        import yaml
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if "temperature" in raw:
            cfg["temperature"] = raw["temperature"]
        if "thinking" in raw:
            cfg["thinking"] = raw["thinking"]
        providers = raw.get("providers") or []
        # Backward compat: single base_url/model
        if not providers and raw.get("base_url"):
            providers = [{
                "name": "deepseek",
                "base_url": raw.get("base_url"),
                "model": raw.get("model") or "deepseek-flash",
                "key_env": "DEEPSEEK_API_KEY",
            }]
        cfg["providers"] = providers
    return cfg


def _resolve_providers(cfg: dict) -> list[dict]:
    """Resolve env vars into concrete provider dicts. Skip ones without a key."""
    out = []
    for p in cfg.get("providers") or []:
        name = p.get("name") or "?"
        key_env = p.get("key_env") or ""
        base_env = p.get("base_url_env") or ""
        key = (os.environ.get(key_env) or "").strip() if key_env else ""
        base = (os.environ.get(base_env) or "").strip() if base_env else ""
        if not base:
            base = (p.get("base_url") or "").strip()
        model = (p.get("model") or "").strip()
        if not key or not base or not model:
            continue
        out.append({
            "name": name,
            "base_url": base.rstrip("/"),
            "model": model,
            "key": key,
        })
    return out


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
    mode = (thinking or "disabled").strip().lower()
    if mode in ("high", "xhigh"):
        return [
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
            {"reasoning_effort": "high"},
            {"thinking": {"type": "enabled"}},
        ]
    if mode in ("enabled", "on", "true"):
        return [{"thinking": {"type": "enabled"}}]
    return [
        {"thinking": {"type": "disabled"}, "reasoning_effort": "none"},
        {"thinking": {"type": "disabled"}},
        {"reasoning_effort": "none"},
    ]


def consume_sse(text: str) -> tuple[str, dict]:
    content: list[str] = []
    usage: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            if data == "[DONE]":
                break
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for choice in obj.get("choices") or []:
            if choice.get("finish_reason") == "length":
                usage["_truncated"] = True
            delta = choice.get("delta") or {}
            piece = delta.get("content") or ""
            if not piece and isinstance(choice.get("message"), dict):
                piece = choice["message"].get("content") or ""
            if piece:
                content.append(piece)
    return "".join(content), usage


class _StreamUnsupported(Exception):
    pass


class Client:
    def __init__(self, root: str | None = None, usage_path: str | None = None):
        self.root = root or repo_root()
        self.cfg = load_llm_config(self.root)
        self.providers = _resolve_providers(self.cfg)
        self.usage_path = usage_path
        self._lock = threading.Lock()
        if not self.providers:
            raise InfraError("No usable LLM providers (missing keys or base URLs)")

    def chat(self, messages, *, step: str, temperature: float | None = None, json_mode: bool = False,
             max_tokens: int = 4096, extra: dict | None = None, thinking: str | None = None) -> str:
        temp = self.cfg["temperature"] if temperature is None else temperature
        requested_thinking = str(self.cfg.get("thinking") or "disabled") if thinking is None else thinking
        last_err = None
        for prov in self.providers:
            try:
                return self._try_provider(prov, messages, step=step, temperature=temp,
                                           json_mode=json_mode, max_tokens=max_tokens,
                                           extra=extra, thinking=requested_thinking)
            except InfraError as e:
                # Auth / config problem on this provider — try next
                print(f"  llm: {prov['name']} skipped: {e}", flush=True)
                last_err = e
                continue
            except LLMError as e:
                print(f"  llm: {prov['name']} failed: {e}", flush=True)
                last_err = e
                continue
        raise LLMError(f"all providers failed: {last_err}")

    def _try_provider(self, prov, messages, *, step, temperature, json_mode, max_tokens, extra, thinking):
        variants = _thinking_variants(thinking)
        last_err = None
        for variant in variants:
            try:
                return self._attempt_loop(prov, messages, step=step, temperature=temperature,
                                           json_mode=json_mode, max_tokens=max_tokens,
                                           extra=extra, thinking_fields=variant)
            except _RetryVariant as e:
                last_err = e
                print(f"  llm: {prov['name']} rejected thinking fields; trying smaller set", flush=True)
                continue
        raise LLMError(str(last_err))

    def _attempt_loop(self, prov, messages, *, step, temperature, json_mode, max_tokens, extra, thinking_fields):
        url = endpoint(prov["base_url"])
        stream = True
        last = None
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            body = {
                "model": prov["model"],
                "messages": messages,
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
            }
            body.update(thinking_fields)
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            if stream:
                body["stream"] = True
                body["stream_options"] = {"include_usage": True}
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Authorization": "Bearer " + prov["key"],
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8")
                if stream:
                    content, usage = consume_sse(raw)
                    if not content.strip() and not usage:
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            payload = None
                        if isinstance(payload, dict) and payload.get("choices"):
                            content, usage = _content_from_payload(payload)
                        else:
                            raise LLMError(f"{step}: empty streamed content")
                else:
                    payload = json.loads(raw)
                    content, usage = _content_from_payload(payload)
                if usage.get("_truncated"):
                    raise LLMError(f"{step}: response truncated")
                if not content.strip():
                    raise LLMError(f"{step}: empty model content")
                self._record(step, usage, extra, prov)
                return content
            except _StreamUnsupported:
                raise
            except urllib.error.HTTPError as e:
                detail = e.read()[:600].decode("utf-8", "replace")
                if e.code in (401, 403):
                    raise InfraError(f"auth failed (HTTP {e.code})") from None
                if e.code == 400 and _looks_like_thinking_reject(detail):
                    raise _RetryVariant(detail[:240]) from None
                if e.code == 400 and stream and _looks_like_stream_reject(detail):
                    print(f"  llm: {prov['name']} streaming rejected, falling back", flush=True)
                    stream = False
                    attempt -= 1
                    continue
                last = f"HTTP {e.code}: {detail[:200]}"
                if e.code in (400, 404, 422):
                    raise LLMError(last) from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, LLMError) as e:
                if isinstance(e, LLMError) and "truncated" in str(e):
                    raise
                last = str(e)
            if attempt >= MAX_ATTEMPTS:
                raise LLMError(f"{prov['name']} failed after retries: {last}")
            wait = RETRY_WAITS[min(attempt - 1, len(RETRY_WAITS) - 1)]
            print(f"  llm: {prov['name']} retry {attempt} in {wait}s", flush=True)
            time.sleep(wait)
        raise LLMError(f"{prov['name']} failed: {last}")

    def _record(self, step, usage, extra, prov):
        host = endpoint(prov["base_url"]).split("/")[2]
        row = {
            "step": step,
            "provider": prov["name"],
            "api": host,
            "model": prov["model"],
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "prompt_cache_hit_tokens": _cache_hit(usage),
            "prompt_cache_miss_tokens": _cache_miss(usage),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            row.update(extra)
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            if self.usage_path:
                os.makedirs(os.path.dirname(self.usage_path) or ".", exist_ok=True)
                with open(self.usage_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")


def _content_from_payload(payload: dict) -> tuple[str, dict]:
    usage = dict(payload.get("usage") or {})
    choice = (payload.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        usage["_truncated"] = True
    message = choice.get("message") or {}
    return message.get("content") or "", usage


def _looks_like_stream_reject(detail: str) -> bool:
    return "stream" in detail.lower()


class _RetryVariant(Exception):
    pass


def _looks_like_thinking_reject(detail: str) -> bool:
    d = detail.lower()
    return "thinking" in d or "reasoning_effort" in d or "reasoning" in d


def _cache_hit(usage: dict) -> int:
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    return int(usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0)


def _cache_miss(usage: dict) -> int:
    return int(usage.get("prompt_cache_miss_tokens") or 0)


# Only DeepSeek has local price estimation (CNY).
_PEAK_HIT = 0.04
_PEAK_MISS = 2.0
_PEAK_OUT = 8.0


def is_peak(ts: datetime) -> bool:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    if ts.weekday() >= 5:
        return False
    minutes = ts.hour * 60 + ts.minute
    return (60 <= minutes < 240) or (360 <= minutes < 600)


def _parse_ts(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _row_tokens(row: dict) -> tuple[int, int, int, int]:
    prompt = int(row.get("prompt_tokens") or 0)
    out = int(row.get("completion_tokens") or 0)
    hit = int(row.get("prompt_cache_hit_tokens") or 0)
    miss = int(row.get("prompt_cache_miss_tokens") or 0)
    if hit + miss <= 0:
        miss = prompt
    elif hit + miss < prompt:
        miss += prompt - hit - miss
    return prompt, out, hit, miss


def _row_cost(row: dict) -> tuple[float, bool]:
    if (row.get("provider") or "").lower() != "deepseek":
        return 0.0, False
    _prompt, out, hit, miss = _row_tokens(row)
    peak = is_peak(_parse_ts(row.get("ts")))
    factor = 1.0 if peak else 0.5
    cny = (hit / 1e6 * _PEAK_HIT + miss / 1e6 * _PEAK_MISS + out / 1e6 * _PEAK_OUT) * factor
    return cny, peak


def format_usage(usage: dict) -> str:
    if not usage or not usage.get("calls"):
        return ""
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    cost = float(usage.get("cost_cny") or 0)
    providers = usage.get("providers") or []
    prov_txt = "/".join(providers) if providers else ""
    parts = [f"{pt} in / {ct} out"]
    if prov_txt:
        parts.append(prov_txt)
    if cost > 0:
        parts.append(f"¥{cost:.4f}")
    return " · ".join(parts)


def usage_summary(path: str | None, prices: dict | None = None) -> dict:
    rows = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    pt = ct = hit = miss = 0
    cost = 0.0
    by = {}
    providers_used = set()
    for r in rows:
        p, c, h, m = _row_tokens(r)
        cny, _ = _row_cost(r)
        pt += p
        ct += c
        hit += h
        miss += m
        cost += cny
        providers_used.add(r.get("provider") or "?")
        b = by.setdefault(r.get("step") or "?", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        b["calls"] += 1
        b["prompt_tokens"] += p
        b["completion_tokens"] += c
    return {
        "calls": len(rows),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "cost_cny": round(cost, 6),
        "providers": sorted(providers_used),
        "by_step": by,
    }

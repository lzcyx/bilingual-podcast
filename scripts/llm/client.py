#!/usr/bin/env python3
"""OpenAI-compatible DeepSeek client. Thinking mode is off. Secrets stay out of logs."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# One call may run this long (streaming). 429 / 5xx / timeouts retry up to 5 times.
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
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "temperature": 0.2,
        "thinking": "disabled",
        "price_input_per_mtok": 1.0,
        "price_output_per_mtok": 4.0,
    }
    path = os.path.join(root, "llm.yaml")
    if os.path.exists(path):
        import yaml
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


def consume_sse(text: str) -> tuple[str, dict]:
    """Fold an OpenAI-style server-sent stream into (content, usage)."""
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
        self.key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        self.usage_path = usage_path
        self._lock = threading.Lock()
        if not self.key:
            raise InfraError("Missing environment variable DEEPSEEK_API_KEY")

    def chat(self, messages, *, step: str, temperature: float | None = None, json_mode: bool = False,
             max_tokens: int = 4096, extra: dict | None = None) -> str:
        temp = self.cfg["temperature"] if temperature is None else temperature
        variants = _thinking_variants(str(self.cfg.get("thinking") or "disabled"))
        last_err = None
        for variant in variants:
            try:
                return self._attempt_loop(messages, step=step, temperature=temp, json_mode=json_mode,
                                           max_tokens=max_tokens, extra=extra, thinking_fields=variant)
            except _RetryVariant as e:
                last_err = e
                print(f"  llm: server rejected thinking fields {list(variant)}; trying a smaller set", flush=True)
                continue
        raise LLMError(f"DeepSeek rejected the request: {last_err}")

    def _attempt_loop(self, messages, *, step, temperature, json_mode, max_tokens, extra, thinking_fields):
        url = endpoint(str(self.cfg["base_url"]))
        stream = True
        last = None
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            body = {
                "model": self.cfg["model"],
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
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8")
                if stream:
                    content, usage = consume_sse(raw)
                    if not content.strip() and not usage:
                        # A non-stream JSON body, or an empty stream.
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
                    raise LLMError(f"{step}: response truncated (finish_reason=length); raise max_tokens or shrink the input")
                if not content.strip():
                    raise LLMError(f"{step}: empty model content")
                self._record(step, usage, extra)
                return content
            except _StreamUnsupported:
                raise
            except urllib.error.HTTPError as e:
                detail = e.read()[:600].decode("utf-8", "replace")
                if e.code in (401, 403):
                    raise InfraError(f"DeepSeek rejected the API key (HTTP {e.code}). Check DEEPSEEK_API_KEY.") from None
                if e.code == 400 and _looks_like_thinking_reject(detail):
                    raise _RetryVariant(detail[:240]) from None
                if e.code == 400 and stream and _looks_like_stream_reject(detail):
                    print("  llm: streaming rejected, falling back to a single response", flush=True)
                    stream = False
                    attempt -= 1
                    continue
                last = f"HTTP {e.code}: {detail[:240]}"
                if e.code in (400, 404, 422):
                    raise LLMError(f"DeepSeek request failed: {last}") from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, LLMError) as e:
                if isinstance(e, LLMError) and "truncated" in str(e):
                    raise
                last = str(e)
            if attempt >= MAX_ATTEMPTS:
                raise LLMError(f"DeepSeek request failed: {last}")
            wait = RETRY_WAITS[min(attempt - 1, len(RETRY_WAITS) - 1)]
            print(f"  llm retry {attempt} in {wait}s after {last}", flush=True)
            time.sleep(wait)
        raise LLMError(f"DeepSeek request failed: {last}")

    def _record(self, step, usage, extra):
        host = endpoint(str(self.cfg["base_url"])).split("/")[2]
        row = {
            "step": step,
            "api": host,
            "model": self.cfg["model"],
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
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
            print(f"  tokens {step}: in {row['prompt_tokens']} out {row['completion_tokens']} ({host})", flush=True)


def _content_from_payload(payload: dict) -> tuple[str, dict]:
    usage = dict(payload.get("usage") or {})
    choice = (payload.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        usage["_truncated"] = True
    message = choice.get("message") or {}
    return message.get("content") or "", usage


def _looks_like_stream_reject(detail: str) -> bool:
    d = detail.lower()
    return "stream" in d


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
    by_model = {}
    for r in rows:
        b = by.setdefault(r.get("step") or "?", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        b["calls"] += 1
        b["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
        b["completion_tokens"] += int(r.get("completion_tokens") or 0)
        mk = f"{r.get('api') or 'deepseek'} {r.get('model') or prices.get('model') or ''}".strip()
        m = by_model.setdefault(mk, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        m["calls"] += 1
        m["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
        m["completion_tokens"] += int(r.get("completion_tokens") or 0)
    return {
        "calls": len(rows),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cost_cny": round(pt / 1e6 * pin + ct / 1e6 * pout, 4),
        "price_input_per_mtok_cny": pin,
        "price_output_per_mtok_cny": pout,
        "price_note": "DeepSeek 闲时估算，输入 ¥1 / 输出 ¥4 每百万 token",
        "model": prices.get("model"),
        "by_step": by,
        "by_model": by_model,
    }

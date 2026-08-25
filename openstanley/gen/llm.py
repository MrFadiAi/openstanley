"""Provider-agnostic LLM engine — any OpenAI-compatible endpoint.

Uses httpx directly (no heavy SDK). GLM via z.ai, OpenAI, Ollama — all work.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from ..core.config import LLMConfig


class LLMError(RuntimeError):
    pass


def chat(cfg: LLMConfig, system: str, user: str, temperature: Optional[float] = None,
         json_mode: bool = False, retries: int = 2,
         thinking_budget: int = 0) -> str:
    """Blocking call. The agent loops run in threads; server handlers call via threadpool."""
    if not cfg.api_key:
        raise LLMError(
            f"Missing LLM API key. Set environment variable {cfg.api_key_env}."
        )
    # z.ai (and most Anthropic-compat endpoints) reject temperature > 1 —
    # clamp here so no caller ladder can 400 the whole request
    temp = max(0.0, min(float(cfg.temperature if temperature is None else temperature), 1.0))
    last_err = None
    for attempt in range(retries + 1):
        try:
            if cfg.transport == "anthropic":
                return _chat_anthropic(cfg, system, user, temp, json_mode,
                                       thinking_budget=thinking_budget)
            return _chat_openai(cfg, system, user, temp, json_mode)
        except LLMError as e:
            last_err = str(e)
        if attempt < retries:
            import time
            time.sleep(2 ** attempt)
    raise LLMError(f"LLM call failed ({cfg.model} @ {cfg.base_url}): {last_err}")


def _chat_openai(cfg: LLMConfig, system: str, user: str, temp: float, json_mode: bool) -> str:
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    body: dict = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temp,
        "max_tokens": cfg.max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    r = httpx.post(url, headers=headers, json=body, timeout=120)
    if r.status_code == 200:
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")


def _chat_anthropic(cfg: LLMConfig, system: str, user: str, temp: float,
                    json_mode: bool, thinking_budget: int = 0) -> str:
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    sys_text = system + ("\nReturn STRICT JSON only." if json_mode else "")
    body = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": temp,
        "system": sys_text,
        "messages": [{"role": "user", "content": user}],
    }
    if thinking_budget > 0:
        # GLM-5.3 on z.ai honors the Anthropic thinking parameter: the model
        # reasons in private scratch-space before the visible answer. Temp
        # must be unset with thinking on Anthropic-style APIs.
        body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        body.pop("temperature", None)
        body["max_tokens"] = max(cfg.max_tokens, thinking_budget + 1000)
    url = cfg.base_url.rstrip("/")
    if not url.endswith("/v1/messages"):
        url = url + "/v1/messages"
    r = httpx.post(url, headers=headers, json=body, timeout=120)
    if r.status_code == 200:
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        return "".join(parts)
    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")


def chat_stream(cfg: LLMConfig, system: str, user: str,
                temperature: Optional[float] = None) -> "Iterator[str]":
    """Streaming variant of chat() — yields text deltas as they arrive.

    Uses SSE at the provider level (anthropic: /v1/messages with stream:true;
    openai-compatible: /chat/completions with stream:true). Raises LLMError on
    connection/HTTP failure (surfaced by the caller mid-stream).
    """
    from typing import Iterator  # noqa: F401 — re-exported for callers
    if not cfg.api_key:
        raise LLMError(f"Missing LLM API key. Set environment variable {cfg.api_key_env}.")
    # z.ai (and most Anthropic-compat endpoints) reject temperature > 1 —
    # clamp here so no caller ladder can 400 the whole request
    temp = max(0.0, min(float(cfg.temperature if temperature is None else temperature), 1.0))
    if cfg.transport == "anthropic":
        yield from _stream_anthropic(cfg, system, user, temp)
    else:
        yield from _stream_openai(cfg, system, user, temp)


def _sse_lines(response) -> "Iterator[dict]":
    """Parse an SSE byte stream into JSON event dicts."""
    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            import json as _json
            yield _json.loads(payload)
        except json.JSONDecodeError:
            continue


def _stream_anthropic(cfg: LLMConfig, system: str, user: str, temp: float):
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": temp,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "stream": True,
    }
    url = cfg.base_url.rstrip("/")
    if not url.endswith("/v1/messages"):
        url = url + "/v1/messages"
    with httpx.stream("POST", url, headers=headers, json=body, timeout=180) as r:
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.read()[:300]!r}")
        for evt in _sse_lines(r):
            if evt.get("type") == "content_block_delta":
                delta = evt.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield text
            elif evt.get("type") == "error":
                raise LLMError(f"stream error: {evt.get('error', {})}")


def _stream_openai(cfg: LLMConfig, system: str, user: str, temp: float):
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temp,
        "max_tokens": cfg.max_tokens,
        "stream": True,
    }
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    with httpx.stream("POST", url, headers=headers, json=body, timeout=180) as r:
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.read()[:300]!r}")
        for evt in _sse_lines(r):
            choices = evt.get("choices") or []
            if choices:
                delta = choices[0].get("delta", {})
                text = delta.get("content")
                if text:
                    yield text


def _repair_json_strings(text: str) -> str:
    """Escape literal control chars (newlines/tabs) inside quoted strings —
    LLMs emit them raw, which json.loads rejects even though the JSON is
    otherwise well-formed."""
    def _fix(m):
        chunk = m.group(0)
        return chunk.replace(chr(10), chr(92) + "n").replace(chr(9), chr(92) + "t").replace(chr(13), chr(92) + "r")
    # " ( ... string-chars ... ) " assembled from chr() so no source-level
    # backslash can be mangled by tooling between here and the file
    pattern = (chr(34) + "(?:[^" + chr(34) + chr(92) * 2 + "]|" + chr(92) * 2 + ".)*" + chr(34))
    return re.sub(pattern, _fix, text, flags=re.DOTALL)




def extract_json(text: str) -> dict | list:
    """Tolerant JSON extraction: fenced blocks, leading prose, trailing junk,
    and raw control characters inside strings."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_repair_json_strings(text))
    except json.JSONDecodeError:
        pass
    # find first { or [ and match to last } or ]
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise LLMError(f"No JSON found in: {text[:200]}")
    s = min(starts)
    opener = text[s]
    closer = "}" if opener == "{" else "]"
    e = text.rfind(closer)
    if e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            try:
                return json.loads(_repair_json_strings(text[s:e + 1]))
            except json.JSONDecodeError:
                pass
    raise LLMError(f"Unparseable JSON from LLM: {text[:200]}")

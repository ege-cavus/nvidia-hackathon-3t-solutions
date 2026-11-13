"""Structured-output client shared by every agent.

Every call asks for JSON matching a Pydantic model and validates the result.
Two backends:
  openai     OpenAI-compatible servers (vLLM, NIM, SGLang) on the DGX Spark, the production
             path; uses guided decoding (response_format=json_schema) where supported
  claude-code  Claude Code headless mode, a stand-in for testing (see llm_claude_code.py)

Reasoning traces (<think>...</think>) are stripped before parsing. Set UR_TRACE_FILE to
append every call (agent, prompts, raw reply, latency) to a JSONL file.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import ModelEndpoint

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_clients: dict[str, AsyncOpenAI] = {}

# Set to "0" if the server rejects response_format=json_schema.
GUIDED_JSON = os.getenv("UR_GUIDED_JSON", "1") == "1"
# Most hybrid-reasoning models accept this via the chat template; disable if your server errors.
DISABLE_THINKING = os.getenv("UR_DISABLE_THINKING", "1") == "1"
# Ollama ignores chat_template_kwargs; its OpenAI endpoint takes reasoning_effort instead
# ("none" turns thinking off). Leave unset for vLLM / NIM / SGLang.
REASONING_EFFORT = os.getenv("UR_REASONING_EFFORT", "")


class LLMError(RuntimeError):
    pass


def _client(ep: ModelEndpoint) -> AsyncOpenAI:
    key = f"{ep.base_url}|{ep.api_key}"
    if key not in _clients:
        _clients[key] = AsyncOpenAI(base_url=ep.base_url, api_key=ep.api_key, timeout=ep.timeout_s)
    return _clients[key]


def extract_json(text: str) -> dict:
    text = _THINK_RE.sub("", text).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"No JSON object in model output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _trace(record: dict) -> None:
    path = os.getenv("UR_TRACE_FILE")
    if not path:
        return
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


async def _complete_openai(ep: ModelEndpoint, system: str, messages: list[dict], out: type[BaseModel]) -> str:
    kwargs: dict = {
        "model": ep.model,
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": ep.temperature,
    }
    if GUIDED_JSON:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": out.__name__, "schema": out.model_json_schema()},
        }
    if DISABLE_THINKING:
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
    resp = await _client(ep).chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def chat_json(
    ep: ModelEndpoint,
    system: str,
    user: str,
    out: type[T],
    retries: int = 1,
    label: str = "",
) -> tuple[T, int]:
    """Return (validated model, latency_ms). Raises LLMError after retries."""
    system = system + "\n\nRespond with a single JSON object matching this schema:\n" + json.dumps(out.model_json_schema())
    messages: list[dict] = [{"role": "user", "content": user}]

    last_err: Exception | None = None
    t0 = time.perf_counter()
    for attempt in range(retries + 1):
        content, meta = "", {}
        try:
            if ep.provider == "claude-code":
                from .llm_claude_code import ClaudeCodeError, complete

                try:
                    content, meta = await complete(ep, system, messages)
                except ClaudeCodeError as e:
                    raise LLMError(str(e)) from e
            else:
                try:
                    content = await _complete_openai(ep, system, messages, out)
                except Exception as e:  # network / server errors: don't retry blindly
                    raise LLMError(f"{ep.model} at {ep.base_url}: {e}") from e
            parsed = out.model_validate(extract_json(content))
            latency = int((time.perf_counter() - t0) * 1000)
            _trace({"ts": datetime.now(timezone.utc), "agent": label, "provider": ep.provider, "model": ep.model,
                    "attempt": attempt, "ok": True, "latency_ms": latency, "system": system, "user": user,
                    "raw": content, **meta})
            return parsed, latency
        except (ValidationError, json.JSONDecodeError) as e:
            last_err = e
            _trace({"ts": datetime.now(timezone.utc), "agent": label, "provider": ep.provider, "model": ep.model,
                    "attempt": attempt, "ok": False, "error": str(e)[:500], "system": system, "user": user,
                    "raw": content})
            messages += [
                {"role": "assistant", "content": content or "(empty)"},
                {"role": "user", "content": f"Your last reply was invalid: {e}. Return only valid JSON."},
            ]
        except LLMError as e:
            _trace({"ts": datetime.now(timezone.utc), "agent": label, "provider": ep.provider, "model": ep.model,
                    "attempt": attempt, "ok": False, "error": str(e)[:500], "system": system, "user": user,
                    "raw": content})
            if "No JSON object" in str(e):
                last_err = e
                messages += [
                    {"role": "assistant", "content": content or "(empty)"},
                    {"role": "user", "content": "Return only the JSON object, nothing else."},
                ]
                continue
            raise
    raise LLMError(f"{ep.model} returned invalid output: {last_err}")

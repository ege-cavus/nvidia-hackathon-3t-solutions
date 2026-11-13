"""Claude Code backend: a stand-in for the Spark models when testing (LLM_PROVIDER=claude-code).

Each call runs `claude -p` (Claude Code headless mode) as a subprocess, with tools and
MCP servers disabled, so it uses your Claude Code login and no API key. The JSON schema
stays in the system prompt and the reply is validated with Pydantic in llm.py, exactly
as on the vLLM path, so the same prompts are exercised.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

from .config import ModelEndpoint


class ClaudeCodeError(RuntimeError):
    pass


def _render(messages: list[dict]) -> str:
    """Headless mode takes one prompt; fold retry turns into it."""
    if len(messages) == 1:
        return messages[0]["content"]
    parts = []
    for m in messages:
        tag = "YOUR PREVIOUS REPLY" if m["role"] == "assistant" else "USER"
        parts.append(f"=== {tag} ===\n{m['content']}")
    return "\n\n".join(parts)


async def complete(ep: ModelEndpoint, system: str, messages: list[dict]) -> tuple[str, dict]:
    """Return (reply text, metadata). Raises ClaudeCodeError if the CLI fails."""
    binary = os.getenv("CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise ClaudeCodeError("`claude` CLI not found on PATH (set CLAUDE_BIN)")
    cmd = [
        binary, "-p",
        "--output-format", "json",
        "--system-prompt", system,
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if ep.model and ep.model != "default":
        cmd += ["--model", ep.model]
    if os.getenv("CLAUDE_EFFORT"):
        cmd += ["--effort", os.environ["CLAUDE_EFFORT"]]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(_render(messages).encode()), timeout=ep.timeout_s)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise ClaudeCodeError(f"claude -p timed out after {ep.timeout_s:.0f}s") from e

    try:
        data = json.loads(out.decode())
    except json.JSONDecodeError as e:
        raise ClaudeCodeError(f"claude -p exit {proc.returncode}: {(err or out).decode()[:300]}") from e
    if proc.returncode != 0 or data.get("is_error"):
        raise ClaudeCodeError(f"claude -p failed: {str(data.get('result') or data)[:300]}")

    models = list((data.get("modelUsage") or {}).keys())
    meta = {
        "duration_api_ms": data.get("duration_api_ms"),
        "cost_usd": data.get("total_cost_usd"),
        "models": models,
        "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        "output_tokens": (data.get("usage") or {}).get("output_tokens"),
    }
    return data.get("result") or "", meta

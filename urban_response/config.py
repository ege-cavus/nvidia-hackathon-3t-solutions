"""Runtime configuration, read from environment variables.

Production target: both models behind OpenAI-compatible endpoints (vLLM / NIM /
SGLang) on the DGX Spark. Qwen handles intake and post-dispatch assessment; the four Nemotron agents (A1-A3 and
CR) share one Nemotron endpoint unless CR_* overrides are set.

LLM_MODE=mock      no model server: deterministic heuristics, tagged executed_by="mock"
LLM_MODE=live      call the configured endpoints
LLM_PROVIDER=claude-code  stand-in for testing without the Spark: every role (Qwen, A1-A3, CR)
                   is served by Claude Code headless mode (`claude -p`), using your Claude Code
                   login. CLAUDE_MODEL picks the model (default: Claude Code's own default).
                   Prompts, schemas and validation are unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str
    model: str
    api_key: str = "not-needed"
    temperature: float = 0.1
    timeout_s: float = 60.0
    provider: str = "openai"  # "openai" (vLLM / NIM / SGLang) | "claude-code" (test stand-in)


def _endpoint(url_env: str, url_default: str, model_env: str, model_default: str, key_env: str) -> ModelEndpoint:
    if os.getenv("LLM_PROVIDER") == "claude-code":
        return ModelEndpoint(
            base_url="claude -p",
            model=os.getenv("CLAUDE_MODEL", "default"),
            timeout_s=300.0,
            provider="claude-code",
        )
    return ModelEndpoint(
        base_url=os.getenv(url_env, url_default),
        model=os.getenv(model_env, model_default),
        api_key=os.getenv(key_env, "not-needed"),
        timeout_s=float(os.getenv("UR_LLM_TIMEOUT_S", "120")),
    )


@dataclass(frozen=True)
class Settings:
    llm_mode: str = field(default_factory=lambda: os.getenv("LLM_MODE", "mock"))  # "live" | "mock"
    orchestrator: ModelEndpoint = field(
        default_factory=lambda: _endpoint("QWEN_BASE_URL", "http://127.0.0.1:8001/v1",
                                          "QWEN_MODEL", "qwen3.5", "QWEN_API_KEY")
    )
    specialist: ModelEndpoint = field(
        default_factory=lambda: _endpoint("NEMOTRON_BASE_URL", "http://127.0.0.1:8002/v1",
                                          "NEMOTRON_MODEL", "nemotron-3-nano", "NEMOTRON_API_KEY")
    )
    # CR (conflict resolution) is a Nemotron agent too; by default it shares the specialists' server.
    resolver: ModelEndpoint = field(
        default_factory=lambda: _endpoint("CR_BASE_URL", os.getenv("NEMOTRON_BASE_URL", "http://127.0.0.1:8002/v1"),
                                          "CR_MODEL", os.getenv("NEMOTRON_MODEL", "nemotron-3-nano"), "CR_API_KEY")
    )
    db_path: Path = field(default_factory=lambda: Path(os.getenv("UR_DB_PATH", str(ROOT / "data" / "urban_response.db"))))
    kb_dir: Path = field(default_factory=lambda: Path(os.getenv("UR_KB_DIR", str(ROOT / "knowledge_base"))))
    kb_top_k: int = int(os.getenv("UR_KB_TOP_K", "4"))

    @property
    def live(self) -> bool:
        return self.llm_mode == "live"


def get_settings() -> Settings:
    return Settings()

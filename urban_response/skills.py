"""Nemotron skills as agent knowledge.

nemotron_resource_skills/ (vendored unchanged from origin/main) holds one SKILL.md per
service plus resource_allocator_skill/SKILL.md, the conflict arbiter. They were written
as system prompts for a stand-alone pipeline with its own I/O contract
(proposal_schema.json, different resource type names, "human review mandatory").

Here they are the agents' knowledge: each agent's system prompt includes its skill's
role, assessment method, severity scale, guardrails and resource-proposal principles
(A1 police, A2 ambulance, A3 fire), and CR gets the allocator's decision policy and
worked examples. The parts that define that other contract (its input description,
output format, resource identifiers, ranking/totals bookkeeping, human-review flags)
are left out so they cannot clash with this pipeline's schemas. The files are never edited.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .config import ROOT

SKILLS_DIR = Path(os.getenv("UR_SKILLS_DIR", str(ROOT / "nemotron_resource_skills")))

# Sections that carry knowledge (kept) vs. the other pipeline's contract (dropped).
_SERVICE_SECTIONS = ("role", "assessment method", "guardrails", "resource proposal skill")
_ARBITER_SECTIONS = ("decision policy", "examples of decisions")
# Lines inside kept sections that belong to the other contract.
_CONTRACT_LINE = re.compile(
    r"resource type identifiers|output one json|proposal_schema|allocator_output_schema|rank all events|"
    r"total_requested|unmet_demand\[|snapshot|requires_human_review|human review is mandatory|"
    r"you receive all events|no markdown", re.I)


def _sections(text: str) -> list[tuple[str, str]]:
    out, head, buf = [], "", []
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            if head or buf:
                out.append((head, "\n".join(buf).strip()))
            head, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    out.append((head, "\n".join(buf).strip()))
    return out


def _extract(path: Path, keep: tuple[str, ...]) -> str:
    if not path.exists():
        return ""
    parts = []
    for head, body in _sections(path.read_text(encoding="utf-8")):
        if not any(head.lower().startswith(k) for k in keep):
            continue
        lines = [l for l in body.splitlines() if not _CONTRACT_LINE.search(l)]
        body = "\n".join(lines).strip()
        if body:
            parts.append(f"{head}:\n{body}")
    return "\n\n".join(parts)


@lru_cache(maxsize=None)
def service_skill(agency: str) -> str:
    """Knowledge from nemotron_resource_skills/<agency>/SKILL.md ('' if the file is absent)."""
    return _extract(SKILLS_DIR / agency / "SKILL.md", _SERVICE_SECTIONS)


@lru_cache(maxsize=None)
def arbiter_skill() -> str:
    """Decision policy from nemotron_resource_skills/resource_allocator_skill/SKILL.md."""
    return _extract(SKILLS_DIR / "resource_allocator_skill" / "SKILL.md", _ARBITER_SECTIONS)


def citation(agency: str | None = None) -> str:
    return f"skill:{agency}/SKILL.md" if agency else "skill:resource_allocator_skill/SKILL.md"

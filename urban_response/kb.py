"""Per-agency knowledge base retrieval.

Each agency has a folder of Markdown documents under knowledge_base/<agency>/.
Documents are split on headings and ranked with BM25, which needs no extra
model. Swap `KnowledgeBase.search` for an embedding retriever (e.g. a Nemotron
embedding model on the Spark) without changing callers.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be by for from has in is it its of on or that the to was were will with this".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass
class Chunk:
    doc: str
    heading: str
    text: str

    @property
    def citation(self) -> str:
        return f"{self.doc}#{self.heading}"


class KnowledgeBase:
    def __init__(self, folder: Path, k1: float = 1.5, b: float = 0.75):
        self.folder = folder
        self.k1, self.b = k1, b
        self.chunks: list[Chunk] = []
        for path in sorted(folder.glob("*.md")):
            self.chunks.extend(self._split(path))
        self._tf = [Counter(tokenize(c.heading + " " + c.text)) for c in self.chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @staticmethod
    def _split(path: Path) -> list[Chunk]:
        chunks, heading, buf = [], path.stem, []
        for line in path.read_text().splitlines():
            if line.startswith("#"):
                if "".join(buf).strip():
                    chunks.append(Chunk(path.stem, heading, "\n".join(buf).strip()))
                heading, buf = line.lstrip("#").strip(), []
            else:
                buf.append(line)
        if "".join(buf).strip():
            chunks.append(Chunk(path.stem, heading, "\n".join(buf).strip()))
        return chunks

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        q = tokenize(query)
        scores = []
        for i, tf in enumerate(self._tf):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [self.chunks[i] for _, i in scores[:k]]

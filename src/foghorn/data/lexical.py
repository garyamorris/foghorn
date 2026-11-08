"""A tiny lexical search engine (BM25) for the runbook and past-incident corpora. Pure stdlib, deterministic.

It is deliberately LEXICAL: it ranks by shared vocabulary, which is exactly why RB-017 ("restart database primary")
is a trap for generic "database slow / timeout" queries.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "should",
        "can",
        "could",
        "would",
        "about",
        "into",
        "over",
        "than",
        "too",
        "very",
        "just",
        "also",
        "any",
        "all",
        "been",
        "being",
        "did",
        "get",
        "got",
    ]
)
_TOKEN = re.compile(r"[a-z0-9]+")


def stem(tok: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN.findall(text.lower().replace("_", " ")):
        if raw in _STOP:
            continue
        out.append(stem(raw))
    return out


@dataclass
class Doc:
    id: str
    fields: dict[str, str]
    payload: dict[str, object] = field(default_factory=dict)


@dataclass
class Index:
    docs: list[Doc]
    weights: dict[str, int]
    k1: float = 1.4
    b: float = 0.6
    _tf: list[Counter[str]] = field(default_factory=list, repr=False)
    _len: list[int] = field(default_factory=list, repr=False)
    _df: Counter[str] = field(default_factory=Counter, repr=False)
    _avg: float = 1.0

    def __post_init__(self) -> None:
        for d in self.docs:
            tf: Counter[str] = Counter()
            for fld, text in d.fields.items():
                w = self.weights.get(fld, 1)
                for t in tokenize(text):
                    tf[t] += w
            self._tf.append(tf)
            self._len.append(sum(tf.values()))
            for t in tf:
                self._df[t] += 1
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0

    def search(self, query: str, k: int = 4) -> list[tuple[Doc, float, list[str]]]:
        q = tokenize(query)
        n = len(self.docs)
        scored: list[tuple[float, str, int, list[str]]] = []
        for i, d in enumerate(self.docs):
            tf = self._tf[i]
            s = 0.0
            matched: list[str] = []
            for t in dict.fromkeys(q):
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg))
                matched.append(t)
            if s > 0:
                scored.append((s, d.id, i, matched))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(self.docs[i], round(s, 4), m) for s, _id, i, m in scored[:k]]


def ids(results: Sequence[tuple[Doc, float, list[str]]]) -> list[str]:
    return [d.id for d, _s, _m in results]

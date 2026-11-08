"""Embeddings for the Store's semantic index (`langgraph.json` store.index.embed).

Anthropic has no embeddings API, so embeddings are configured independently of the chat provider.
`hash` (default): deterministic hashed bag-of-words - lexical, not semantic, but ranks sensibly, free, offline.
DIMS is fixed in langgraph.json; switching embedders changes vector meaning, so wipe the store when you switch.
Keep imports light: the server loads this file by path. Async so the event loop is never blocked.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re

DIMS = 256
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "is",
        "it",
        "my",
        "i",
        "we",
        "you",
        "for",
        "with",
        "that",
        "this",
        "at",
        "be",
        "are",
    ]
)


def _tokens(text: str) -> list[str]:
    toks = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]
    toks = [t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t for t in toks]
    return toks + [f"{a}_{b}" for a, b in itertools.pairwise(toks)]


def hash_embed(text: str) -> list[float]:
    vec = [0.0] * DIMS
    for tok in _tokens(text):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        vec[h % DIMS] += 1.0 if (h >> 31) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


async def aembed(texts: list[str]) -> list[list[float]]:
    return [hash_embed(t) for t in texts]

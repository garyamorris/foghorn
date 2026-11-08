"""Offline-first evaluation harness. Evaluator signature == langsmith's `(inputs, outputs, reference_outputs)`
so the SAME functions run here (CI, pure python) and under `langsmith.evaluate(...)` (online, optional).

Why not just langsmith.evaluate(upload_results=False) in CI? Probed on langsmith 0.14.17: with an empty API key and
LANGSMITH_TRACING=false it still issued GET /info and POST /runs/multipart to api.smith.langchain.com from a
background thread (blocked by the test socket guard). Not acceptable for a 100%-offline gate.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Case:
    id: str
    inputs: dict[str, Any]
    reference: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Score:
    key: str
    score: float  # 0..1
    comment: str | None = None


Evaluator = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], "Score | Sequence[Score]"]
Target = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def load_cases(path: Path) -> list[Case]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            d = json.loads(line)
            out.append(Case(d["id"], d["inputs"], d.get("reference", {}), tuple(d.get("tags", ()))))
    ids = [c.id for c in out]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    return out


@dataclass
class Report:
    rows: list[dict[str, Any]]

    def metric(self, key: str) -> float:
        vals = [s.score for r in self.rows for s in r["scores"] if s.key == key]
        return statistics.fmean(vals) if vals else float("nan")

    def metrics(self) -> dict[str, float]:
        keys = sorted({s.key for r in self.rows for s in r["scores"]})
        return {k: round(self.metric(k), 4) for k in keys}

    def failures(self, key: str) -> list[str]:
        return [r["case"].id for r in self.rows for s in r["scores"] if s.key == key and s.score < 1.0]

    def assert_thresholds(self, thresholds: dict[str, float]) -> None:
        bad = {k: (round(self.metric(k), 4), t) for k, t in thresholds.items() if not self.metric(k) >= t}  # NaN fails
        assert not bad, (
            "eval thresholds violated (metric: (got, min)): "
            + json.dumps(bad)
            + " failing cases: "
            + json.dumps({k: self.failures(k) for k in bad})
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "metrics": self.metrics(),
                "rows": [
                    {"id": r["case"].id, "outputs": r["outputs"], "scores": [s.__dict__ for s in r["scores"]]}
                    for r in self.rows
                ],
            },
            indent=2,
            default=str,
        )


async def run_suite(
    target: Target, cases: Iterable[Case], evaluators: Sequence[Evaluator], *, concurrency: int = 4
) -> Report:
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> dict[str, Any]:
        async with sem:
            try:
                out = await target(c.inputs)
            except Exception as e:
                return {"case": c, "outputs": {"__error__": repr(e)}, "scores": [Score("no_crash", 0.0, repr(e))]}
        scores: list[Score] = [Score("no_crash", 1.0)]
        for ev in evaluators:
            r = ev(c.inputs, out, c.reference)
            scores.extend([r] if isinstance(r, Score) else list(r))
        return {"case": c, "outputs": out, "scores": scores}

    return Report(rows=list(await asyncio.gather(*(one(c) for c in cases))))

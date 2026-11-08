"""Offline behaviour of the lesson distiller (``smart`` role, schema ``Distillation``).

A stateless function of the postmortem in the prompt: lessons come from the postmortem's own ``lessons_learned`` (plus a
fallback derived from its root cause), the incident summary from its summary/root cause/timeline. Nothing comes from
ground truth. Profiles: ``competent`` (and gullible) keep the postmortem's lessons; ``naive`` produces a generic platitude
the human is expected to discard; ``cautious`` keeps only lessons it is confident about.
"""

from __future__ import annotations

import json
import re
from typing import Any

from foghorn.flows.memory_keeper.models import Distillation, LessonCandidate
from foghorn.models.offline import Call, Script, struct

_BLOCK = re.compile(r"<(?P<tag>postmortem|limit)>(?P<body>.*?)</(?P=tag)>", re.S)


def pack(postmortem: dict[str, Any], limit: int) -> str:
    return f"<limit>{limit}</limit>\n<postmortem>{json.dumps(postmortem, sort_keys=True, default=str)}</postmortem>"


def unpack(text: str) -> tuple[dict[str, Any], int]:
    got = {m.group("tag"): m.group("body") for m in _BLOCK.finditer(text)}
    return json.loads(got.get("postmortem", "{}")), int(got.get("limit", "2"))


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", (text or "").strip(), maxsplit=1)[0]


def _confidence(pm: dict[str, Any]) -> float:
    """A postmortem a human edited is worth more than one that was approved as drafted."""
    return {"edited": 0.8, "approved": 0.7}.get(str(pm.get("status")), 0.6)


def incident_summary(pm: dict[str, Any]) -> str:
    actions = [e["summary"] for e in pm.get("timeline") or [] if e.get("kind") == "action"]
    tail = f" Mitigation: {'; '.join(actions)}." if actions else ""
    return (
        f"{pm.get('severity', '')} {pm.get('title', '')}. {_first_sentence(pm.get('summary', ''))} "
        f"Root cause: {_first_sentence(pm.get('root_cause', ''))}{tail}"
    ).strip()


def distill(pm: dict[str, Any], limit: int, profile: str) -> Distillation:
    category = pm.get("category")
    conf = _confidence(pm)
    texts = [t for t in pm.get("lessons_learned") or [] if str(t).strip()]
    if not texts and pm.get("root_cause"):
        texts = [
            f"{_first_sentence(pm['root_cause'])} Check this first when {pm.get('component') or 'the service'} degrades."
        ]
    if profile == "naive":
        cands = [
            LessonCandidate(text="Communicate early and often during an incident.", category=category, confidence=0.4)
        ]
    else:
        cands = [LessonCandidate(text=str(t).strip(), category=category, confidence=conf) for t in texts]
        if profile == "cautious":
            cands = [c for c in cands if c.confidence >= 0.7]
    return Distillation(lessons=cands[: max(0, min(limit, 5))], incident_summary=incident_summary(pm))


def _distill(c: Call) -> Any:
    pm, limit = unpack(c.human)
    return struct(distill(pm, limit, c.profile))


def distiller_script() -> Script:
    return Script().when("distill", lambda c: c.schema_name == "Distillation", _distill)

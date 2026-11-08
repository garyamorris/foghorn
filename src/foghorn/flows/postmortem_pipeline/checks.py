"""Deterministic quality checks over a postmortem draft: the always-on half of the review step.

The ``judge``-role critic adds qualitative issues; these checks are REAL (they parse the draft and compare it with the
facts bundle), so the offline critic is just this function wearing a model costume, and a real critic can never wave
through a draft that fails them.
"""

from __future__ import annotations

import re
from typing import Any

from foghorn.flows.postmortem_pipeline.models import Issue

_BLAME = re.compile(r"\b(negligen\w*|careless\w*|incompeten\w*|fault of|should have known|blam\w+|idiot\w*)\b", re.I)
_UNDETERMINED = re.compile(r"(not (yet )?(determined|established|confirmed)|undetermined|unknown|inconclusive)", re.I)


def _text(draft: dict[str, Any], *fields: str) -> str:
    parts: list[str] = []
    for f in fields:
        v = draft.get(f)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(x if isinstance(x, str) else str(x.get("title", x)) for x in v)
    return "\n".join(parts)


def check_postmortem(draft: dict[str, Any], facts: dict[str, Any]) -> list[Issue]:
    """Blockers (the draft must not go to a human like this) and warnings (the human is told)."""
    issues: list[Issue] = []

    def add(id_: str, field: str, message: str, severity: str = "blocker") -> None:
        issues.append(Issue(id=id_, field=field, message=message, severity=severity))

    comp = facts.get("component")
    root = facts.get("root_cause")
    rc_text = str(draft.get("root_cause") or "")
    if facts.get("report_available") and root and not facts.get("abstained"):
        if not comp or comp.lower() not in rc_text.lower():
            add("root_cause_matches_report", "root_cause", f"root cause must name the commander's finding ({comp})")
    elif not _UNDETERMINED.search(rc_text):
        add(
            "root_cause_undetermined",
            "root_cause",
            "the commander did not establish a root cause (or no report was available): say so explicitly",
        )

    have = {e.get("ref") for e in draft.get("timeline") or []}
    missing = [e["ref"] for e in facts.get("timeline") or [] if e.get("key") and e.get("ref") not in have]
    if missing:
        add("timeline_complete", "timeline", "timeline is missing key events: " + ", ".join(missing[:6]))

    impact = str(draft.get("impact") or "")
    if not re.search(r"\d", impact) and facts.get("impact", {}).get("alerts"):
        add(
            "impact_quantified",
            "impact",
            "impact must be quantified from the alert data (peak value, duration, services)",
        )

    items = draft.get("action_items") or []
    if not items:
        add("action_items_present", "action_items", "a postmortem needs at least one action item")
    known = set(facts.get("known_owner_ids") or [])
    if known:
        unowned = [i.get("title", "?") for i in items if i.get("owner") not in known]
        if unowned:
            add(
                "action_items_owned",
                "action_items",
                f"{len(unowned)} action item(s) lack an owner from the on-call data ({', '.join(sorted(known))}): "
                + "; ".join(u[:60] for u in unowned[:3]),
            )

    if not draft.get("lessons_learned"):
        add(
            "lessons_present",
            "lessons_learned",
            "add at least one lesson learned (it feeds the memory keeper)",
            "warning",
        )
    if not draft.get("what_went_well") or not draft.get("what_went_badly"):
        add("went_well_badly", "what_went_well", "list both what went well and what went badly", "warning")
    blame = _BLAME.search(_text(draft, "summary", "root_cause", "contributing_factors", "what_went_badly"))
    if blame:
        add("blameless", "summary", f"blameless language required, found {blame.group(0)!r}")
    return issues


def blockers(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "blocker"]

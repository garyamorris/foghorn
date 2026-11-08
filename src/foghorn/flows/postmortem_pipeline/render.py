"""Pure markdown rendering of a postmortem dict (what the reviewer reads in the interrupt and what is published)."""

from __future__ import annotations

from typing import Any


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def render_markdown(pm: dict[str, Any], *, open_issues: list[dict[str, Any]] | None = None) -> str:
    lines = [
        f"# Postmortem {pm['incident_id']}: {pm['title']}",
        "",
        f"*{pm.get('severity', '')} - component `{pm.get('component') or 'n/a'}` - revision {pm.get('revision', 1)} - "
        f"{pm.get('status', 'draft')}*",
        "",
        "## Summary",
        pm.get("summary") or "(no summary)",
        "",
        "## Impact",
        pm.get("impact", ""),
        "",
        "## Detection",
        pm.get("detection", ""),
        "",
        "## Root cause",
        pm.get("root_cause", ""),
        "",
        "### Contributing factors",
        _bullets(pm.get("contributing_factors", [])),
        "",
        "## Timeline (virtual time, UTC)",
    ]
    for e in pm.get("timeline", []):
        mark = "*" if e.get("key") else " "
        lines.append(f"- {mark} `{e['t']}` [{e['kind']}] {e['summary']}")
    lines += [
        "",
        "## What went well",
        _bullets(pm.get("what_went_well", [])),
        "",
        "## What went badly",
        _bullets(pm.get("what_went_badly", [])),
        "",
        "## Action items",
    ]
    for a in pm.get("action_items", []):
        lines.append(f"- [{a['priority']}] {a['title']} - owner: {a.get('owner') or 'UNASSIGNED'} ({a['kind']})")
    if not pm.get("action_items"):
        lines.append("- (none)")
    lines += ["", "## Lessons learned", _bullets(pm.get("lessons_learned", []))]
    if open_issues:
        lines += ["", "## Open review issues", *(f"- ({i['severity']}) {i['id']}: {i['message']}" for i in open_issues)]
    return "\n".join(lines) + "\n"

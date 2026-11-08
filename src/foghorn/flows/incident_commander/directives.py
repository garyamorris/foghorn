"""Prompt directives: a tiny grammar inside the editable prompts that visibly changes behaviour (W6 'edit the prompt')."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_POLICIES = ("prefer_flag_revert", "prefer_rollback", "mitigate_before_fix")


@dataclass(frozen=True)
class Directives:
    policy: str | None = None
    risk: str = "normal"
    style: str = "verbose"
    require_evidence: int = 0
    raw: tuple[str, ...] = field(default_factory=tuple)

    def echo(self) -> str:
        return "Active directives: " + "; ".join(self.raw) if self.raw else ""


def parse_directives(*prompts: str | None) -> Directives:
    policy: str | None = None
    risk, style, need = "normal", "verbose", 0
    raw: list[str] = []
    for text in prompts:
        for line in (text or "").splitlines():
            line = line.strip()
            if m := re.fullmatch(r"policy:\s*(\w+)", line):
                if m.group(1) in _POLICIES:
                    policy = m.group(1)
                    raw.append(f"policy={policy}")
            elif m := re.fullmatch(r"risk:\s*(strict|normal)", line):
                risk = m.group(1)
                raw.append(f"risk={risk}")
            elif m := re.fullmatch(r"style:\s*(terse|verbose)", line):
                style = m.group(1)
                raw.append(f"style={style}")
            elif m := re.fullmatch(r"require_evidence\s*>=\s*(\d+)", line):
                need = int(m.group(1))
                raw.append(f"require_evidence>={need}")
    return Directives(policy, risk, style, need, tuple(dict.fromkeys(raw)))

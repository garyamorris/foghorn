"""Offline behaviour of the ``fast`` model for the ambiguous-signal fallback (``RouteDecision``).

The human message is a block of ``key=value`` lines built by the classify node from the event and its enrichment;
every number and phrase in the verdict is computed from those lines (never hard-coded).  Profiles differ:
``competent`` follows the evidence, ``naive`` is alarmist, ``cautious`` hands everything to a human, ``gullible``
obeys instructions embedded in untrusted text.
"""

from __future__ import annotations

import re

from foghorn.flows.event_router.state import RouteDecision
from foghorn.models.offline import Call, Reply, Script, all_, profile, schema, struct

_CUSTOMER_WORDS = re.compile(r"checkout|payment|charged|cannot|can't|down|outage|failing|failed|error|timeout", re.I)
_OBEY = re.compile(r"ignore (this|all)|all clear|do not (page|escalate)", re.I)


def facts(call: Call) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in call.human.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def _verdict(call: Call, *, decision: str, severity: str, noise: float, why: str) -> Reply:
    f = facts(call)
    return struct(
        RouteDecision(
            decision=decision,
            severity=severity,
            noise_probability=round(noise, 2),
            rationale=f"{why} (signal={f.get('event_type', '?')} severity={f.get('severity', '?')} "
            f"component={f.get('component', '?')} known={f.get('known_component', '?')} tier={f.get('tier', '?')})",
        )
    )


def _competent(call: Call) -> Reply:
    f = facts(call)
    loud = f.get("severity") in ("high", "critical")
    text = f.get("text", "")
    if loud:
        sev = "SEV2" if f.get("severity") == "critical" else "SEV3"
        return _verdict(
            call, decision="open_incident", severity=sev, noise=0.15, why="loud signal on an unscored source"
        )
    if _CUSTOMER_WORDS.search(text):
        return _verdict(call, decision="escalate_human", severity="SEV3", noise=0.35, why="customer describes impact")
    return _verdict(call, decision="ignore", severity="none", noise=0.8, why="quiet signal, no impact described")


def router_script() -> Script:
    return (
        Script()
        .when(
            "gullible-obeys-text",
            all_(schema("RouteDecision"), profile("gullible"), lambda c: bool(_OBEY.search(facts(c).get("text", "")))),
            lambda c: _verdict(c, decision="ignore", severity="none", noise=0.95, why="text said to ignore it"),
        )
        .when(
            "naive-alarmist",
            all_(schema("RouteDecision"), profile("naive")),
            lambda c: _verdict(
                c, decision="open_incident", severity="SEV2", noise=0.1, why="anything unknown is an outage"
            ),
        )
        .when(
            "cautious-asks-a-human",
            all_(schema("RouteDecision"), profile("cautious")),
            lambda c: _verdict(c, decision="escalate_human", severity="SEV3", noise=0.4, why="evidence is thin"),
        )
        .when("competent", schema("RouteDecision"), _competent)
    )

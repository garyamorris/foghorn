"""People, rotas and the on-call schedule (D11 ``people``, ``rotas``)."""

from __future__ import annotations

from typing import Any

from foghorn.data.util import to_epoch, to_iso

#: weekly rotation, handoff Monday 09:00 UTC; this Monday is week 0
ROTA_ANCHOR = "2026-01-05T09:00:00Z"
WEEK_S = 7 * 86400

PEOPLE: list[dict[str, Any]] = [
    {
        "id": "p.raman",
        "name": "Priya Raman",
        "title": "DBA on-call",
        "roles": ["dba", "approver_high"],
        "rotas": ["rota-dba"],
        "timezone": "Europe/London",
        "slack": "@priya",
    },
    {
        "id": "m.lee",
        "name": "Marcus Lee",
        "title": "DBA",
        "roles": ["dba"],
        "rotas": ["rota-dba", "rota-platform"],
        "timezone": "America/New_York",
        "slack": "@marcus",
    },
    {
        "id": "a.bello",
        "name": "Aisha Bello",
        "title": "Payments lead",
        "roles": ["payments_lead", "approver_high", "incident_commander"],
        "rotas": ["rota-payments", "rota-commander"],
        "timezone": "Europe/London",
        "slack": "@aisha",
    },
    {
        "id": "t.kowalski",
        "name": "Tomasz Kowalski",
        "title": "Payments engineer",
        "roles": ["service_oncall"],
        "rotas": ["rota-payments", "rota-commerce"],
        "timezone": "Europe/Warsaw",
        "slack": "@tomasz",
    },
    {
        "id": "j.okafor",
        "name": "Jordan Okafor",
        "title": "Pricing engineer, flag owner",
        "roles": ["service_oncall", "flag_owner"],
        "rotas": ["rota-growth", "rota-commerce"],
        "timezone": "America/Chicago",
        "slack": "@jordan",
    },
    {
        "id": "e.petrova",
        "name": "Elena Petrova",
        "title": "Commerce engineer",
        "roles": ["service_oncall"],
        "rotas": ["rota-commerce", "rota-fulfilment", "rota-messaging"],
        "timezone": "Europe/Berlin",
        "slack": "@elena",
    },
    {
        "id": "s.whitfield",
        "name": "Sam Whitfield",
        "title": "Platform SRE, incident commander",
        "roles": ["service_oncall", "incident_commander"],
        "rotas": ["rota-platform", "rota-commander"],
        "timezone": "America/Los_Angeles",
        "slack": "@sam",
    },
    {
        "id": "n.adeyemi",
        "name": "Ngozi Adeyemi",
        "title": "Messaging SRE",
        "roles": ["service_oncall"],
        "rotas": ["rota-messaging", "rota-growth", "rota-fulfilment"],
        "timezone": "Africa/Lagos",
        "slack": "@ngozi",
    },
]

ROTAS: list[dict[str, Any]] = [
    {"id": "rota-commerce", "team": "commerce", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-payments", "team": "payments", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-platform", "team": "platform", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-dba", "team": "data-infra", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-growth", "team": "growth", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-fulfilment", "team": "fulfilment", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-messaging", "team": "messaging", "handoff": "Mon 09:00 UTC"},
    {"id": "rota-commander", "team": "sre", "handoff": "Mon 09:00 UTC"},
]


def rota_members(rota_id: str) -> list[dict[str, Any]]:
    return sorted((p for p in PEOPLE if rota_id in p["rotas"]), key=lambda p: p["id"])


def rota_records() -> list[dict[str, Any]]:
    return [{**r, "members": [p["id"] for p in rota_members(r["id"])], "rotation": "weekly"} for r in ROTAS]


def people_seed_records() -> list[dict[str, Any]]:
    """D11 ``people.jsonl``: Store items in namespace ("people",)."""
    out = []
    for p in PEOPLE:
        text = f"{p['name']}, {p['title']}; roles {', '.join(p['roles'])}; rotas {', '.join(p['rotas'])}; timezone {p['timezone']}."
        out.append(
            {
                "namespace": ["people"],
                "key": f"person-{p['id']}",
                "value": {
                    "text": text,
                    "name": p["name"],
                    "person_id": p["id"],
                    "roles": p["roles"],
                    "rotas": p["rotas"],
                    "timezone": p["timezone"],
                    "source": "seed",
                },
            }
        )
    return out


def oncall_at(rota_id: str, as_of_iso: str) -> dict[str, Any]:
    """Who is on call for ``rota_id`` at the virtual time ``as_of`` (deterministic weekly rotation)."""
    members = rota_members(rota_id)
    if not members:
        return {"rota": rota_id, "primary": None, "secondary": None, "shift_started_at": None}
    epoch = to_epoch(as_of_iso)
    anchor = to_epoch(ROTA_ANCHOR)
    week = (epoch - anchor) // WEEK_S
    primary = members[week % len(members)]
    secondary = members[(week + 1) % len(members)] if len(members) > 1 else None
    return {
        "rota": rota_id,
        "primary": _public(primary),
        "secondary": _public(secondary) if secondary else None,
        "shift_started_at": to_iso(anchor + week * WEEK_S),
        "rotation": "weekly",
    }


def _public(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "name": p["name"],
        "roles": list(p["roles"]),
        "timezone": p["timezone"],
        "contact": p["slack"],
    }


def people_with_role(role: str) -> list[dict[str, Any]]:
    return [_public(p) for p in PEOPLE if role in p["roles"]]

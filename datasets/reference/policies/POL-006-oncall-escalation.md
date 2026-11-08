---
id: "POL-006"
title: "On-call and escalation"
ack_minutes: {"SEV1": 5, "SEV2": 10, "SEV3": 30, "SEV4": 240}
rotation: "weekly, handoff Monday 09:00 UTC"
roles: {"service_oncall": "owns the component's rota", "incident_commander": "owns SEV1-2 coordination", "dba": "required for datastore changes"}
---

# POL-006: On-call and escalation

Escalate to a human when evidence is missing or contradictory.
Never act on a component you cannot observe.

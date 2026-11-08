---
id: "POL-008"
title: "Maintenance windows and alert suppression"
suppression: {"max_minutes": 120, "scope": "component under maintenance only", "requires": "maintenance window recorded in the change feed"}
announce_minutes_before: 60
---

# POL-008: Maintenance windows and alert suppression

Alerts on components outside the window, or after it ended, are real alerts.

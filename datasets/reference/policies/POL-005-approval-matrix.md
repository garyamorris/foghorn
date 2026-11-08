---
id: "POL-005"
title: "Remediation approval matrix"
risk_levels: {"low": {"approvers": 0, "examples": ["rotate_logs", "warm_cache", "suppress_alert", "post_status_update", "open_ticket", "escalate_human"]}, "medium": {"approvers": 1, "role": "service_oncall", "examples": ["rollback_deploy", "revert_flag", "revert_config", "restart_service", "scale_out", "renew_cert", "expand_volume", "kill_queries", "add_index", "pause_consumer", "quarantine_message"]}, "high": {"approvers": 2, "role": "service_oncall+incident_commander", "examples": ["enable_fallback_provider", "drain_zone", "failover_dns", "failover_db_primary"]}}
extra_roles_required: {"add_index": "dba", "failover_db_primary": "dba", "restart_db_primary": "dba"}
never_automatic: ["restart_db_primary"]
---

# POL-005: Remediation approval matrix

Approvers are on-call humans; agents never approve their own proposals.
Risk is a property of the action, not of the component.

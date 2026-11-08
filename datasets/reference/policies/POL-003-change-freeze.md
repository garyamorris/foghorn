---
id: "POL-003"
title: "Change freeze"
windows: [{"name": "Peak trading", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "09:00", "end": "18:00", "tz": "UTC"}]
during_freeze: {"rollbacks": "allowed", "break_glass_required": ["add_index", "failover_db_primary", "restart_db_primary", "scale_out_datastore", "kill_queries_datastore"]}
break_glass: {"reason_min_chars": 20, "second_approver": true}
---

# POL-003: Change freeze

Rollbacks and reverts are always allowed. Manual intervention on a tier-0 datastore during peak trading needs break-glass: a written reason of at least 20 characters and a second approver.

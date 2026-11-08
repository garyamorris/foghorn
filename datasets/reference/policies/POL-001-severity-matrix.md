---
id: "POL-001"
title: "Severity matrix"
levels: {"SEV1": "Revenue path fully down or data loss; all hands; executive notification", "SEV2": "Major degradation of a tier-0 revenue path (checkout, payments) or large customer impact", "SEV3": "Partial degradation, single non-revenue component, workaround exists", "SEV4": "Minor or cosmetic, no customer impact"}
rules: ["tier-0 component error_rate above 5 percent for 5 minutes is at least SEV2", "revenue path unavailable is SEV1", "planned maintenance has no severity"]
---

# POL-001: Severity matrix

Severity is set by triage and revised by the incident commander.
Event severity (how loud a signal is) is not incident severity (business impact).

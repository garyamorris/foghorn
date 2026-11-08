"""Status-page drafting and the POL-002 lint (pure functions).

POL-002: status page required for SEV1/SEV2; at most 600 characters; factual, no speculation about the root cause; never
an 'all clear' before recovery is verified; no internal component names or people; no data-breach talk unless security
confirmed it. The draft describes what CUSTOMERS see, so internal components are translated to customer-facing terms and
only components with confirmed impact (material, queried metrics) are mentioned, never suspects or red herrings.
"""

from __future__ import annotations

import re
from typing import Any

from foghorn.data import catalog
from foghorn.flows.rca_tournament.facts import Facts

MAX_CHARS = 600
STATUS_REQUIRED = frozenset({"SEV1", "SEV2"})
#: internal component -> what a customer experiences (None = invisible to customers)
CUSTOMER_TERMS: dict[str, str | None] = {
    "checkout-service": "checkout",
    "payments-service": "payment",
    "payment-provider": "payment",
    "cart-service": "cart",
    "pricing-service": "pricing",
    "auth-service": "sign-in",
    "sessions-cache": "sign-in",
    "search-service": "search",
    "notification-service": "email notifications",
    "event-bus": "email notifications",
    "inventory-service": "stock availability",
    "shipping-service": "delivery estimates",
    "orders-service": "order placement",
    "user-profile": "account pages",
    "catalog-service": "browsing",
    "web-frontend": None,
    "edge-gateway": None,
    "orders-db": None,
    "recommendation-service": None,
    "email-provider": None,
}
_SPECULATION = re.compile(
    r"\b(root cause|caused by|because of|due to (?:a|the) |bug|deploy(?:ment)?|rollback|misconfig)", re.I
)
_ALL_CLEAR = re.compile(r"all[ -]clear|fully (?:resolved|restored)|issue (?:is )?resolved|back to normal", re.I)
_PEOPLE = re.compile(r"\b[a-z]\.[a-z]{3,}\b|@[\w.-]+")
_BREACH = re.compile(r"\bbreach|\bcompromis", re.I)


PRIORITY = (
    "checkout",
    "payment",
    "sign-in",
    "cart",
    "order placement",
    "search",
    "email notifications",
    "pricing",
    "stock availability",
    "delivery estimates",
    "account pages",
    "browsing",
)


def affected_terms(f: Facts, limit: int = 4) -> list[str]:
    """Customer-facing capabilities with CONFIRMED degradation (a material symptom metric, or a non-trivial alert that fired),
    most important first. Suspects and red herrings never appear."""
    comps: set[str] = set()
    for c, a in f.anomalies().items():
        if any(m in ("error_rate", "p95_ms") for m in a.signal_metrics) or c in ("sessions-cache", "event-bus"):
            comps.add(c)
    for al in f.alerts:
        if (
            al["rule"] != "synthetic_probe_failed"
            and al.get("metric") in ("error_rate", "p95_ms", "hit_ratio")
            and al["component"] in catalog.COMPONENTS
        ):
            comps.add(al["component"])
    found = {t for c in comps if (t := CUSTOMER_TERMS.get(c))}
    return sorted(found, key=lambda t: (PRIORITY.index(t) if t in PRIORITY else 99, t))[:limit]


def required(severity: str) -> bool:
    return severity in STATUS_REQUIRED


def draft_status(terms: list[str], severity: str, *, recovered: bool = False, style: str = "verbose") -> str:
    if not terms:
        terms = ["some features"]
    what = ", ".join(terms[:-1]) + (" and " if len(terms) > 1 else "") + terms[-1]
    verb = "are" if len(terms) > 1 or terms[-1].endswith("s") else "is"
    if recovered:
        return (
            f"Update: we have mitigated the issue affecting {what}. We are monitoring closely while service "
            "returns to its usual performance and will confirm once monitoring is complete."
        )
    if style == "terse":
        return (
            f"We are investigating: {what} {verb} currently degraded for some customers. Next update within 30 minutes."
        )
    return (
        f"We are investigating an issue where {what} {verb} currently degraded for some customers. "
        "Our engineers are working on a fix. We will share another update within 30 minutes."
    )


def lint_status(text: str, *, recovered: bool = False, max_chars: int = MAX_CHARS) -> list[str]:
    """POL-002 violations found in ``text`` (empty list = publishable)."""
    out: list[str] = []
    if len(text) > max_chars:
        out.append(f"longer than {max_chars} characters")
    if _ALL_CLEAR.search(text) and not recovered:
        out.append("declares an all clear before recovery is verified")
    if _SPECULATION.search(text):
        out.append("speculates about the cause")
    low = text.lower()
    names = sorted(c for c in catalog.COMPONENT_IDS if c in low)
    if names:
        out.append("names internal components: " + ", ".join(names))
    if _PEOPLE.search(text):
        out.append("names people")
    if _BREACH.search(text):
        out.append("mentions a data breach")
    return out


def sanitize_status(text: str, terms: list[str], severity: str) -> str:
    """Replace a draft that fails the lint by a compliant one built from the confirmed customer-facing terms."""
    return draft_status(terms, severity)


def comms_rules(severity: str) -> dict[str, Any]:
    return {"status_page_required": required(severity), "max_chars": MAX_CHARS, "policy": "POL-002"}

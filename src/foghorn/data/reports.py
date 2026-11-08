"""Customer reports (the human voice): per-category templates, generated deterministically."""

from __future__ import annotations

from typing import Any

from foghorn.data.util import hex_id, rng

#: component -> what a customer calls it
PUBLIC_NAME: dict[str, str] = {
    "web-frontend": "the website",
    "edge-gateway": "the site",
    "auth-service": "sign-in",
    "catalog-service": "product pages",
    "search-service": "search",
    "cart-service": "the cart",
    "checkout-service": "checkout",
    "payments-service": "payment",
    "inventory-service": "stock availability",
    "pricing-service": "pricing",
    "recommendation-service": "recommendations",
    "notification-service": "email notifications",
    "orders-service": "orders",
    "shipping-service": "delivery tracking",
    "user-profile": "my account",
    "orders-db": "orders",
    "sessions-cache": "sign-in",
    "event-bus": "email notifications",
    "payment-provider": "payment",
    "email-provider": "email notifications",
}

_T: dict[str, list[str]] = {
    "generic": [
        "I get an error on {area} and cannot continue (ref {ref}).",
        "{Area} has been failing for the last few minutes, is something wrong?",
        "Tried {area} on the app and the website, both fail.",
        "{Area} is really slow today, timed out twice.",
    ],
    "payment": [
        "My card payment fails at the last step with 'payment could not be processed' (ref {ref}).",
        "Payment keeps failing, I tried two different cards.",
        "Checkout takes forever and then says payment unavailable.",
        "Got charged-pending message but no order confirmation (ref {ref}).",
    ],
    "email": [
        "I never got my order confirmation email (ref {ref}).",
        "No shipping update email for my order since this morning.",
        "Password reset email has not arrived after 20 minutes.",
    ],
    "slow": [
        "The whole site feels sluggish, pages take 10+ seconds.",
        "{Area} is much slower than usual (ref {ref}).",
        "Order history page keeps spinning.",
    ],
    "signin": [
        "I keep being asked to sign in again and it is very slow.",
        "Sign-in takes ages and then times out (ref {ref}).",
        "Logged out of my cart twice in five minutes.",
    ],
    "partial": [
        "Some pages load and some give an error, very intermittent (ref {ref}).",
        "Can't load the site from my phone, desktop works sometimes.",
        "Getting random errors everywhere in the app.",
        "Checkout page blank for me, home page fine.",
    ],
    "unknown_state": [
        "My payment was declined but my bank shows a pending charge (ref {ref}).",
        "Order stuck on 'processing' for 15 minutes, did it go through?",
        "I was charged and got no confirmation, please check order ref {ref}.",
        "Payment page spins forever, I'm afraid to retry in case I get charged twice.",
        "Confirmation never arrived, support chat says system issue?",
    ],
}
_VOICE = {
    "cert_expiry": "payment",
    "dependency_outage": "payment",
    "disk_full": "email",
    "queue_poison_message": "email",
    "cache_stampede": "signin",
    "slow_query_migration": "slow",
    "memory_leak": "generic",
    "config_change": "generic",
    "bad_deploy": "generic",
    "network_partition": "partial",
    "telemetry_gap": "unknown_state",
    "runaway_query": "slow",
    "db_pool_exhaustion": "generic",
    "planned_maintenance": "slow",
}
_CHANNELS = ("chat", "email", "chat", "phone", "social")


def generate_reports(
    seed: int, sid: str, category: str, root: str, onset_s: int, window_end_s: int, params: dict[str, Any]
) -> list[dict[str, Any]]:
    if category == "planned_maintenance":
        return []
    r = rng(seed, sid, "reports")
    count = int(params.get("reports", 9 if category == "telemetry_gap" else r.randint(3, 6)))
    voice = params.get("report_voice", _VOICE[category])
    area = params.get("report_area") or PUBLIC_NAME.get(root, "the site")
    pool = _T[voice] + (_T["generic"] if voice == "generic" else [])
    first = int(params.get("reports_first_min", 3 if category != "telemetry_gap" else 4))
    spread = int(params.get("reports_span_min", 14))
    out: list[dict[str, Any]] = []
    times = sorted(onset_s + (first + r.randint(0, spread)) * 60 + r.randint(0, 59) for _ in range(count))
    for i, ts in enumerate(times):
        if ts >= window_end_s:
            continue
        ref = "ord_" + hex_id(sid, "ref", i, n=6)
        text = pool[r.randrange(len(pool))].format(area=area, Area=area[:1].upper() + area[1:], ref=ref)
        out.append(
            {
                "ticket_id": f"CS-{sid}-{i + 1:04d}",
                "ts": ts,
                "channel": _CHANNELS[r.randrange(len(_CHANNELS))],
                "text": text,
                "customer_tier": "plus" if r.random() < 0.25 else "standard",
                "order_ref": ref,
                "severity": "low" if i < 2 else "medium",
            }
        )
    return out

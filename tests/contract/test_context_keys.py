"""Every graph must accept every run-context key the dispatcher can send.

`context_schema(**ctx)` raises on unknown keys, which fails the whole run (three builders hit this independently).
The dispatcher passes the same eight keys to whatever graph it starts, so each graph's context schema must DECLARE
all of them (it may ignore the ones it does not use). Adding a `CTX_*` key to `contracts/meta.py` and to
`events/routing.py::run_context` makes this test fail for every flow that was not updated - which is the point.
"""

from __future__ import annotations

import pytest

from foghorn.contracts import meta
from tests.contract.test_langgraph_json import GRAPHS, _cwd_is_root, _load_like_server  # noqa: F401

DISPATCHER_KEYS = {
    meta.CTX_SCENARIO_ID,
    meta.CTX_AS_OF,
    meta.CTX_MODEL_PROFILE,
    meta.CTX_APPROVAL_POLICY,
    meta.CTX_BREAKPOINTS,
    meta.CTX_MEMORY_ENABLED,
    meta.CTX_CHAOS,
    meta.CTX_TOKEN_DELAY_MS,
}


@pytest.mark.parametrize("gid", sorted(GRAPHS))
def test_graph_context_schema_declares_every_dispatcher_key(gid):
    graph = _load_like_server(gid)
    props = set((graph.get_context_jsonschema() or {}).get("properties", {}))
    missing = DISPATCHER_KEYS - props
    assert not missing, f"{gid}: context_schema does not declare {sorted(missing)} (a run with them would crash)"


def test_dispatcher_keys_cover_everything_run_context_can_emit():
    """If run_context() grows a key, this set (and every flow's Context) must grow with it."""
    from foghorn.events.config import EventsSettings
    from foghorn.events.routing import run_context

    ctx = run_context(
        {"trace": {"scenario_id": "S01"}},
        {meta.CTX_BREAKPOINTS: ["x"], meta.CTX_MEMORY_ENABLED: True, meta.CTX_CHAOS: "tools", meta.CTX_AS_OF: "t"},
        EventsSettings(),
    )
    assert set(ctx) <= DISPATCHER_KEYS

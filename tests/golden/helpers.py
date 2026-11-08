from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).parent / "data"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RUN = re.compile(r"(lc_run--|run-)[0-9a-f-]{20,}")


def normalise(obj: Any) -> Any:
    """Strip the only non-deterministic things LangGraph/LangChain put in a payload: uuid4/uuid6 ids."""
    s = json.dumps(obj, sort_keys=True, default=str)
    return json.loads(_RUN.sub(r"\1<id>", _UUID.sub("<uuid>", s)))


def assert_golden(name: str, obj: Any) -> None:
    path = GOLDEN / f"{name}.json"
    actual = normalise(obj)
    if os.environ.get("UPDATE_GOLDENS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    assert path.exists(), f"missing golden {path}; run UPDATE_GOLDENS=1 pytest and review the diff"
    assert actual == json.loads(path.read_text()), (
        f"golden drift in {name}; if intended: UPDATE_GOLDENS=1 pytest, then review git diff"
    )

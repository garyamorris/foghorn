"""Regenerate langgraph.uv.json (reproducible `uv export --frozen` Docker builds) from langgraph.json.

The two files must stay identical except `dependencies: ["."]` (editable install) vs `source: {kind: uv}` (locked
install). A contract test fails when they drift; run `make sync-uv-config` after editing langgraph.json.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build() -> dict[str, object]:
    cfg = json.loads((ROOT / "langgraph.json").read_text())
    cfg.pop("dependencies", None)
    return {"source": {"kind": "uv", "root": "."}, **cfg}


if __name__ == "__main__":
    (ROOT / "langgraph.uv.json").write_text(json.dumps(build(), indent=2) + "\n")
    print("wrote langgraph.uv.json")

"""Generated documentation must match its sources, so docs and the dashboard's Studio Guide page cannot drift."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_studio_guide_is_up_to_date_with_the_per_graph_guides():
    spec = importlib.util.spec_from_file_location("build_studio_guide", ROOT / "scripts" / "build_studio_guide.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (ROOT / "docs" / "STUDIO_GUIDE.md").read_text() == mod.render(), "run `make studio-guide`"


def test_every_graph_has_a_studio_guide():
    from foghorn.contracts.meta import GRAPH_IDS

    missing = [g for g in GRAPH_IDS if not (ROOT / "src" / "foghorn" / "flows" / g / "GUIDE.md").exists()]
    assert not missing, f"graphs without a GUIDE.md: {missing}"

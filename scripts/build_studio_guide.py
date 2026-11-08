"""Assemble docs/STUDIO_GUIDE.md from the per-graph guides in src/foghorn/flows/*/GUIDE.md.

The same files feed the dashboard's Studio Guide page (`GET /api/studio/guide`), so the docs and the UI cannot
disagree. Run `make studio-guide` after editing a GUIDE.md; a test fails when the committed file is stale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / "src" / "foghorn" / "flows"
ORDER = [
    "event_router",
    "incident_commander",
    "rca_tournament",
    "storm_correlator",
    "change_gate",
    "postmortem_pipeline",
    "ops_copilot",
    "shift_handoff",
    "memory_keeper",
    "eval_runner",
]
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)

INTRO = """# Foghorn in LangGraph Studio

Studio is the inspector for everything in this deployment: structure, threads, state, interrupts, forks, memory.
Start the server and open it:

```bash
make dev                      # langgraph dev on http://127.0.0.1:2026 (offline, deterministic, tracing off)
```

Studio URL: <https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2026> (needs a LangSmith login in the
browser; Chrome recommended - Safari and Brave block loopback servers, use `langgraph dev --tunnel` there). The
dashboard at <http://127.0.0.1:2026/app/> is the product UI; Studio is the developer view of the same graphs.

Things to try in any graph: switch graphs in the top-left selector; open **Manage Assistants** (named assistants for
each model profile and approval policy are seeded, and prompts are editable via the gear icon on a node); add an
**Interrupt** breakpoint before a node; **edit state** at a checkpoint and **fork**; re-run from a past checkpoint;
toggle streaming; browse **long-term memory**.

The model profiles (`competent`, `naive`, `cautious`, `gullible`) change how the offline "model" reasons: pick them
in the assistant's context. Real Claude is opt-in with `FOGHORN_PROVIDER=auto` and an `ANTHROPIC_API_KEY`.

### If Studio says "Failed to initialize Studio / Failed to fetch"

Studio is a public https page calling a server on your own machine, so the **browser** must allow it. The server side
is already correct (it answers Studio's CORS preflight, including `Access-Control-Request-Private-Network`; an
end-to-end test guards that). Check, in order:

1. The server is running (`curl http://127.0.0.1:2026/ok`) and the URL's `baseUrl` matches its port.
2. **Chrome asks to let `smith.langchain.com` "access other apps and services on this device"** (Local Network
   Access): choose **Allow**. If you dismissed it, reset it under the padlock icon -> Site settings.
3. Ad blockers, privacy extensions and corporate policies that block requests to `127.0.0.1`/`localhost` show up as
   `net::ERR_BLOCKED_BY_CLIENT` in DevTools -> Network: disable them for `smith.langchain.com`.
4. Safari and Brave block loopback entirely: use Chrome, or `langgraph dev --tunnel` (this exposes the server
   publicly while the tunnel is up - the dashboard routes are unauthenticated unless you set
   `FOGHORN_DASHBOARD_API_TOKEN`).
"""


def load() -> list[tuple[dict[str, Any], str]]:
    guides: dict[str, tuple[dict[str, Any], str]] = {}
    for path in sorted(FLOWS.glob("*/GUIDE.md")):
        m = _FRONT.match(path.read_text())
        if not m:
            raise SystemExit(f"{path}: missing YAML front matter")
        meta = yaml.safe_load(m.group(1)) or {}
        guides[str(meta.get("graph_id") or path.parent.name)] = (meta, m.group(2).strip())
    return [guides[g] for g in ORDER if g in guides] + [v for k, v in guides.items() if k not in ORDER]


def render() -> str:
    guides = load()
    out = [INTRO, "## Graphs\n"]
    for meta, _ in guides:
        out.append(f"- [`{meta['graph_id']}`](#{meta['graph_id'].replace('_', '-')}) - {meta.get('title', '')}")
    out.append("")
    for meta, body in guides:
        gid = meta["graph_id"]
        out += [f"## {gid}", "", f"**{meta.get('title', gid)}.** {meta.get('summary', '')}", ""]
        feats = meta.get("features") or []
        if feats:
            out += ["Features: " + ", ".join(f"`{f}`" for f in feats), ""]
        steps = meta.get("studio_steps") or []
        if steps:
            out += ["### What to click in Studio", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)] + [""]
        samples = meta.get("sample_inputs") or []
        if samples:
            out += ['### Sample inputs (paste into the input form, "View Raw")', ""]
            for s in samples:
                out += [
                    f"**{s.get('name', 'sample')}**",
                    "",
                    "```json",
                    json.dumps(s.get("input"), indent=2),
                    "```",
                    "",
                ]
        if body:
            out += ["### Notes", "", re.sub(r"^#{1,2} ", "#### ", body, flags=re.M), ""]
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    (ROOT / "docs" / "STUDIO_GUIDE.md").write_text(render())
    print("wrote docs/STUDIO_GUIDE.md")

#!/usr/bin/env python3
"""make_trace_data.py: pull ONE real Foghorn thread from LangSmith (read-only) and write a compact trace_data_hero.json for trace.html.

    cd /Users/garyamorris/Developer/repos/langgraph-app
    .venv/bin/python -I docs/demo/video-source/viz/make_trace_data.py                  # hero thread, project foghorn-video
    .venv/bin/python -I docs/demo/video-source/viz/make_trace_data.py --thread <thread_id> --out docs/demo/video-source/viz/other.json

Needs LANGSMITH_API_KEY in the environment (the SDK reads it; it is never printed or written). Every number and text in the output
comes from LangSmith run data: names, run types, start/end offsets, token counts, cost, model name, tool args/results, prompt and reply
excerpts. Nothing is invented. Folding done here (so the page only draws what matters):
  * middleware / parser wrappers (Analyst.*, RunnableSequence, _check_refusal, PydanticToolsParser) are folded into their parent
  * `X > X` (a subgraph whose only child is the same name) is shown once
  * `model > Analyst.awrap_model_call > ChatOpenAI` is shown as one LLM row `model`
  * `tools > Analyst.awrap_tool_call > <tool>` is shown as the tool row, and parallel calls of one tool become `<tool> xN`
list_runs(trace_id=...) already returns inputs/outputs/extra/error for every run, so only two kinds of calls are made: one
root-run query by metadata.thread_id, and one list_runs per trace (paged by the SDK, 100 runs per page).
"""
import argparse, ast, datetime as dt, json, os, re, sys, warnings

warnings.filterwarnings("ignore")
from langsmith import Client

HERE = os.path.dirname(os.path.abspath(__file__))
HERO_THREAD = "22b4ce55-d28a-5cfd-a521-daa1c1761802"
HIDE_NAMES = {"RunnableSequence", "_check_refusal", "PydanticToolsParser", "RunnableLambda", "RunnableParallel"}

ap = argparse.ArgumentParser()
ap.add_argument("--thread", default=HERO_THREAD)
ap.add_argument("--project", default=os.environ.get("LANGSMITH_PROJECT") or "foghorn-video")
ap.add_argument("--out", default=os.path.join(HERE, "trace_data_hero.json"))
args = ap.parse_args()


def ms(a, b):
    return round((b - a).total_seconds() * 1000)


def trim(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def headtail(s, h, t):
    s = " ".join(str(s).split())
    return s if len(s) <= h + t + 3 else s[:h].rstrip() + " … " + s[-t:].lstrip()


def num(x, nd=6):
    return round(float(x), nd) if x is not None else 0.0


def hidden(r):
    return r.run_type == "parser" or r.name in HIDE_NAMES or r.name.startswith("Analyst.")


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "output_text") and c.get("text"):
                    parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        return " ".join(parts)
    return "" if content is None else str(content)


def msg_parts(m):
    k = m.get("kwargs") or {}
    role = (m.get("id") or ["", "", "", ""])[-1].replace("Message", "").lower()
    return role, k


def fmt_val(v):
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {fmt_val(x)}" for k, x in v.items()) + "}"
    return str(v)


def llm_detail(full):
    """prompt excerpt + reply for one LLM run."""
    inv = (full.extra or {}).get("invocation_params") or {}
    md = (full.extra or {}).get("metadata") or {}
    msgs = ((full.inputs or {}).get("messages") or [[]])[0]
    roles, last, sysmsg = [], None, ""
    for m in msgs:
        role, k = msg_parts(m)
        roles.append(role)
        if role == "system" and not sysmsg:
            sysmsg = text_of(k.get("content"))
        last = (role, text_of(k.get("content")), k)
    # compress the role list: system, human, ai, tool, tool, tool -> "system · human · ai · tool×3"
    ctx, prev = [], None
    for r_ in roles:
        if ctx and prev == r_:
            ctx[-1][1] += 1
        else:
            ctx.append([r_, 1])
        prev = r_
    ctx = [r_ if n == 1 else f"{r_}×{n}" for r_, n in ctx]
    gen = (((full.outputs or {}).get("generations") or [[{}]])[0] or [{}])[0]
    mk = (gen.get("message") or {}).get("kwargs") or {}
    reply_text = text_of(mk.get("content")) or gen.get("text") or ""
    calls = []
    for tc in mk.get("tool_calls") or []:
        calls.append({"name": tc.get("name"), "args": [[k, trim(fmt_val(v), 170)] for k, v in (tc.get("args") or {}).items()]})
    forced = ((inv.get("tool_choice") or {}).get("function") or {}).get("name") if isinstance(inv.get("tool_choice"), dict) else None
    bound = [((t.get("function") or {}).get("name")) for t in (inv.get("tools") or []) if isinstance(t, dict)]
    cd = getattr(full, "completion_token_details", None) or {}
    det = {
        "model": inv.get("model_name") or inv.get("model") or md.get("ls_model_name"),
        "ptok": full.prompt_tokens or 0, "ctok": full.completion_tokens or 0,
        "reasoning": (cd.get("reasoning") if isinstance(cd, dict) else 0) or 0,
        "cost": num(full.total_cost),
        "ctx": ctx, "n_msgs": len(msgs),
        "sys": trim(sysmsg, 190),
        "last_role": last[0] if last else "", "last_len": len(last[1]) if last else 0,
        "last": headtail(last[1], 170, 230) if last else "",
        "reply": headtail(reply_text, 330, 90) if reply_text else "",
        "calls": calls,
        "forced": forced, "bound": [b for b in bound if b],
        "node": md.get("langgraph_node"),
    }
    return det


def tool_detail(full):
    out = full.outputs or {}
    o = out.get("output", out) if isinstance(out, dict) else out
    content = o.get("content") if isinstance(o, dict) else o
    content = text_of(content) if not isinstance(content, str) else content
    inp = full.inputs or {}
    a = [[k, trim(fmt_val(v), 90)] for k, v in inp.items() if k != "config"]
    return {"args": a, "out": headtail(content, 190, 80), "out_len": len(content or "")}


def extract_interrupt(err):
    """GraphInterrupt((Interrupt(value={...}, id='..'),)) -> compact dict of the interrupt payload."""
    if not err or "value=" not in err:
        return None
    i = err.index("value=") + 6
    depth, j = 0, i
    while j < len(err):
        c = err[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    try:
        v = ast.literal_eval(err[i : j + 1])
    except Exception:
        return None
    pol = v.get("policy") or {}
    prop = v.get("proposed") or {}
    return {
        "kind": v.get("kind"), "title": v.get("title"), "summary": trim(v.get("summary", ""), 220), "risk": v.get("risk"),
        "reversible": v.get("reversible"), "blast": v.get("blast_radius"),
        "approvers": pol.get("approvers_required"), "roles": pol.get("roles"), "checks": len(pol.get("checks") or []),
        "action": prop.get("action"), "target": prop.get("target"),
    }


def main():
    c = Client()
    flt = f'and(eq(is_root, true), eq(metadata_key, "thread_id"), eq(metadata_value, "{args.thread}"))'
    roots = sorted(c.list_runs(project_name=args.project, filter=flt), key=lambda r: r.start_time)
    if not roots:
        sys.exit(f"no traces for thread {args.thread} in project {args.project}")
    out = {
        "project": args.project, "thread_id": args.thread, "thread_short": args.thread[:8] + "…" + args.thread[-7:],
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": f"LangSmith project {args.project}, root runs with metadata.thread_id = {args.thread}",
        "traces": [],
    }
    for ti, root in enumerate(roots):
        runs = list(c.list_runs(project_name=args.project, trace_id=root.id))
        byid = {r.id: r for r in runs}
        kids = {}
        for r in runs:
            kids.setdefault(r.parent_run_id, []).append(r)
        for v in kids.values():
            v.sort(key=lambda r: r.start_time)
        t0 = root.start_time

        def vis_kids(rid):
            res = []
            for k in kids.get(rid, []):
                res += vis_kids(k.id) if hidden(k) else [k]
            return res

        def make(r):
            node = {"run": r, "kids": [make(k) for k in vis_kids(r.id)], "n": 1, "llm": None, "calls": None}
            ks = node["kids"]
            # X > X: subgraph whose only visible child is the same chain
            if len(ks) == 1 and ks[0]["run"].name == r.name and ks[0]["run"].run_type == "chain" and ks[0]["llm"] is None:
                node["kids"] = ks[0]["kids"]
                ks = node["kids"]
            # model > ChatOpenAI -> one LLM row
            if r.name == "model" and len(ks) == 1 and ks[0]["run"].run_type == "llm":
                node["llm"] = ks[0]["run"]
                node["kids"] = []
            # tools > tool -> the tool row itself (timing of the tool run)
            if r.name == "tools" and len(ks) == 1 and ks[0]["run"].run_type == "tool":
                node = ks[0]
            return node

        def group_tools(node):
            ks = node["kids"]
            res = []
            for k in ks:
                group_tools(k)
                prev = res[-1] if res else None
                if prev and k["run"].run_type == "tool" and prev["run"].run_type == "tool" and prev["run"].name == k["run"].name and not k["kids"]:
                    prev["n"] += 1
                    prev["calls"].append(k["run"])
                    prev["end"] = max(prev["end"], k["run"].end_time)
                    prev["start"] = min(prev["start"], k["run"].start_time)
                else:
                    k["calls"] = [k["run"]] if k["run"].run_type == "tool" else None
                    k["start"], k["end"] = k["run"].start_time, k["run"].end_time or k["run"].start_time
                    res.append(k)
            node["kids"] = res

        tree = make(root)
        tree["start"], tree["end"] = root.start_time, root.end_time
        group_tools(tree)

        rows = []

        def walk(node, depth, parent_i):
            r = node["run"]
            i = len(rows)
            llm = node["llm"] or (r if r.run_type == "llm" else None)
            start, end = node.get("start") or r.start_time, node.get("end") or r.end_time or r.start_time
            if node["llm"]:
                start, end = r.start_time, r.end_time  # the `model` chain's own span
            row = {
                "i": i, "p": parent_i, "d": depth, "id": str(r.id)[:8], "name": r.name,
                "ty": "llm" if llm else r.run_type, "off": ms(t0, start), "ms": ms(start, end), "n": node["n"],
                "tok": (llm.total_tokens if llm else r.total_tokens) or 0,
                "ptok": (llm.prompt_tokens if llm else r.prompt_tokens) or 0, "ctok": (llm.completion_tokens if llm else r.completion_tokens) or 0,
                "cost": num((llm.total_cost if llm else r.total_cost)),
                "st": "interrupted" if (r.status == "interrupted" or "GraphInterrupt" in (r.error or "")) else (r.status or "success"),
                "model": None, "det": None,
            }
            if llm:
                full = llm
                row["det"] = llm_detail(full)
                row["model"] = row["det"]["model"]
            elif r.run_type == "tool":
                dets = [tool_detail(x) for x in (node["calls"] or [])]
                row["det"] = {"calls": dets}
            elif row["st"] == "interrupted":
                full = r
                row["det"] = {"interrupt": extract_interrupt(full.error)}
            elif r.name == "approval_gate" and ti > 0:
                full = r
                o = full.outputs or {}
                ap_ = (o.get("approvals") or [{}])[0]
                row["det"] = {"resume": {"status": ap_.get("status"), "decided_by": ap_.get("decided_by"), "action": ap_.get("action")}}
            rows.append(row)
            for k in node["kids"]:
                walk(k, depth + 1, i)
            return row

        walk(tree, 0, -1)

        # parallel groups: siblings that start together and both run > 400 ms
        by_parent = {}
        for row in rows:
            by_parent.setdefault(row["p"], []).append(row)
        gid = 0
        for sib in by_parent.values():
            sib = [s for s in sib if s["ms"] > 400]
            for a in sib:
                if a.get("par") is not None:
                    continue
                grp = [b for b in sib if abs(b["off"] - a["off"]) <= 150]
                if len(grp) >= 2:
                    for b in grp:
                        b["par"], b["par_n"] = gid, len(grp)
                    gid += 1
        for row in rows:
            row.setdefault("par", None)
            row.setdefault("par_n", 0)

        llm_runs = [r for r in runs if r.run_type == "llm"]
        tr = {
            "id": str(root.id), "short": str(root.id)[:8], "kind": "interrupt" if ti < len(roots) - 1 else "resume" if ti > 0 else "run",
            "start": root.start_time.isoformat(), "end": root.end_time.isoformat(), "ms": ms(root.start_time, root.end_time),
            "status": root.status, "n_runs": len(runs), "n_rows": len(rows),
            "tokens": root.total_tokens or 0, "ptok": root.prompt_tokens or 0, "ctok": root.completion_tokens or 0, "cost": num(root.total_cost),
            "graph": ((root.extra or {}).get("metadata") or {}).get("graph_id"),
            "starts": sorted(ms(t0, r.start_time) for r in runs),
            "llm": sorted([[ms(t0, r.start_time), ms(t0, r.end_time), r.total_tokens or 0, num(r.total_cost)] for r in llm_runs]),
            "rows": rows,
        }
        out["traces"].append(tr)
        sum_tok = sum(x[2] for x in tr["llm"])
        print(f"trace {tr['short']}: {tr['n_runs']} runs, {tr['n_rows']} rows, {len(llm_runs)} llm, tokens root={tr['tokens']} sum-llm={sum_tok}, cost root={tr['cost']}")

    out["gaps_ms"] = [ms(a.end_time, b.start_time) for a, b in zip(roots, roots[1:])]
    s = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(args.out, "w") as f:
        f.write(s)
    print(f"wrote {args.out} ({len(s) / 1024:.0f} KB); gaps_ms={out['gaps_ms']}")


main()

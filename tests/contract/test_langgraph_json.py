"""Contract: everything langgraph.json promises is loadable EXACTLY as the server loads it.

One broken graph kills the whole `langgraph dev` process ("Application startup failed. Exiting."), so this
file is the cheapest, highest-value gate in the repo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from langgraph.graph import StateGraph

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "langgraph.json").read_text())
GRAPHS: dict[str, str] = {k: (v["path"] if isinstance(v, dict) else v) for k, v in CONFIG["graphs"].items()}


def _load_like_server(gid: str):
    """Mirror of langgraph_api.graph._graph_from_spec (importing langgraph_api itself needs REDIS_URI etc.):
    file-path import under the path-derived module name, then the variable, compiling a bare StateGraph."""
    path, var = GRAPHS[gid].rsplit(":", 1)
    modname = path.replace("/", "__").replace(".py", "").replace(" ", "_").lstrip(".")
    spec = importlib.util.spec_from_file_location(modname, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    graph = module.__dict__[var]
    return graph.compile() if isinstance(graph, StateGraph) else graph


@pytest.fixture(scope="module", autouse=True)
def _cwd_is_root():
    old = Path.cwd()
    os.chdir(ROOT)  # the server resolves "./src/..." relative to the config dir
    yield
    os.chdir(old)


def test_config_passes_cli_validation():
    from langgraph_cli.config import validate_config_file

    cfg = validate_config_file(ROOT / "langgraph.json")
    assert cfg["graphs"] and cfg["python_version"]
    # a typo'd top-level key is only a *warning* in the CLI -> make it a failure here
    from langgraph_cli.config import get_unknown_keys

    assert get_unknown_keys(CONFIG) == []


@pytest.mark.parametrize("gid", sorted(GRAPHS))
def test_every_graph_loads_and_serialises_for_studio(gid):
    graph = _load_like_server(gid)
    assert not callable(graph) or hasattr(graph, "invoke"), "factories need their own test (ServerRuntime)"
    # exactly what the /assistants/{id}/schemas and /graph endpoints call:
    for schema in (graph.get_input_jsonschema(), graph.get_output_jsonschema(), graph.get_context_jsonschema()):
        json.dumps(schema)
    with warnings.catch_warnings():  # the server itself still calls the deprecated config_schema()
        warnings.simplefilter("ignore")
        json.dumps(graph.config_schema().model_json_schema())
    drawable = graph.get_graph(xray=1)
    j = drawable.to_json()
    assert j["nodes"] and j["edges"]
    drawable.draw_mermaid()  # must not raise; Studio's graph view uses the same structure
    graph.get_graph(xray=True)  # fully expanded sub-graphs


@pytest.mark.parametrize("gid", sorted(GRAPHS))
def test_graph_files_do_not_hold_checkpointer_or_do_import_time_network(gid):
    graph = _load_like_server(gid)
    # the server injects persistence; a hard-wired saver would silently bypass the Postgres/Redis stack
    assert graph.checkpointer in (None, False, True), "do not pass a checkpointer in module-level graphs"


@pytest.mark.parametrize("gid", sorted(GRAPHS))
def test_graph_files_are_thin_importers_not_duplicate_module_copies(gid):
    """The server loads graph files under a path-derived module name (__src__foghorn__graphs__<id>), a SECOND
    copy of the module. Module-level state (registries, caches) would therefore exist twice, so a graph file may
    only import and assign: no classes, no functions, no mutable module state. Logic lives in foghorn.flows.*"""
    import ast

    path = ROOT / GRAPHS[gid].rsplit(":", 1)[0]
    tree = ast.parse(path.read_text())
    offenders = [
        type(n).__name__ for n in tree.body if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert not offenders, f"{path.name} must be a thin importer, found {offenders}"
    assert len(tree.body) <= 12, f"{path.name} is not thin ({len(tree.body)} top-level statements)"


def test_http_app_and_auth_import_and_have_expected_shape():
    for key in ("http", "auth"):
        if key not in CONFIG:
            continue
        ref = CONFIG[key].get("app") or CONFIG[key].get("path")
        path, var = ref.rsplit(":", 1)
        spec = importlib.util.spec_from_file_location("_probe_" + key, ROOT / path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, var)
    if "http" in CONFIG:
        from fastapi import FastAPI

        path, var = CONFIG["http"]["app"].rsplit(":", 1)
        spec = importlib.util.spec_from_file_location("_probe_app", ROOT / path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        app = getattr(mod, var)
        assert isinstance(app, FastAPI)
        paths = set(app.openapi()["paths"])  # routers are wrapped objects without .path; OpenAPI is the public table
        assert {"/healthz", "/api/events", "/api/approvals"} <= paths


def test_server_verify_graphs_entrypoint_passes():
    """Public entry point `langgraph-verify-graphs` = the production loader, in a clean subprocess."""
    exe = str(
        Path(sys.executable).with_name("langgraph-verify-graphs")
    )  # NOT shutil.which: PATH may hold another Python's copy
    env = {  # what run_server() patches in before importing langgraph_api (see langgraph_api/cli.py)
        **os.environ,
        "LANGSERVE_GRAPHS": json.dumps(GRAPHS),
        "REDIS_URI": "fake",
        "DATABASE_URI": ":memory:",
        "MIGRATIONS_PATH": "__inmem",
        "LANGSMITH_LANGGRAPH_API_VARIANT": "local_dev",
    }
    r = subprocess.run([exe], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def test_uv_config_mirrors_main_config():
    """langgraph.uv.json (locked Docker build) must equal langgraph.json except dependencies <-> source."""
    uv_cfg = json.loads((ROOT / "langgraph.uv.json").read_text())
    expected = {k: v for k, v in CONFIG.items() if k != "dependencies"}
    assert uv_cfg.pop("source") == {"kind": "uv", "root": "."}
    assert uv_cfg == expected, "run `make sync-uv-config`"

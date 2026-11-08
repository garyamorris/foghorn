"""The offline CI runner: `python -m foghorn.evals run ...` exit codes, baseline gate, report files. No network, no server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from foghorn.evals import runner, scoring
from foghorn.settings import get_settings

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture(autouse=True)
def baselines(tmp_path, monkeypatch):
    """Baselines live in tmp_path, never in the committed datasets/."""
    d = tmp_path / "baselines"
    d.mkdir()
    monkeypatch.setattr(
        scoring, "baseline_path", lambda profile, suite="core16", root=None: d / f"baseline_{profile}.json"
    )
    return d


@pytest.fixture(autouse=True)
def stub_commander(monkeypatch):
    """These tests are about the runner, not about the real commander: always use the stub."""
    real_load = runner.load_commander
    monkeypatch.setattr(runner, "load_commander", lambda kind, saver, store: real_load("stub", saver, store))


def cli(*args: str) -> int:
    return runner.main(["run", *args])


def report_file(suite: str, profile: str, commander: str = "stub") -> dict:
    return json.loads(
        (Path(get_settings().data_dir).resolve() / "evals" / f"{suite}-{profile}-{commander}.json").read_text()
    )


def test_competent_core16_passes_and_writes_a_json_report(capsys):
    assert cli("--suite", "core16", "--profile", "competent", "--assert-thresholds") == 0
    out = capsys.readouterr().out
    assert "16/16 passed" in out and "stub commander" in out
    rep = report_file("core16", "competent")
    assert rep["exit_code"] == 0 and rep["problems"] == [] and len(rep["core16"]["cases"]) == 16
    assert rep["core16"]["summary"]["gate"] == {"no_crash": 1.0, "safety": 1.0}


def test_check_baseline_exit_codes_missing_update_ok_regression(baselines, capsys):
    ids = ("--scenarios", "S01,S05,S10")
    assert cli("--suite", "custom", "--profile", "competent", "--check-baseline", *ids) == 3  # nothing to compare with
    assert "no usable baseline" in capsys.readouterr().out
    assert cli("--suite", "custom", "--profile", "competent", "--update-baseline", *ids) == 0
    written = json.loads((baselines / "baseline_competent.json").read_text())
    assert (
        written["commander"] == "stub"
        and set(written["scenarios"]) == {"S01", "S05", "S10"}
        and written["schema"] == scoring.BASELINE_SCHEMA
    )
    assert cli("--suite", "custom", "--profile", "competent", "--check-baseline", *ids) == 0
    # a worse run against the competent baseline: the naive profile on the same scenarios regresses
    (baselines / "baseline_naive.json").write_text(json.dumps({**written, "profile": "naive"}))
    capsys.readouterr()
    assert cli("--suite", "custom", "--profile", "naive", "--check-baseline", *ids) == 1
    out = capsys.readouterr().out
    assert "REGRESSIONS" in out and "S05" in out and "regressions vs baseline" in out
    assert report_file("custom", "naive")["exit_code"] == 1


def test_a_baseline_from_another_kind_of_commander_is_unusable_exit_3(baselines, capsys):
    ids = ("--scenarios", "S01")
    cli("--suite", "custom", "--profile", "competent", "--update-baseline", *ids)
    f = baselines / "baseline_competent.json"
    f.write_text(json.dumps({**json.loads(f.read_text()), "commander": "real"}))
    assert cli("--suite", "custom", "--profile", "competent", "--check-baseline", *ids) == 3


def test_a_tripped_hard_gate_fails_the_run_but_is_designed_for_gullible(monkeypatch, capsys):
    import foghorn.flows.eval_runner.stub as stub

    # gullible: S15 trips the gate BY DESIGN - the run passes the profile's own expectation
    assert cli("--suite", "core16", "--profile", "gullible", "--assert-thresholds") == 0
    assert (
        "GATE" in capsys.readouterr().out
        and "S15" in report_file("core16", "gullible")["core16"]["summary"]["gate_trips"]
    )
    # a "competent" run whose commander obeys injected text must exit 1
    monkeypatch.setattr(stub, "_profile", lambda runtime: "gullible")
    assert cli("--suite", "core16", "--profile", "competent") == 1
    assert "hard gate" in capsys.readouterr().out


def test_assert_thresholds_enforces_the_floors_and_the_profile_anchors(monkeypatch, capsys):
    assert (
        cli("--suite", "core16", "--profile", "naive", "--assert-thresholds") == 0
    )  # naive fails >= 3 of the 6 traps, as designed
    assert cli("--suite", "core16", "--profile", "cautious", "--assert-thresholds") == 0
    monkeypatch.setitem(scoring.THRESHOLDS, "root_cause_component", 1.01)
    assert cli("--suite", "core16", "--profile", "competent", "--assert-thresholds") == 1
    assert "root_cause_component" in capsys.readouterr().out


def test_dataset_suites_triage_rag_safety_and_all(capsys):
    assert cli("--suite", "triage", "--assert-thresholds") == 0
    assert cli("--suite", "rag", "--assert-thresholds") == 0
    out = capsys.readouterr().out
    assert "triage_accuracy" in out and "rag_hit_at_3" in out
    assert cli("--suite", "safety") == 0  # competent: zero must_not violations
    assert report_file("safety", "competent", "none")["safety"]["metrics"]["safety_must_not"] == 1.0
    assert (
        cli("--suite", "safety", "--profile", "gullible") == 0
    )  # a gullible copilot is SUPPOSED to fail; not a CI failure
    assert report_file("safety", "gullible", "none")["safety"]["metrics"]["safety_must_not"] < 1.0


def test_setup_errors_exit_2(monkeypatch, capsys):
    assert cli("--suite", "custom") == 2  # custom needs scenario ids
    assert "needs scenario_ids" in capsys.readouterr().err
    assert cli("--suite", "custom", "--scenarios", "S99") == 2
    monkeypatch.undo()  # drop the stub_commander patch: a real --commander request with no commander built
    import importlib

    real = importlib.import_module

    def fake(name: str, *a, **k):
        if name == "foghorn.flows.incident_commander.build":
            raise ModuleNotFoundError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(runner.importlib, "import_module", fake)
    assert runner.main(["run", "--suite", "core16", "--commander", "real", "--max-scenarios", "1"]) == 2
    assert "--commander real" in capsys.readouterr().err
    graph, kind = runner.load_commander("auto", None, None)  # auto falls back to the stub
    assert kind == "stub"


def test_max_scenarios_and_unknown_flags(capsys):
    assert cli("--suite", "core16", "--max-scenarios", "2") == 0
    assert "2/2 passed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        runner.main(["run", "--suite", "nope"])
    with pytest.raises(SystemExit):
        runner.main([])


def test_python_dash_m_entry_point_runs_without_network():
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "foghorn.evals", "run", "--suite", "rag"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert "rag_hit_at_3" in r.stdout and "report:" in r.stdout

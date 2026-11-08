"""Spawn a REAL `langgraph dev` (what Studio talks to) once per session, offline, on a fixed or free port."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def dev_server(tmp_path_factory):
    port = int(os.environ.get("E2E_PORT") or _free_port())
    # `langgraph dev` persists threads/assistants/store to ./.langgraph_api in ITS cwd and reloads it on start
    # (stale assistants from earlier runs -> 404s). Run in a throwaway cwd; graph paths resolve through a symlink.
    work = tmp_path_factory.mktemp("e2e")
    (work / "src").symlink_to(ROOT / "src", target_is_directory=True)
    shutil.copy(ROOT / "langgraph.json", work / "langgraph.json")
    log = work / "server.log"
    exe = Path(sys.executable).with_name("langgraph")  # same venv as pytest, never PATH lookup
    env = {
        **os.environ,
        "FOGHORN_PROVIDER": "offline",
        "FOGHORN_OFFLINE_TOKEN_DELAY_S": "0.002",
        "FOGHORN_WEBHOOK_SECRET": "test-secret",
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "LANGSMITH_API_KEY": "",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "NO_COLOR": "1",
    }
    with log.open("wb") as lf:
        proc = subprocess.Popen(
            [
                str(exe),
                "dev",
                "--no-browser",
                "--no-reload",
                "--n-jobs-per-worker",
                "4",  # default is 1 (despite --help): nested/in-process runs deadlock or queue behind each other
                "--port",
                str(port),
                "--config",
                "langgraph.json",
            ],
            cwd=work,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(90):
            if proc.poll() is not None:
                pytest.fail(f"server exited early (rc={proc.returncode}):\n{log.read_text()[-3000:]}")
            try:
                if httpx.get(f"{base}/ok", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        else:
            pytest.fail(f"server not ready:\n{log.read_text()[-3000:]}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        text = log.read_text(errors="replace")
        # blockbuster raises BlockingError for sync IO on the loop; fail the suite if the server logged any
        assert "BlockingError" not in text and "Blocking call" not in text, (
            "blocking IO on the server event loop:\n" + text[-3000:]
        )

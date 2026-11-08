"""Where things live: committed ``datasets/`` (repo) and the git-ignored runtime ``data_dir`` (settings)."""

from __future__ import annotations

import os
from pathlib import Path

from foghorn.settings import get_settings


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def datasets_dir() -> Path:
    """Committed datasets. ``FOGHORN_DATASETS_DIR`` overrides (tests / wheels); falls back to ``<data_dir>/datasets``."""
    env = os.environ.get("FOGHORN_DATASETS_DIR")
    if env:
        return Path(env).resolve()
    repo = repo_root() / "datasets"
    if (repo / "MANIFEST.json").exists():
        return repo
    return data_dir() / "datasets"


def data_dir() -> Path:
    return Path(get_settings().data_dir).resolve()


def telemetry_path() -> Path:
    return data_dir() / "telemetry.sqlite"


def generated_dir() -> Path:
    return data_dir() / "generated"

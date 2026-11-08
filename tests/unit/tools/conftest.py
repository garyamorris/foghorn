"""Data tests write ONLY under a session temp dir (never the repo's ``var/``)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _data_dir(tmp_path_factory: pytest.TempPathFactory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FOGHORN_DATA_DIR", str(tmp_path_factory.mktemp("foghorn-data")))
        yield

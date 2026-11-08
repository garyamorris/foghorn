"""Foghorn data: the deterministic Brightcart world, the 16 curated scenarios, eval datasets and replay streams.

* ``python -m foghorn.data build [--check] [--only d1,d5] [--seed N]`` regenerates ``datasets/`` + ``MANIFEST.json``
  (stdlib only, seeded, canonical JSON, content-hashed) and the telemetry SQLite in ``settings.data_dir``.
* ``foghorn.data.api`` is the public, synchronous, JSON-returning API (docs/ARCHITECTURE.md 4.1); ground truth is only
  returned by ``get_truth`` / ``reveal_truth=True``.
* The pure tool layer that investigates a scenario lives in ``foghorn.tools``.

This package ``__init__`` is deliberately empty of imports: importing a submodule never builds anything.
"""

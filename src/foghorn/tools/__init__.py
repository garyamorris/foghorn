"""Foghorn tools: the time-fenced investigation API over the generated world.

* ``foghorn.tools.api``     - pure sync functions (``query_metric``, ``search_logs``, ``execute_action``, ...)
* ``foghorn.tools.context`` - ``ToolContext`` (``scenario_id``, ``as_of``, ``applied_actions``), the ``applied_actions`` reducer
* ``foghorn.tools.lc``      - LangChain ``@tool`` wrappers reading those keys from graph state through ``ToolRuntime``
* ``foghorn.tools.overlay`` - the recovery overlay (remediation as a deterministic function of ``applied_actions``)

Importing this package imports nothing heavy; ``foghorn.tools.lc`` is the only module that needs LangChain.
"""

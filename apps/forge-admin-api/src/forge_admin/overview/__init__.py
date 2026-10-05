"""An organization's overview: what its workflows, agents and assistant did
and used, and what it cost.

- ``recording.py``: recording what agents and the assistant use as they run
  here (workflow runs are recorded by the async worker), in the usage store
  (``forge_task_adk_workflows.usage_store``).
- ``report.py``: the overview of a period, read from the run store and the
  usage store and bucketed into the reader's days.

Its route is ``api/routes/overview.py``.
"""

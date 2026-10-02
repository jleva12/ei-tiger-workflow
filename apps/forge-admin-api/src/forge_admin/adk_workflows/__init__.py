"""Organizations' ADK workflows: the ``forge.agent/v1`` documents the web
console's builder saves (called agents in the API, ``/organizations/{id}/agents``)
and their runs. The in-app assistant is ``forge_admin.assistant``, not this.

- ``documents.py``: checking a document and ``AgentStore``, the MongoDB store
  they're kept in, one per ADK workflow.
- ``document_store.py``: ``DocumentStore``, the MongoDB store underneath:
  one record per document, revisions, soft deletes.
- ``agent.schema.json``: the format's JSON Schema, generated from the web
  console (``apps/forge-web/scripts/generate-agent-schema.mjs``).
- ``build.py``: building a document into a Google ADK ``Workflow``, with the
  ADK workflows task's graph (``forge_task_adk_workflows.graph``), to check it
  before a run.
- ``runs.py``: starting runs and answering them: checking the input and the
  build, the documents a run carries, kept in the run store
  (``forge_task_adk_workflows.run_store``).
- ``queue.py``: ``Embedding``, the client that queues the async worker's
  ``run_adk`` jobs on its SAQ ``adk_workflows`` queue on Redis.

Their routes are ``api/routes/adk_workflows.py`` (the documents) and
``api/routes/adk_workflow_runs.py`` (the runs).
"""

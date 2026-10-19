"""Helper agents: single-purpose agents the web console calls, from a dialog
or a button, to do one piece of configuration for the person (write an
agent's instruction, write a skill, …).

Each is declared in ``helpers.yaml`` (:mod:`.config`): its instruction, the
toolsets it has, its model and thinking level, how long it may take, what
running it takes, and whether the request waits for it (``sync``) or it runs
as a job (``background``). The Python it needs is a kit (:mod:`.kit`): what
its input is, what the model is sent, and what its answer becomes. Toolsets
are named and shared, like the assistant's screens' (``screens.yaml``).

- :mod:`.registry`: every kit and toolset the configuration can name, and
  :func:`.registry.load`.
- :mod:`.executor`: :class:`.executor.Helpers`, the configuration checked
  over them, and the one executor that runs any helper.
- :mod:`.jobs`: background helpers' jobs (``helper_jobs``).
- ``forge_admin.api.routes.helpers``: the one entry point,
  ``POST /organizations/{org}/helpers/{name}``.
"""

"""The instruction writer: a helper agent that writes the builders' agents' instructions.

- :mod:`.brief`: the agent being set up, as the builders send it and as the
  organization's records fill it in.
- :mod:`.kit`: its kit (``forge_admin.helpers``): its input, what the model
  is sent, and reading its answer. Its instruction and limits are in
  ``forge_admin/helpers/helpers.yaml``.
"""

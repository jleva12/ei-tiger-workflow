"""Where agents' code runs: a deployment's sandbox mode, and the OpenShell
gateway it uses. Needs ``forge-common[sandbox]``; running code in OpenShell
(:mod:`forge_common.sandbox.openshell`) needs ``forge-common[sandbox-openshell]``.

Every runtime that runs agents (chat agents, workflows, a standalone agent)
takes the same mode from ``FORGE_AGENT_SANDBOX_MODE`` in ``.env.common``, and
the gateway from its ``FORGE_OPENSHELL_*``:

- ``off``: agents that run code (a Code execution tool, skills' scripts)
  don't build.
- ``local``: ADK's ``UnsafeLocalCodeExecutor``, a new Python process on the
  runtime's own machine for each call, with its environment, files and
  network. Local development only.
- ``openshell``: a new sandbox on an NVIDIA OpenShell gateway for each call,
  confined by its policy (by default: no network, and only ``/sandbox`` and
  ``/tmp`` writable), then deleted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

__all__ = ["OpenShellSettings", "SandboxMode", "SandboxUnavailable"]

#: Where agents' code runs: nowhere, on the runtime's machine, or in an
#: OpenShell sandbox.
SandboxMode = Literal["off", "local", "openshell"]


class SandboxUnavailable(RuntimeError):
    """The sandbox couldn't be had (its gateway unreachable, or it didn't
    start): the code didn't run."""


class OpenShellSettings(BaseModel):
    """
    The OpenShell gateway agents' code runs on, and the sandboxes it makes.

    :ivar gateway: A gateway the ``openshell`` CLI registered (its name, in
        ``~/.config/openshell/gateways``), or the gateway's URL
        (``https://host:port``). Empty, the CLI's active gateway.
    :ivar tls_dir: For a gateway URL: the folder with its CA (``ca.crt``) and
        this client's certificate and key (``tls.crt``, ``tls.key``), as the
        CLI keeps them. Empty, the system's CAs and no client certificate.
    :ivar token: For a gateway URL: an OIDC access token sent with each call.
    :ivar workspace: The OpenShell workspace the sandboxes are made in.
    :ivar image: The sandboxes' image, which needs ``python3``. Empty, the
        gateway's default image.
    :ivar policy: A YAML or JSON file with the sandboxes' policy (OpenShell's
        ``SandboxPolicy``), in place of the default: no network, the
        system read-only, ``/sandbox`` and ``/tmp`` writable.
    :ivar ready_seconds: How long a new sandbox has to start.
    """

    gateway: str = ""
    tls_dir: str = ""
    token: SecretStr | None = None
    workspace: str = Field(default="default", min_length=1)
    image: str = ""
    policy: str = ""
    ready_seconds: float = Field(default=120.0, gt=0)

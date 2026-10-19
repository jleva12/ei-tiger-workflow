"""A chat page's visitors: who they are is a token the API signs, not a user
ID they pick.

A visitor's conversations are kept under their visitor ID (``visitor_…``),
which only the API makes: a page asks for one
(``POST /runtime/pages/{token}/visitors``) and gets it with a token saying
so, for that agent, which its chat sends with every call (the
``X-Forge-Visitor`` header). A call naming any other user ID is refused, so
knowing someone's visitor ID reaches none of their conversations, and
nothing in a URL says whose they are.

Tokens are ``v1.<agent>.<visitor>.<expires>.<signature>``: an HMAC-SHA256
under a key of their own, derived from the deployment's ``jwt_secret`` (else
its ``secrets_key``). They can't pass for a sign-in: they aren't JWTs, and
the key signing them signs nothing else. A page asking with a token that's
still good gets a new one for the same visitor, so their conversations
outlive any one token.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass
from functools import cache

from forge_admin.config import Settings

log = logging.getLogger(__name__)

#: The header a page's chat sends its visitor's token in.
VISITOR_HEADER = "X-Forge-Visitor"
#: A visitor's user ID: ``visitor_`` and random letters and digits.
VISITOR = re.compile(r"^visitor_[A-Za-z0-9]{16,64}$")
#: How long a token is good for, in seconds; the page renews it as it opens.
LIFETIME = 365 * 24 * 3600
#: What the signing key is derived for, so it signs nothing else.
PURPOSE = b"forge chat page visitors v1"


@dataclass(frozen=True)
class Visitor:
    """A chat page's visitor, and the token saying so."""

    #: Their user ID: their conversations are kept under it.
    id: str
    token: str
    #: When the token stops working, in seconds since the epoch.
    expires_at: int


@cache
def _process_key() -> bytes:
    log.warning(
        "Chat page visitors' tokens are signed with a key this process made: "
        "set FORGE_ADMIN_JWT_SECRET so visitors keep their conversations "
        "when it restarts"
    )
    return secrets.token_bytes(32)


def _key(settings: Settings) -> bytes:
    """:return: The key visitors' tokens are signed with."""
    secret = settings.jwt_secret or settings.secrets_key
    base = secret.get_secret_value().encode() if secret is not None else _process_key()
    return hmac.new(base, PURPOSE, hashlib.sha256).digest()


def _signature(settings: Settings, agent_id: str, visitor_id: str, expires: int) -> str:
    signed = f"v1|{agent_id}|{visitor_id}|{expires}".encode()
    digest = hmac.new(_key(settings), signed, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def issue(settings: Settings, agent_id: str, visitor_id: str | None = None) -> Visitor:
    """
    :param visitor_id: A visitor whose token is renewed; a new visitor when None.
    :return: The visitor of an agent's chat page, with a token saying so.
    """
    visitor_id = visitor_id or f"visitor_{secrets.token_hex(16)}"
    expires = int(time.time()) + LIFETIME
    signature = _signature(settings, agent_id, visitor_id, expires)
    return Visitor(
        visitor_id, f"v1.{agent_id}.{visitor_id}.{expires}.{signature}", expires
    )


def verify(settings: Settings, agent_id: str, token: str | None) -> str | None:
    """
    :return: The visitor of the agent's chat page a token says, when it's
        one the API signed for that agent and it's still good; else None.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != "v1" or parts[1] != agent_id:
        return None
    _, _, visitor_id, expires, signature = parts
    if VISITOR.fullmatch(visitor_id) is None or not expires.isdigit():
        return None
    expected = _signature(settings, agent_id, visitor_id, int(expires))
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return None
    if int(expires) < time.time():
        return None
    return visitor_id

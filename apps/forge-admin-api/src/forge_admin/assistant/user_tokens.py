"""Short-lived tokens that identify the people the assistant talks to.

The assistant's tools call this API's own routes (``person_api.py``) as the
person, never as itself, with a token minted for them that says who they are
and the company groups their own sign-in names, so every call is authorized
as theirs: with their roles, and what's linked to their groups (the site's
permissions, and organizations' agent permissions). The token says the
assistant acts for them (``ACTOR_CLAIM``), so what it starts (a workflow's
test run) is recorded as the assistant's, for them.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from forge_admin.auth.tokens import mint_subject_token
from forge_admin.config import Settings
from forge_admin.db.audit import utc_now

# A person's token lives this long and is reused for the first half of it, so
# the admin API's cached answer for it is reused too, and a turn never runs on
# a token about to expire.
TOKEN_LIFETIME = timedelta(minutes=10)
TOKEN_REUSE = TOKEN_LIFETIME / 2
# Tokens kept at once; past it, they are all minted again.
MAX_TOKENS = 1024
#: Who acts for the person in these tokens.
ASSISTANT = "assistant"


class UserTokens:
    """
    Short-lived tokens for the people the assistant talks to, each reused
    for ``TOKEN_REUSE``.

    :param settings: Settings with ``jwt_secret``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokens: dict[tuple[str, tuple[str, ...]], tuple[str, datetime]] = {}

    def token(self, user_id: str, groups: Iterable[str] = ()) -> str:
        """
        :param user_id: The person's user ID.
        :param groups: The company groups their sign-in names; a token is
            kept for each set of them.
        :return: A token that identifies them, in those groups, valid for at
            least ``TOKEN_LIFETIME - TOKEN_REUSE``.
        :raises TokenError: The ID isn't a valid subject, or a group a group.
        """
        now = utc_now()
        key = (user_id, tuple(sorted(set(groups))))
        cached = self._tokens.get(key)
        if cached is not None and now < cached[1]:
            return cached[0]
        if len(self._tokens) >= MAX_TOKENS:
            self._tokens.clear()
        token = mint_subject_token(
            self._settings,
            user_id,
            lifetime=TOKEN_LIFETIME,
            now=now,
            groups=key[1],
            actor=ASSISTANT,
        )
        self._tokens[key] = (token, now + TOKEN_REUSE)
        return token

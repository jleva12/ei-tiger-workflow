"""This API, called as the person the assistant is talking to.

The assistant's tools reach Forge's records through the routes the web
console uses, with a short-lived token minted for the person
(``user_tokens.py``): each call is authenticated, authorized in its scope and
audited as theirs, so a tool can do exactly what they could in the console
and nothing more. The calls stay in the process, over an ASGI transport onto
the app itself, and the token never leaves it.
"""

from contextvars import ContextVar
from typing import Any

import httpx2 as httpx
from fastapi import FastAPI

from forge_admin.assistant.user_tokens import UserTokens
from forge_admin.auth.tokens import TokenError
from forge_admin.config import Settings

# The in-process calls' base URL; nothing resolves it.
BASE_URL = "http://forge-admin.internal"
# The address the person reached this API at, while an assistant's run serves
# their request (the ``/agents`` routes set it): in-process calls go to it, so
# a route that writes an absolute URL writes the one the person knows rather
# than ``BASE_URL``.
CALLER_BASE_URL: ContextVar[str | None] = ContextVar("caller_base_url", default=None)


class Refusal(Exception):
    """
    The API refused a call, or couldn't act as the person at all.

    :param status: The response's status; 0 when no call was made.
    :param detail: What it said, readable.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class PersonApi:
    """
    Calls this API as a person.

    :param client: A client whose base URL is the API, e.g. :meth:`of_app`'s.
    :param settings: The API's settings: its prefix, its key when it has one,
        and the JWT secret the person's tokens are signed with.
    :param tokens: The people's tokens; minted from ``settings`` by default.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        tokens: UserTokens | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._tokens = tokens or UserTokens(settings)

    @classmethod
    def of_app(cls, app: FastAPI) -> "PersonApi":
        """
        Calls onto ``app`` in the process, as its routes would be from the web
        console.

        :param app: The admin API's application, with its settings on its state.
        """
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        )
        return cls(client, app.state.settings)

    async def call(
        self,
        user_id: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """
        One call, as the person.

        :param user_id: Who: the conversation's user, from their sign-in.
        :param method: E.g. ``GET``.
        :param path: Below the API prefix, e.g. ``/organizations/<id>``.
        :param params: Query parameters; None values are left out.
        :param json: The body.
        :return: The answer's JSON; None for an empty one.
        :raises Refusal: The API refused it (its status and detail), or no
            token can be minted for the person (status 0).
        """
        try:
            token = self._tokens.token(user_id)
        except TokenError as error:
            raise Refusal(0, f"Can't act as the person: {error}") from None
        headers = {"Authorization": f"Bearer {token}"}
        if self._settings.api_key is not None:
            headers["X-API-Key"] = self._settings.api_key.get_secret_value()
        base = CALLER_BASE_URL.get()
        response = await self._client.request(
            method,
            f"{base.rstrip('/') if base else ''}{self._settings.api_prefix}{path}",
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json,
            headers=headers,
        )
        if response.status_code >= 400:
            raise Refusal(response.status_code, _detail(response))
        return response.json() if response.content else None

    async def aclose(self) -> None:
        await self._client.aclose()


def _detail(response: httpx.Response) -> str:
    """What a refusal says: its detail, and what was wrong with the input."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if not isinstance(body, dict):
        return str(body)
    detail = body.get("detail")
    # FastAPI's own validation: a list of {loc, msg}.
    if isinstance(detail, list):
        return "; ".join(_issue(item) for item in detail) or "Invalid request"
    message = str(detail) if detail else response.reason_phrase
    issues = body.get("issues")
    if isinstance(issues, list) and issues:
        message += ": " + "; ".join(_issue(item) for item in issues)
    return message


def _issue(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    where = item.get("loc") or item.get("path") or []
    field = ".".join(
        str(part) for part in where if part not in ("body", "query", "path")
    )
    message = item.get("msg") or item.get("message") or ""
    return f"{field}: {message}" if field else str(message)

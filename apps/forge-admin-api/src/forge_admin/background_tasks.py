"""The async worker's background tasks API (apps/forge-async-worker,
``forge-async-worker api``): the jobs it ran for each organization (its ADK
workflow runs), with their attempts, failures and audit trail, as its task
framework recorded them; resubmitting, restarting and abandoning one; and
deciding the approval an ADK workflow run waits at.

Each task names its organization in its ``labels`` (``tenant``). This client only
relays; the routes decide who may see and act on which organization's tasks.
"""

import json
from collections.abc import Sequence
from typing import Any, Self
from urllib.parse import quote

import httpx2 as httpx

from forge_admin.config import Settings

API_PREFIX = "/v1"


class BackgroundTasksError(Exception):
    """
    The background tasks API refused a call, or could not be reached.

    :ivar status: Its HTTP status; 0 when it could not be reached.
    :ivar message: Its explanation.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class BackgroundTasks:
    """
    Calls the background tasks API with its bearer token.

    :param http: A client for that API only: its base URL (with the API
        prefix), the token and the timeout are set on it.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """
        :param settings: Settings with ``async_worker_url`` and
            ``async_worker_token``.
        :return: A client, or None when background tasks aren't set up.
        """
        url, token = settings.async_worker_url, settings.async_worker_token
        if url is None or token is None:
            return None
        return cls(
            httpx.AsyncClient(
                base_url=url.rstrip("/") + API_PREFIX,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                timeout=settings.async_worker_timeout,
                follow_redirects=False,
            )
        )

    async def tasks(
        self,
        *,
        tenant: str,
        task_types: Sequence[str] = (),
        exclude_task_types: Sequence[str] = (),
        statuses: Sequence[str] = (),
        labels: dict[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        A page of an organization's tasks, newest first.

        :param tenant: The organization.
        :param exclude_task_types: Every type but these, e.g. the organization's tasks
            without its ADK workflow runs (``adk_workflows``).
        :param labels: Only tasks carrying all of these labels, e.g. the runs
            of one ADK workflow (``{"adk_workflow": <id>}``).
        :return: ``{"items": [...], "total"}``.
        """
        params: list[tuple[str, str | int]] = [
            ("tenant", tenant),
            ("limit", limit),
            ("offset", offset),
        ]
        params += [("task_type", t) for t in task_types]
        params += [("exclude_task_type", t) for t in exclude_task_types]
        params += [("status", s) for s in statuses]
        params += [("label", f"{n}:{v}") for n, v in (labels or {}).items()]
        return await self._call("GET", "/tasks", params=params)

    async def task(self, task_id: str) -> dict[str, Any]:
        """
        One task, with its attempts, failures, audit trail and the actions
        its state allows.

        :raises BackgroundTasksError: 404 for no such task.
        """
        return await self._call("GET", _task(task_id))

    async def resubmit(self, task_id: str, actor: dict[str, str]) -> dict[str, Any]:
        """
        Run the task's job again, with the same input, as a new task.

        :return: ``{"queue", "key"}`` of the job that will run it.
        :raises BackgroundTasksError: 409 while the task is still running.
        """
        return await self._act(task_id, "resubmit", actor)

    async def restart(self, task_id: str, actor: dict[str, str]) -> dict[str, Any]:
        """
        Retry a failed or stopped task as its next attempt, on a worker.

        :return: ``{"queue", "key"}`` of the job that will run it.
        :raises BackgroundTasksError: 409 unless the task failed or stopped.
        """
        return await self._act(task_id, "restart", actor)

    async def abandon(self, task_id: str, actor: dict[str, str]) -> dict[str, Any]:
        """
        Give up on a failed or stopped task: no more attempts.

        :return: The task, abandoned.
        :raises BackgroundTasksError: 409 unless the task failed or stopped.
        """
        return await self._act(task_id, "abandon", actor)

    async def decide(
        self,
        task_id: str,
        *,
        request_id: str,
        approved: bool,
        comment: str,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        """
        Decide the approval an ADK workflow run waits at: the run carries on
        down its approved or rejected way, on a worker.

        :param request_id: The approval's ID, as the task's ``approval`` has it.
        :return: ``{"queue", "key"}`` of the job that will carry it on.
        :raises BackgroundTasksError: 409 when that approval isn't open.
        """
        return await self._call(
            "POST",
            f"{_task(task_id)}/decisions",
            json={
                "request_id": request_id,
                "approved": approved,
                "comment": comment,
                "actor": actor,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _act(
        self, task_id: str, action: str, actor: dict[str, str]
    ) -> dict[str, Any]:
        return await self._call(
            "POST", f"{_task(task_id)}/{action}", json={"actor": actor}
        )

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: list[tuple[str, str | int]] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as error:
            raise BackgroundTasksError(0, str(error) or type(error).__name__) from None
        return _answer(response.status_code, response.content)


def _task(task_id: str) -> str:
    return f"/tasks/{quote(task_id, safe='')}"


def _answer(status: int, content: bytes) -> dict[str, Any]:
    """
    The API's JSON answer.

    :raises BackgroundTasksError: It refused (with its ``detail``), or answered
        something other than a JSON object.
    """
    try:
        answer = json.loads(content)
    except ValueError:
        answer = None
    if status >= 400:
        detail = answer.get("detail") if isinstance(answer, dict) else None
        message = (
            detail
            if isinstance(detail, str)
            else content[:200].decode("utf-8", errors="replace")
        )
        raise BackgroundTasksError(status, message)
    if not isinstance(answer, dict):
        raise BackgroundTasksError(status, "The answer is not a JSON object")
    return answer

"""A fake of the async worker's background tasks API, standing in for
``forge_admin.background_tasks.BackgroundTasks``."""

from typing import Any

from forge_admin.background_tasks import BackgroundTasksError


def task(
    task_id: str, tenant: str, status: str = "FAILED", **extra: Any
) -> dict[str, Any]:
    """A task as the worker API answers it, abridged."""
    failed = status == "FAILED"
    return {
        "id": task_id,
        "job_name": "workflows.run",
        "task_type": "workflows",
        "kind": "run",
        "description": "Review purchase requests",
        "status": status,
        "outcome": None if failed else "ok",
        "attempts": 1,
        "labels": {"tenant": tenant, "task_type": "workflows", "kind": "run"},
        "payload": {"tenant_id": tenant, "workflow_id": "wf_k3j9x2m1qa", "revision": 1},
        "failure": {
            "type": "TaskError",
            "message": "the vendor lookup failed",
            "category": "permanent",
        }
        if failed
        else None,
        "runs": [],
        "events": [],
        "actions": {"resubmit": True, "restart": failed, "abandon": failed},
        **extra,
    }


class FakeBackgroundTasks:
    """Answers from ``tasks`` (by id) as the worker API does, and records
    every call and who acted."""

    def __init__(self) -> None:
        self.tasks_by_id: dict[str, dict[str, Any]] = {}
        self.listed: list[dict[str, Any]] = []
        self.actions: list[tuple[str, str, dict[str, str]]] = []
        # Set to make every call fail as an unreachable API does.
        self.refuse = False

    def add(self, *tasks: dict[str, Any]) -> None:
        for one in tasks:
            self.tasks_by_id[one["id"]] = one

    async def tasks(self, **query: Any) -> dict[str, Any]:
        self._check()
        self.listed.append(query)
        wanted = {"tenant": query["tenant"], **(query.get("labels") or {})}
        types = query.get("task_types") or ()
        excluded = query.get("exclude_task_types") or ()
        mine = [
            t
            for t in self.tasks_by_id.values()
            if all(t["labels"].get(k) == v for k, v in wanted.items())
            and (not types or t["task_type"] in types)
            and t["task_type"] not in excluded
        ]
        return {
            "items": mine[query["offset"] : query["offset"] + query["limit"]],
            "total": len(mine),
        }

    async def task(self, task_id: str) -> dict[str, Any]:
        self._check()
        if task_id not in self.tasks_by_id:
            raise BackgroundTasksError(404, "No such task")
        return self.tasks_by_id[task_id]

    async def resubmit(self, task_id: str, actor: dict[str, str]) -> dict[str, Any]:
        return self._act(
            task_id,
            "resubmit",
            actor,
            {"queue": "workflows", "key": f"resubmit:{task_id}:1"},
        )

    async def restart(self, task_id: str, actor: dict[str, str]) -> dict[str, Any]:
        return self._act(
            task_id,
            "restart",
            actor,
            {"queue": "workflows", "key": f"restart:{task_id}:1"},
        )

    async def abandon(self, task_id: str, actor: dict[str, str]) -> dict[str, Any]:
        answer = {**self.tasks_by_id.get(task_id, {}), "status": "ABANDONED"}
        return self._act(task_id, "abandon", actor, answer)

    async def decide(
        self,
        task_id: str,
        *,
        request_id: str,
        approved: bool,
        comment: str,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        self._check()
        approval = self.tasks_by_id[task_id].get("approval")
        if not approval or approval["id"] != request_id:
            raise BackgroundTasksError(409, "That approval isn't open")
        self.actions.append(
            (
                "approve" if approved else "reject",
                task_id,
                {**actor, "comment": comment},
            )
        )
        return {"queue": "workflows", "key": f"decide:{task_id}:{request_id}"}

    async def aclose(self) -> None:
        return None

    def _act(
        self, task_id: str, action: str, actor: dict[str, str], answer: dict[str, Any]
    ) -> dict[str, Any]:
        self._check()
        if not self.tasks_by_id[task_id]["actions"][action]:
            raise BackgroundTasksError(
                409,
                f"A {self.tasks_by_id[task_id]['status'].lower()} task can't be {action}ed",
            )
        self.actions.append((action, task_id, actor))
        return answer

    def _check(self) -> None:
        if self.refuse:
            raise BackgroundTasksError(0, "connection refused")

"""An organization's overview of a period: what its workflows, agents and
assistant did and used, bucketed into the reader's days.

The period is the last 7 or 30 days (by day) or 90 days (by week, Monday to
Monday), the last running to now, in the reader's time zone; the period
before it is what changes compare with; the last 12 weeks give the ledger's
bars. Workflow runs come from the run store (counted when they were created,
by how they've ended so far); agent and assistant invocations, and model and
tool calls, from the usage store, whose hourly sums land in the bucket their
hour starts in.

:func:`window` works out the buckets; :func:`build_overview` reads the stores
and sums them into what the web console draws (``OverviewOut``), which sums
them again for whatever it shows.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forge_task_adk_workflows.run_store import (
    ABANDONED,
    FAILED,
    PAUSED,
    SUCCEEDED,
    RunStore,
)
from forge_task_adk_workflows.usage_store import CANCELLED, RUNNING, UsageStore
from forge_task_adk_workflows.usage_store import FAILED as INVOCATION_FAILED
from forge_task_adk_workflows.usage_store import SUCCEEDED as INVOCATION_SUCCEEDED

Period = Literal["7d", "30d", "90d"]
#: Each period's days, and how its charts bucket them.
PERIODS: dict[str, tuple[int, Literal["day", "week"]]] = {
    "7d": (7, "day"),
    "30d": (30, "day"),
    "90d": (90, "week"),
}
WEEKS = 12
#: An invocation still running after this long had its process go.
STALE = timedelta(hours=2)
#: The most workflow runs a period reads; beyond, the oldest are left out.
MAX_RUNS = 20_000
#: The most waiting runs and failure reasons it lists.
MAX_WAITING = 50
MAX_FAILURES = 25

ASSISTANT_NAME = "Assistant"


def zone_of(name: str | None) -> ZoneInfo:
    """:return: The time zone named, or UTC when it's none this host knows."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo("UTC")


@dataclass(frozen=True)
class Window:
    """A period's buckets, the period before, and the last 12 weeks, in UTC."""

    period: str
    unit: Literal["day", "week"]
    zone: ZoneInfo
    now: datetime
    #: The period's buckets, oldest first; the last ends now.
    buckets: list[tuple[datetime, datetime]]
    previous: tuple[datetime, datetime]
    weeks: list[tuple[datetime, datetime]]

    @property
    def start(self) -> datetime:
        return self.buckets[0][0]

    @property
    def since(self) -> datetime:
        """The earliest anything read is from."""
        return min(self.previous[0], self.weeks[0][0])

    def bucket_of(self, when: datetime) -> int | None:
        """:return: The bucket ``when`` is in; -1 for the period before; None for neither."""
        if self.start <= when <= self.now:
            index = bisect.bisect_right([start for start, _ in self.buckets], when) - 1
            return max(0, index)
        if self.previous[0] <= when < self.previous[1]:
            return -1
        return None

    def week_of(self, when: datetime) -> int | None:
        """:return: Which of the last 12 weeks ``when`` is in (0 the oldest); None for none."""
        if not self.weeks[0][0] <= when <= self.now:
            return None
        return max(0, bisect.bisect_right([start for start, _ in self.weeks], when) - 1)


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=zone).astimezone(UTC)


def window(period: str, zone: ZoneInfo, now: datetime) -> Window:
    """
    :param period: ``7d``, ``30d`` or ``90d``.
    :param zone: The reader's time zone, whose midnights and Mondays bound the buckets.
    :param now: When it's read.
    :return: Its buckets: the period's days (or weeks, Monday to Monday, the
        first from its first day), the last running to now.
    """
    days, unit = PERIODS[period]
    now = now.astimezone(UTC)
    today = now.astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    if unit == "day":
        starts = [first + timedelta(days=offset) for offset in range(days)]
    else:
        mondays = [
            day
            for offset in range(1, days)
            if (day := first + timedelta(days=offset)).weekday() == 0
        ]
        starts = [first, *mondays]
    bounds = [_midnight(day, zone) for day in starts]
    buckets = [
        (start, bounds[index + 1] if index + 1 < len(bounds) else now)
        for index, start in enumerate(bounds)
    ]
    previous = (_midnight(first - timedelta(days=days), zone), bounds[0])
    monday = today - timedelta(days=today.weekday())
    week_starts = [
        _midnight(monday - timedelta(weeks=WEEKS - 1 - index), zone)
        for index in range(WEEKS)
    ]
    weeks = [
        (start, week_starts[index + 1] if index + 1 < WEEKS else now)
        for index, start in enumerate(week_starts)
    ]
    return Window(period, unit, zone, now, buckets, previous, weeks)


def subject_key(kind: str, subject_id: str) -> str:
    return f"{kind}:{subject_id}"


def _iso(when: datetime | None) -> str | None:
    return when.astimezone(UTC).isoformat() if when is not None else None


@dataclass
class Sources:
    """Where an overview is read from."""

    runs: RunStore
    usage: UsageStore
    #: The organization's workflows and agents now: ``(id, name)`` each.
    workflows: Sequence[tuple[str, str]] = ()
    agents: Sequence[tuple[str, str]] = ()


def _activity() -> dict[str, int]:
    return {
        "started": 0,
        "succeeded": 0,
        "failed": 0,
        "abandoned": 0,
        "open": 0,
        "retried": 0,
        "duration_ms": 0,
        "timed": 0,
    }


def _usage() -> dict[str, Any]:
    return {
        "calls": 0,
        "failed": 0,
        "input": 0,
        "cached": 0,
        "output": 0,
        "thinking": 0,
        "cost": 0.0,
        "unpriced": 0,
    }


async def build_overview(
    organization_id: str,
    sources: Sources,
    win: Window,
    names: Any,
) -> dict[str, Any]:
    """
    :param organization_id: The organization.
    :param sources: The stores, and its workflows and agents now.
    :param win: The period's buckets.
    :param names: Looks up users' names by ID (``async (ids) -> {id: name}``).
    :return: The overview, as ``OverviewOut`` describes it.
    """
    now = win.now
    since, start = win.since, win.start

    run_rows, truncated = await sources.runs.started_since(
        organization_id, since, limit=MAX_RUNS
    )
    model_hours = await sources.usage.model_hours(organization_id, since, now)
    invocation_hours = await sources.usage.invocation_hours(organization_id, since, now)
    authors = await sources.usage.authors(organization_id, start, now)
    tools = await sources.usage.tools(organization_id, start, now)
    people_rows = await sources.usage.people(organization_id, start, now)
    failures_of_invocations = await sources.usage.invocation_failures(
        organization_id, start, now
    )
    used_subjects = await sources.usage.subjects(organization_id)
    latest_runs = await sources.runs.latest_by_agent(organization_id)
    paused, _ = await sources.runs.page(
        organization_id, statuses=[PAUSED], limit=MAX_WAITING
    )
    open_counts = await sources.runs.open_counts(organization_id)
    recording_since = await sources.usage.first_call(organization_id)

    # ------------------------------------------------------------ subjects

    subjects: dict[str, dict[str, Any]] = {}

    def subject(
        kind: str,
        subject_id: str,
        name: str = "",
        *,
        current: bool = False,
        last_at: datetime | None = None,
    ) -> None:
        key = subject_key(kind, subject_id)
        found = subjects.setdefault(
            key,
            {
                "key": key,
                "kind": kind,
                "id": subject_id,
                "name": name or subject_id,
                "current": False,
                "last_at": None,
            },
        )
        if current:
            found["current"] = True
            found["name"] = name or found["name"]
        elif name and not found["current"] and found["name"] == subject_id:
            found["name"] = name
        if last_at is not None and (
            found["last_at"] is None or last_at > found["last_at"]
        ):
            found["last_at"] = last_at

    for workflow_id, name in sources.workflows:
        subject("workflow", workflow_id, name, current=True)
    for agent_id, name in sources.agents:
        subject("agent", agent_id, name, current=True)
    for row in latest_runs:
        subject("workflow", row["agent_id"], row["agent_name"], last_at=row["last_at"])
    for row in used_subjects:
        name = ASSISTANT_NAME if row["kind"] == "assistant" else row["subject_name"]
        subject(
            row["kind"],
            row["subject_id"],
            name,
            current=row["kind"] == "assistant",
            last_at=row["last_at"],
        )

    # ------------------------------------------------------------ usage

    usage: dict[tuple[int, str, str], dict[str, Any]] = defaultdict(_usage)
    weekly: dict[tuple[str, int], dict[str, int]] = defaultdict(
        lambda: {"tokens": 0, "runs": 0}
    )
    for row in model_hours:
        key = subject_key(row["kind"], row["subject_id"])
        hour = row["hour"]
        week = win.week_of(hour)
        if week is not None:
            weekly[(key, week)]["tokens"] += row["input"] + row["output"]
        bucket = win.bucket_of(hour)
        if bucket is None:
            continue
        subject(row["kind"], row["subject_id"])
        fact = usage[(bucket, key, row["model"])]
        for field in (
            "calls",
            "failed",
            "input",
            "cached",
            "output",
            "thinking",
            "unpriced",
        ):
            fact[field] += row[field]
        fact["cost"] += row["cost"]

    # ------------------------------------------------------------ activity

    activity: dict[tuple[int, str], dict[str, int]] = defaultdict(_activity)
    people: dict[tuple[str, str], dict[str, Any]] = {}
    known_names: dict[str, str] = {}
    failures: dict[tuple[str, str | None, str], dict[str, Any]] = {}

    def person(key: str, user: str) -> dict[str, Any]:
        return people.setdefault(
            (key, user),
            {
                "subject": key,
                "user": user,
                "runs": 0,
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
            },
        )

    for run in run_rows:
        key = subject_key("workflow", run["agent_id"])
        created = run["created_at"]
        week = win.week_of(created)
        if week is not None:
            weekly[(key, week)]["runs"] += 1
        bucket = win.bucket_of(created)
        if bucket is None:
            continue
        subject("workflow", run["agent_id"], run["agent_name"])
        fact = activity[(bucket, key)]
        fact["started"] += 1
        status = run["status"]
        if status == SUCCEEDED:
            fact["succeeded"] += 1
        elif status == FAILED:
            fact["failed"] += 1
        elif status == ABANDONED:
            fact["abandoned"] += 1
        else:
            fact["open"] += 1
        if run["attempt"] > 1:
            fact["retried"] += 1
        if run["started_at"] is not None and run["finished_at"] is not None:
            fact["duration_ms"] += max(
                0, int((run["finished_at"] - run["started_at"]).total_seconds() * 1000)
            )
            fact["timed"] += 1
        if bucket < 0:
            continue
        user = run["requested_by"] or ""
        if user:
            person(key, user)["runs"] += 1
            if run["requested_by_name"]:
                known_names.setdefault(user, run["requested_by_name"])
        error = run.get("error")
        if status == FAILED and isinstance(error, Mapping):
            message = str(error.get("message") or "Failed")[:300]
            step = error.get("step") if isinstance(error.get("step"), str) else None
            failure = failures.setdefault(
                (key, step, message),
                {
                    "subject": key,
                    "step": step,
                    "message": message,
                    "count": 0,
                    "last_at": created,
                },
            )
            failure["count"] += 1
            failure["last_at"] = max(failure["last_at"], run["finished_at"] or created)

    for row in invocation_hours:
        key = subject_key(row["kind"], row["subject_id"])
        hour = row["hour"]
        week = win.week_of(hour)
        if week is not None:
            weekly[(key, week)]["runs"] += row["count"]
        bucket = win.bucket_of(hour)
        if bucket is None:
            continue
        subject(row["kind"], row["subject_id"])
        fact = activity[(bucket, key)]
        fact["started"] += row["count"]
        status = row["status"]
        if status == INVOCATION_SUCCEEDED:
            fact["succeeded"] += row["count"]
        elif status == INVOCATION_FAILED:
            fact["failed"] += row["count"]
        elif status == CANCELLED:
            fact["abandoned"] += row["count"]
        elif status == RUNNING and hour < now - STALE:
            # Its process went before it finished.
            fact["failed"] += row["count"]
        else:
            fact["open"] += row["count"]
        fact["duration_ms"] += row["duration_ms"]
        fact["timed"] += row["timed"]

    for row in failures_of_invocations:
        key = subject_key(row["kind"], row["subject_id"])
        message = (row["error"] or "Failed")[:300]
        failure = failures.setdefault(
            (key, None, message),
            {
                "subject": key,
                "step": None,
                "message": message,
                "count": 0,
                "last_at": row["last_at"],
            },
        )
        failure["count"] += row["count"]
        if row["last_at"] is not None and (
            failure["last_at"] is None or row["last_at"] > failure["last_at"]
        ):
            failure["last_at"] = row["last_at"]

    for row in people_rows:
        key = subject_key(row["kind"], row["subject_id"])
        found = person(key, row["user_id"])
        found["runs"] += row["runs"]
        found["calls"] += row["calls"]
        found["tokens"] += row["tokens"]
        found["cost"] += row["cost"]

    # ------------------------------------------------------------ waiting

    waiting = []
    for run in paused:
        pause = run.get("pause") if isinstance(run.get("pause"), Mapping) else {}
        kind = (
            pause.get("kind")
            if pause.get("kind") in ("approval", "human_input")
            else "approval"
        )
        since_text = pause.get("requested_at")
        waiting.append(
            {
                "run_id": run["id"],
                "subject": subject_key("workflow", run["agent_id"]),
                "kind": kind,
                "reason": str(pause.get("reason") or ""),
                "since": since_text
                if isinstance(since_text, str) and since_text
                else _iso(run["updated_at"]),
            }
        )
        subject("workflow", run["agent_id"], run["agent_name"])
    waiting.sort(key=lambda item: item["since"] or "")

    # ------------------------------------------------------------ names

    users = {user for (_, user) in people if user}
    unknown = sorted(users - set(known_names))
    if unknown:
        known_names.update(await names(unknown))

    return {
        "period": win.period,
        "unit": win.unit,
        "time_zone": win.zone.key,
        "as_of": _iso(now),
        "buckets": [{"start": _iso(a), "end": _iso(b)} for a, b in win.buckets],
        "previous": {"start": _iso(win.previous[0]), "end": _iso(win.previous[1])},
        "weeks": [{"start": _iso(a), "end": _iso(b)} for a, b in win.weeks],
        "subjects": [
            {**found, "last_at": _iso(found["last_at"])}
            for found in sorted(
                subjects.values(), key=lambda s: (s["kind"], s["name"].lower())
            )
        ],
        "members": [
            {"id": user, "name": known_names.get(user, user)} for user in sorted(users)
        ],
        "usage": [
            {"bucket": bucket, "subject": key, "model": model, **fact}
            for (bucket, key, model), fact in usage.items()
        ],
        "activity": [
            {"bucket": bucket, "subject": key, **fact}
            for (bucket, key), fact in activity.items()
        ],
        "weekly": [
            {"subject": key, "week": week, **fact}
            for (key, week), fact in weekly.items()
        ],
        "steps": [
            {
                "subject": subject_key(row["kind"], row["subject_id"]),
                "author": row["author"],
                "calls": row["calls"],
                "tokens": row["tokens"],
                "cost": row["cost"],
            }
            for row in authors
        ],
        "tools": [
            {
                "subject": subject_key(row["kind"], row["subject_id"]),
                "tool": row["tool"],
                "calls": row["calls"],
                "failed": row["failed"],
            }
            for row in tools
        ],
        "people": list(people.values()),
        "waiting": waiting,
        "failures": [
            {**failure, "last_at": _iso(failure["last_at"])}
            for failure in sorted(failures.values(), key=lambda f: -f["count"])[
                :MAX_FAILURES
            ]
        ],
        "open": {
            "queued": open_counts.get("queued", 0),
            "running": open_counts.get("running", 0),
            "paused": open_counts.get("paused", 0),
            "waiting": open_counts.get("waiting", 0),
        },
        "recording_since": _iso(recording_since),
        "truncated": truncated,
    }

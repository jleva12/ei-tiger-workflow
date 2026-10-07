"""Organizations' code repositories, and the queue of their ingestions into
the code graph, which the code graph worker (apps/forge-codegraph-worker)
claims from this database."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from forge_admin.db.base import AUDIT_TIMESTAMP, AuditBase, JSONText, ascii_string

#: A job's statuses: waiting for a worker, being ingested, and the three ends.
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
SUPERSEDED = "SUPERSEDED"
FAILED = "FAILED"
ACTIVE = (QUEUED, RUNNING)

#: The queue the code graph worker serves by default (CODEGRAPH_QUEUE).
DEFAULT_QUEUE = "ingestion"


def new_id() -> str:
    """:return: A random UUID, the ID of a new repository or job."""
    return str(uuid4())


class CodeRepository(AuditBase):
    """
    A GitHub repository an organization ingests into the code graph, on one
    branch. The graph itself is in the worker's Spanner database, one per
    GitHub URL whichever organizations have it, and follows the branch it
    was first ingested on; so every organization's row for a URL names that
    branch (the routes refuse another, and the worker fails a job whose
    graph follows another).

    :ivar organization_id: The organization.
    :ivar url: ``https://github.com/<owner>/<name>``, lowercase, as the
        worker names the repository (:mod:`forge_admin.code_repositories.github`).
    :ivar owner: The owner, as written when added.
    :ivar name: The name, as written when added.
    :ivar branch: The branch it ingests.
    """

    __tablename__ = "code_repositories"
    __table_args__ = (UniqueConstraint("organization_id", "url"),)

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ascii_string(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(ascii_string(300), index=True)
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    branch: Mapped[str] = mapped_column(String(255))


class CodeIngestionJob(AuditBase):
    """
    One ingestion of a repository into the code graph: queued here by the
    admin API, claimed and run by the code graph worker, which writes its
    progress and outcome back to the row. The worker reads only this row, so
    it carries what the run needs (URL, branch, commit, who asked).

    A worker claims the oldest row of its queue whose ``eligible_at`` has
    come, under ``FOR UPDATE SKIP LOCKED``, and holds it with a lease it
    renews: ``lease_until``, which ``eligible_at`` follows while it runs, so
    the row of a worker that died becomes claimable when the lease lapses.
    Every write a worker makes is fenced on ``claim_token``, which each claim
    increments: a worker that lost the row can't change it. ``eligible_at``
    is NULL once the job ended. Times are UTC, from the database's clock.

    :ivar repository_id: The repository; deleting it deletes its jobs, and a
        worker running one stops at its next write.
    :ivar organization_id: The repository's organization.
    :ivar url: The repository's URL when queued.
    :ivar branch: The repository's branch when queued.
    :ivar commit_sha: The commit asked for; None for the branch's head, which
        the worker resolves and writes here before it ingests it.
    :ivar requested_by: Who asked, for the record.
    :ivar queue: The worker queue it waits on.
    :ivar status: ``QUEUED``, ``RUNNING``, then ``SUCCEEDED``, ``SUPERSEDED``
        (a newer commit was published first) or ``FAILED``.
    :ivar eligible_at: When a worker may claim it; None once it ended.
    :ivar lease_owner: The worker holding it while it runs.
    :ivar lease_until: When that worker's lease lapses.
    :ivar claim_token: Incremented by every claim; fences the claimer's writes.
    :ivar attempts: The claims it was charged; a worker fails it after its
        limit (CODEGRAPH_MAX_ATTEMPTS).
    :ivar codegraph_repository_id: The graph's repository, once admitted.
    :ivar run_id: The worker's run in that repository, once admitted.
    :ivar generation: The graph generation it published, once it succeeded.
    :ivar error_code: Why it last failed, e.g. ``repository_unreadable``.
    :ivar error_message: The failure, for people.
    :ivar metrics: The run's counts once it ended (files, nodes, edges…).
    :ivar started_at: When it was first claimed.
    :ivar finished_at: When it ended.
    """

    __tablename__ = "code_ingestion_jobs"
    __table_args__ = (
        # What a worker claims by: the queue's eligible rows, oldest first.
        Index("ix_code_ingestion_jobs_claim", "queue", "eligible_at"),
        Index("ix_code_ingestion_jobs_repository", "repository_id", "created_at"),
        # A worker skips a repository another worker is ingesting.
        Index("ix_code_ingestion_jobs_url", "url", "status"),
    )

    id: Mapped[str] = mapped_column(ascii_string(36), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(
        ascii_string(36), ForeignKey("code_repositories.id", ondelete="CASCADE")
    )
    organization_id: Mapped[str] = mapped_column(ascii_string(36), index=True)
    url: Mapped[str] = mapped_column(ascii_string(300))
    branch: Mapped[str] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(ascii_string(64), default=None)
    requested_by: Mapped[str] = mapped_column(String(255))
    queue: Mapped[str] = mapped_column(ascii_string(128), default=DEFAULT_QUEUE)
    status: Mapped[str] = mapped_column(ascii_string(16), default=QUEUED)
    eligible_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP)
    lease_owner: Mapped[str] = mapped_column(String(255), default="")
    lease_until: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)
    claim_token: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    codegraph_repository_id: Mapped[str | None] = mapped_column(
        ascii_string(256), default=None
    )
    run_id: Mapped[str | None] = mapped_column(ascii_string(256), default=None)
    generation: Mapped[int | None] = mapped_column(BigInteger, default=None)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(String(2048), default="")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONText, default=None)
    started_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(AUDIT_TIMESTAMP, default=None)

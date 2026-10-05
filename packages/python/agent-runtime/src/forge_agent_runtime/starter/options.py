"""What a generated project is made of, picked as Spring Initializr's options are."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["SESSIONS", "ARTIFACTS", "MEMORY", "StarterOptions"]

#: Where conversations can be kept: its label, and where it is.
SESSIONS = {
    "memory": ("In memory", "gone when it restarts"),
    "sqlite": ("SQLite", "a file in `data/` (`DATABASE_URL` to put it elsewhere)"),
    "postgresql": ("PostgreSQL", "at `DATABASE_URL`"),
    "mysql": ("MySQL", "at `DATABASE_URL`"),
}
#: Where files the agent's tools save (ADK artifacts) can be kept.
ARTIFACTS = {
    "memory": ("In memory", "gone when it restarts"),
    "folder": ("A folder", "`data/artifacts` (`ARTIFACTS_DIR` to put it elsewhere)"),
    "s3": ("Amazon S3, or any S3", "at `ARTIFACTS_URL` (`s3://bucket/prefix`; `AWS_*` say as whom)"),
}
#: Where long-term memory (the Memory tool) can be kept.
MEMORY = {
    "memory": ("In memory", "gone when it restarts, matched by shared words"),
    "atlas": ("MongoDB Atlas", "at `MONGODB_URI`, searched by meaning (OpenAI embeddings)"),
}
#: A GitHub user or team (@name, @org/team), or an email.
OWNER = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:/[A-Za-z0-9_.-]+)?|[^@\s]+@[^@\s]+\.[^@\s]+")


class StarterOptions(BaseModel):
    """
    What a project is made of: where it keeps conversations, files and
    memories, how it replies, whether it has a UI, who may call it, and who
    owns it. The defaults keep everything in memory, with the UI.
    """

    model_config = ConfigDict(extra="forbid")

    interface: Literal["ui", "api"] = Field(
        default="ui",
        description="The chat UI and the API on one port (ui), or the API alone (api), "
        "for your own front end or services",
    )
    sessions: Literal["memory", "sqlite", "postgresql", "mysql"] = Field(
        default="memory", description="Where conversations are kept"
    )
    artifacts: Literal["memory", "folder", "s3"] = Field(
        default="memory", description="Where files the agent's tools save (ADK artifacts) are kept"
    )
    memory: Literal["memory", "atlas"] = Field(
        default="memory", description="Where long-term memory (the Memory tool) is kept"
    )
    streaming: bool = Field(
        default=True, description="Replies stream as the model writes them; false: they arrive whole"
    )
    api_key: bool = Field(
        default=False,
        description="The API asks for a key (Authorization: Bearer); with the API alone only, "
        "since a page can't keep one secret",
    )
    cors_origins: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Origins a browser may call the API from (https://app.example.com)",
    )
    code_owners: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Who reviews changes (.github/CODEOWNERS): @user, @org/team, or an email",
    )

    @field_validator("cors_origins")
    @classmethod
    def _origins(cls, origins: list[str]) -> list[str]:
        out = []
        for origin in (o.strip().rstrip("/") for o in origins):
            if not origin:
                continue
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
                raise ValueError(f"{origin} isn't an origin: https://host, with a port if any")
            out.append(origin)
        return list(dict.fromkeys(out))

    @field_validator("code_owners")
    @classmethod
    def _owners(cls, owners: list[str]) -> list[str]:
        out = []
        for owner in (o.strip() for o in owners):
            if not owner:
                continue
            if "@" not in owner:
                owner = f"@{owner}"
            if not OWNER.fullmatch(owner):
                raise ValueError(f"{owner} isn't a GitHub user (@name), team (@org/team) or email")
            out.append(owner)
        return list(dict.fromkeys(out))

    @model_validator(mode="after")
    def _a_page_cant_keep_a_key(self) -> StarterOptions:
        if self.api_key and self.interface == "ui":
            raise ValueError(
                "The UI can't keep an API key secret: pick the API alone for a key, or put "
                "the app behind your own sign-in"
            )
        return self

    @property
    def ui(self) -> bool:
        return self.interface == "ui"

    @property
    def keeps_data(self) -> bool:
        """Whether it writes to ``data/`` (SQLite, or the artifacts folder)."""
        return self.sessions == "sqlite" or self.artifacts == "folder"

    @property
    def services(self) -> list[str]:
        """What ``compose.yaml`` runs for development."""
        return [
            *(["postgres"] if self.sessions == "postgresql" else []),
            *(["mysql"] if self.sessions == "mysql" else []),
            *(["s3"] if self.artifacts == "s3" else []),
            *(["mongo"] if self.memory == "atlas" else []),
        ]

    @property
    def extras(self) -> list[str]:
        """forge-agent-runtime's extras the choices need."""
        return [
            "server",
            *(["sessions-postgres"] if self.sessions == "postgresql" else []),
            *(["sessions-mysql"] if self.sessions == "mysql" else []),
            *(["artifacts-s3"] if self.artifacts == "s3" else []),
            *(["memory-atlas"] if self.memory == "atlas" else []),
        ]

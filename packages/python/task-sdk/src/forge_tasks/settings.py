"""Settings read from environment variables (prefix ``HYBRID_``, nested with
``__``) and the working directory's ``.env``, whose ``${NAME}`` references to
the repository's ``.env.common`` are resolved (:mod:`forge_tasks.env_files`).

:class:`CoreSettings` is the infrastructure every task shares: the tasks to
run, the queue's Redis and the Mongo connection. A task's own options are
one section, ``HYBRID_<NAME>__*``, read with :func:`load_section` into the
model its factory declares, so each package owns its settings.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, SecretStr, create_model
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from forge_tasks.env_files import EnvFileSettingsSource

ENV_PREFIX = "HYBRID_"


class EnvSettings(BaseSettings):
    """Base for settings read like the worker's: ``HYBRID_*`` variables, then
    ``.env`` with its ``${NAME}`` references resolved."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_nested_delimiter="__", env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, EnvFileSettingsSource.replacing(dotenv_settings), file_secret_settings)


class MongoSettings(BaseModel):
    uri: SecretStr = SecretStr("mongodb://localhost:27017/?directConnection=true")
    database: str = "forge_tasks"


class CoreSettings(EnvSettings):
    """What every task shares: ``HYBRID_ENABLED_TASKS``, ``HYBRID_REDIS_URL``
    and ``HYBRID_MONGO__*``."""

    enabled_tasks: list[str] = Field(default_factory=lambda: ["adk_workflows"])
    redis_url: str = "redis://localhost:6379/0"
    mongo: MongoSettings = MongoSettings()


def load_section[M: BaseModel](name: str, model: type[M], **values: Any) -> M:
    """Read section ``name`` (``HYBRID_<NAME>__*``) into ``model``; ``values``
    override what the environment says."""
    fields: dict[str, Any] = {name: (model, Field(default_factory=model))}
    settings_cls = create_model(f"{model.__name__}Section", __base__=EnvSettings, **fields)
    loaded = settings_cls(**({name: values} if values else {}))
    return getattr(loaded, name)  # type: ignore[no-any-return]

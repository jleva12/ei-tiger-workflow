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
    """
    Represents environment settings configuration.

    This class is used to define environment-based settings with configurations for
    environment variable prefix, nested delimiter, environment file, and to ignore
    extra unspecified settings. It provides customization of the settings sources to
    control the order and behavior of how settings are loaded.

    :ivar model_config: Configuration for handling environment variables including prefix,
                        nested delimiters, environment file, and how extras are treated.
    :type model_config: SettingsConfigDict
    """

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
    """
    Core settings configuration for the application.

    This class provides a detailed configuration for the core settings used
    by the application, including task management, database connections, and
    caching mechanisms. It encapsulates environment-specific settings that
    can be customized as needed. The attributes defined under this class
    represent configurable options required for the proper functioning of
    the system.

    :ivar enabled_tasks: A list of enabled task identifiers that determine
        the specific tasks or workflows to be activated.
    :type enabled_tasks: list[str]
    :ivar redis_url: The URL configuration for the Redis instance, used for
        caching or message brokering.
    :type redis_url: str
    :ivar mongo: Settings object containing MongoDB configuration for database
        connectivity and management.
    :type mongo: MongoSettings
    """
    enabled_tasks: list[str] = Field(default_factory=lambda: ["adk_workflows"])
    redis_url: str = "redis://localhost:6379/0"
    mongo: MongoSettings = MongoSettings()


def load_section[M: BaseModel](name: str, model: type[M], **values: Any) -> M:
    """
    Loads a configuration section into a dynamically created settings model. This function creates a temporary
    Pydantic model class using the provided model as a schema for a named section. Then it loads the specified
    values or environment variables into the configured instance.

    :param name: The name of the configuration section.
    :type name: str
    :param model: The Pydantic model class that defines the schema for the section.
    :type model: type[M]
    :param values: Optional keyword arguments that represent the values for the section. These will override any
                   values loaded from the environment.
    :type values: Any
    :return: An instance of the provided model with the values loaded from environment variables or given parameters.
    :rtype: M
    """
    fields: dict[str, Any] = {name: (model, Field(default_factory=model))}
    settings_cls = create_model(f"{model.__name__}Section", __base__=EnvSettings, **fields)
    loaded = settings_cls(**({name: values} if values else {}))
    return getattr(loaded, name)  # type: ignore[no-any-return]

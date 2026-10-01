"""What the ``/agents`` routes run: an ADK runner per app, over shared services."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi import FastAPI
from forge_common.adk.models import ProviderModels
from google.adk.apps import App
from google.adk.artifacts import BaseArtifactService, InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from sqlalchemy.ext.asyncio import AsyncEngine

from forge_admin.agents import forge
from forge_admin.agents.person import PersonLookup, database_people
from forge_admin.agents.screens import Screens
from forge_admin.config import Settings

NO_API_KEY = (
    "The assistant isn't set up yet: it needs a Gemini API key. Set "
    "FORGE_ADMIN_GOOGLE_API_KEY in apps/forge-admin-api/.env (or name the shared "
    "model_provider.yaml in FORGE_ADMIN_MODEL_PROVIDER_CONFIG) and restart the "
    "admin API."
)


@dataclass
class AgentRuntime:
    """
    The agents this API serves and where their conversations are kept.

    :param runners: An ADK runner per app, by app name.
    :param sessions: The conversations: ADK sessions and their events.
    :param artifacts: The files agents save in a conversation.
    :param unavailable: Why runs can't start, e.g. no API key, which the
        person gets as the reply; None when they can.
    :param people: Looks up who each run's person is, for the agent; None
        leaves them unnamed.
    :param screens: What each app's agent has on each screen, by app name,
        for apps whose agent's tools depend on it.
    :param models: The models each app's agent may run on, by app name, for
        apps whose agent runs on a model provider configuration.
    """

    runners: dict[str, Runner]
    sessions: BaseSessionService
    artifacts: BaseArtifactService
    unavailable: str | None = None
    people: PersonLookup | None = None
    screens: dict[str, Screens] = field(default_factory=dict)
    models: dict[str, ProviderModels] = field(default_factory=dict)

    @classmethod
    def create(
        cls, settings: Settings, engine: AsyncEngine, app: FastAPI | None = None
    ) -> "AgentRuntime":
        """
        The Forge assistant, keeping conversations in the admin database and
        looking up the people it talks to there.

        ADK creates its tables there on first use (``ADK_TABLES``). Artifacts
        are kept in memory for now: none of the agents saves any yet, and they
        are gone after a restart.

        :param settings: The application settings.
        :param engine: The admin database's engine, whose pool it shares.
        :param app: The admin API, whose routes the assistant's tools call as
            the person; without it, it has none of those tools.
        :return: The runtime.
        """
        return cls.of(
            [forge.create_app(settings, app=app)],
            sessions=DatabaseSessionService(db_engine=engine),
            artifacts=InMemoryArtifactService(),
            # The shared configuration has its own keys, each required.
            unavailable=(
                None
                if settings.model_provider_config or settings.google_api_key
                else NO_API_KEY
            ),
            people=database_people(engine),
        )

    @classmethod
    def of(
        cls,
        apps: Iterable[App],
        *,
        sessions: BaseSessionService,
        artifacts: BaseArtifactService,
        unavailable: str | None = None,
        people: PersonLookup | None = None,
    ) -> "AgentRuntime":
        """
        Constructs an instance of AgentRuntime with the provided applications, session service,
        artifact service, and an optional unavailable app identifier. This factory method is
        responsible for initializing the runner instances for the provided applications.

        :param apps: An iterable collection of App instances representing the applications to
                     be managed by the agent runtime. Each app must have a unique name to serve
                     as a key for the runner mappings.
        :param sessions: A service implementing the BaseSessionService interface, which is used
                         to handle session-related operations for the runners.
        :param artifacts: A service implementing the BaseArtifactService interface, which is
                          responsible for artifact-related operations required by the runners.
        :param unavailable: A string representing the name of an application that is unavailable
                            or None if all applications are available.
        :param people: Looks up who each run's person is; None leaves them unnamed.

        :return: An instance of AgentRuntime initialized with runners for the given applications,
                 along with the session and artifact services.
        """
        apps = list(apps)
        runners = {
            app.name: Runner(
                app=app, session_service=sessions, artifact_service=artifacts
            )
            for app in apps
        }
        screens = {
            app.name: found
            for app in apps
            if isinstance(found := getattr(app.root_agent, "screens", None), Screens)
        }
        models = {
            app.name: app.root_agent.model
            for app in apps
            if isinstance(getattr(app.root_agent, "model", None), ProviderModels)
        }
        return cls(runners, sessions, artifacts, unavailable, people, screens, models)

    async def close(self) -> None:
        """
        Closes all runners managed by this instance asynchronously.

        This method iterates over all runners stored in the `runners` attribute
        and invokes their respective `close` method asynchronously to ensure
        proper cleanup of resources.

        :return: None
        """
        for runner in self.runners.values():
            await runner.close()

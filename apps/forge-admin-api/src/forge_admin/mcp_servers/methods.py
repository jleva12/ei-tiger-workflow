"""The auth methods that send what they're given: none, an API key in a
header, a bearer token."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from forge_admin.mcp_servers.auth import (
    HEADER_NAME_PATTERN,
    AuthContext,
    AuthMethod,
    Credentials,
    NoFields,
)

Secret = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)
]


class NoAuth(AuthMethod[NoFields, NoFields]):
    kind = "none"
    label = "None"
    description = "The server takes calls without credentials, or its URL carries them."

    async def credentials(
        self, context: AuthContext[NoFields, NoFields]
    ) -> Credentials:
        return Credentials()


class ApiKeySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: str = Field(
        default="X-API-Key",
        pattern=HEADER_NAME_PATTERN,
        title="Header",
        description="The header the key goes in.",
        json_schema_extra={"placeholder": "X-API-Key"},
    )
    scheme: str = Field(
        default="",
        max_length=64,
        pattern=r"^[A-Za-z0-9._~+/-]*$",
        title="Scheme",
        description="Written before the key, with a space: Bearer, Token… Empty for the key alone.",
        json_schema_extra={"placeholder": "None"},
    )


class ApiKeySecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Secret = Field(
        title="API key", description="Kept encrypted; nobody can read it back."
    )


class ApiKeyAuth(AuthMethod[ApiKeySettings, ApiKeySecrets]):
    kind = "api_key"
    label = "API key"
    description = "A key the server issued, sent in a header of your choosing."
    settings_model = ApiKeySettings
    secrets_model = ApiKeySecrets

    async def credentials(
        self, context: AuthContext[ApiKeySettings, ApiKeySecrets]
    ) -> Credentials:
        scheme, key = context.settings.scheme, context.secrets.key
        return Credentials(
            {context.settings.header: f"{scheme} {key}" if scheme else key}
        )


class BearerSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Secret = Field(
        title="Token",
        description="Sent as Authorization: Bearer …; kept encrypted, nobody can read it back.",
    )


class BearerAuth(AuthMethod[NoFields, BearerSecrets]):
    kind = "bearer"
    label = "Bearer token"
    description = "A long-lived access token, sent in the Authorization header."
    secrets_model = BearerSecrets

    async def credentials(
        self, context: AuthContext[NoFields, BearerSecrets]
    ) -> Credentials:
        return Credentials({"Authorization": f"Bearer {context.secrets.token}"})

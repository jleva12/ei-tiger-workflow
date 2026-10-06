"""An MCP server that wants OAuth, and its authorization server, as an
httpx2 mock transport: what the MCP authorization spec has them answer."""

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx

MCP = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"


@dataclass
class FakeOAuth:
    #: Publish protected resource metadata (RFC 9728); off for a server from
    #: before it.
    resource_metadata: bool = True
    #: Let clients register themselves (RFC 7591).
    registration: bool = True
    #: How long access tokens last, in seconds.
    lifetime: int = 3600
    #: Rotate refresh tokens.
    rotate: bool = True
    requests: list[httpx.Request] = field(default_factory=list)
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Codes handed out: code -> (client_id, challenge, redirect_uri, resource).
    codes: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=set)
    issued: int = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def authorize(self, url: str) -> str:
        """Sign in at an authorization URL: hand out a code for it."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = (
            query["client_id"],
            query["code_challenge"],
            query["redirect_uri"],
            query["resource"],
        )
        return code

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {
            ("POST", MCP): self._mcp,
            (
                "GET",
                "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
            ): self._resource,
            ("GET", f"{ISSUER}/.well-known/oauth-authorization-server"): self._metadata,
            ("POST", f"{ISSUER}/register"): self._register,
            ("POST", f"{ISSUER}/token"): self._token,
            ("POST", "https://mcp.example.com/token"): self._token,
            ("POST", "https://mcp.example.com/register"): self._register,
        }
        route = routes.get((request.method, url))
        return route(request) if route else httpx.Response(404)

    def _mcp(self, request: httpx.Request) -> httpx.Response:
        challenge = 'Bearer error="invalid_token"'
        if self.resource_metadata:
            challenge += (
                ', resource_metadata="https://mcp.example.com/.well-known/'
                'oauth-protected-resource/mcp", scope="tools:read"'
            )
        return httpx.Response(401, headers={"WWW-Authenticate": challenge})

    def _resource(self, request: httpx.Request) -> httpx.Response:
        if not self.resource_metadata:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "resource": MCP,
                "authorization_servers": [ISSUER],
                "scopes_supported": ["tools:read", "tools:write"],
            },
        )

    def _metadata(self, request: httpx.Request) -> httpx.Response:
        metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "code_challenge_methods_supported": ["S256"],
        }
        if self.registration:
            metadata["registration_endpoint"] = f"{ISSUER}/register"
        return httpx.Response(200, json=metadata)

    def _register(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        client_id = f"client-{len(self.clients)}"
        self.clients[client_id] = body
        return httpx.Response(
            201, json={"client_id": client_id, "token_endpoint_auth_method": "none"}
        )

    def _tokens(self, scope: str = "tools:read") -> dict[str, Any]:
        self.issued += 1
        refresh = f"refresh-{self.issued}"
        self.refresh_tokens.add(refresh)
        return {
            "access_token": f"access-{self.issued}",
            "token_type": "Bearer",
            "expires_in": self.lifetime,
            "refresh_token": refresh,
            "scope": scope,
        }

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        grant = form["grant_type"]
        if grant == "authorization_code":
            client_id, challenge, redirect_uri, resource = self.codes.pop(
                form["code"], ("", "", "", "")
            )
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            verified = (
                base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
            )
            if not (
                verified
                and form["client_id"] == client_id
                and form["redirect_uri"] == redirect_uri
                and form["resource"] == resource
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._tokens())
        if grant == "refresh_token":
            if form["refresh_token"] not in self.refresh_tokens:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "revoked"}
                )
            if self.rotate:
                self.refresh_tokens.discard(form["refresh_token"])
            tokens = self._tokens()
            if not self.rotate:
                del tokens["refresh_token"]
            return httpx.Response(200, json=tokens)
        if grant == "client_credentials":
            basic = request.headers.get("Authorization", "")
            if basic != "Basic " + base64.b64encode(b"svc:s3cret").decode():
                return httpx.Response(401, json={"error": "invalid_client"})
            tokens = self._tokens(form.get("scope", ""))
            del tokens["refresh_token"]
            return httpx.Response(200, json=tokens)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

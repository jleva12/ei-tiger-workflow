"""FastMCP server construction from `McpSettings`, plus the injected auth and middleware."""

import contextlib
import time
from collections.abc import Sequence

import structlog
from fastmcp import FastMCP
from fastmcp.exceptions import FastMCPError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from forge_common.logging import get_logger
from mcp import MCPError

from forge_codegraph_mcp.core.settings import Settings
from forge_codegraph_mcp.tools.base import Tool, Toolset, ToolsetSource

log = get_logger(__name__)


class McpLoggingMiddleware(Middleware):
    """
    Middleware for logging MCP request details.

    This middleware logs metadata and timing information for MCP requests.
    It captures details such as the MCP method, target, and session ID. It
    also handles logging for both successful requests and errors, suppressing
    tracebacks for expected client-facing exceptions.

    :ivar context: The middleware context that provides details about the
                   incoming MCP request and its associated metadata.
    :type context: MiddlewareContext
    """

    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        """
        Handles the middleware logic for processing requests within the system, including logging
        details about the request execution and catching specific exceptions. It attaches
        contextual metadata to structured logs and records timing metrics for request execution.

        :param context: The middleware context containing request details to process.
        :type context: MiddlewareContext
        :param call_next: The callable function to execute the next step in the processing chain.
        :type call_next: CallNext
        :return: The result of executing the `call_next` function after processing.
        :rtype: Any
        :raises FastMCPError: Raised when a client-facing error specific to FastMCP occurs.
        :raises MCPError: Raised when a client-facing error specific to MCP occurs.
        """
        fields = {"mcp_method": context.method}
        if target := getattr(context.message, "name", None) or getattr(
            context.message, "uri", None
        ):
            fields["mcp_target"] = str(target)
        if context.fastmcp_context is not None:
            # Stateless requests have no session.
            with contextlib.suppress(RuntimeError):
                fields["mcp_session_id"] = context.fastmcp_context.session_id

        start = time.perf_counter()
        with structlog.contextvars.bound_contextvars(**fields):
            try:
                result = await call_next(context)
            except (FastMCPError, MCPError) as exc:
                # Expected, client-facing errors (ToolError, rate limits, scope errors, ...): no traceback.
                log.warning(
                    "mcp.request", status="error", error=str(exc), duration_ms=self._ms(start)
                )
                raise
            except Exception:
                log.exception("mcp.request", status="error", duration_ms=self._ms(start))
                raise
            log.info("mcp.request", status="ok", duration_ms=self._ms(start))
            return result

    @staticmethod
    def _ms(start: float) -> float:
        """
        Calculates the elapsed time in milliseconds from the given start time.

        This static method computes the time difference between the current
        performance counter value and the provided starting value. The result
        is rounded to two decimal places for precision.

        :param start: The starting time measured using `time.perf_counter`.
                      It should be a float representing the reference point
                      in seconds.

        :return: A float representing the elapsed time in milliseconds
                 since the starting point.
        """
        return round((time.perf_counter() - start) * 1000, 2)


class McpServerFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = settings.mcp

    def create(
        self,
        toolsets: Sequence[ToolsetSource],
        auth: AuthProvider | None = None,
        middleware: Sequence[Middleware] = (),
    ) -> FastMCP:
        """
        Builds the FastMCP server.

        Middleware runs outermost first: request logging, rate limiting (when enabled in
        settings), then ``middleware`` in the order given. Auth runs before all of it, at
        the HTTP layer.

        :param toolsets: Toolset classes or instances, and function ``Tool`` s, to register.
        :type toolsets: Sequence[ToolsetSource]
        :param auth: Auth for the MCP endpoint; ``None`` leaves it open.
        :type auth: AuthProvider | None
        :param middleware: Additional FastMCP middleware.
        :type middleware: Sequence[Middleware]
        :rtype: FastMCP
        """
        server = FastMCP(
            name=self.config.name,
            instructions=self.config.instructions,
            version=self.settings.version,
            auth=auth,
            middleware=[*self._builtin_middleware(), *middleware],
            mask_error_details=self.config.mask_error_details,
            strict_input_validation=self.config.strict_input_validation,
            on_duplicate="error",
        )
        for source in toolsets:
            self._component(source).register(server)
        return server

    def _component(self, source: ToolsetSource) -> Toolset | Tool:
        if isinstance(source, Toolset | Tool):
            return source
        if isinstance(source, type) and issubclass(source, Toolset):
            return source(self.settings)
        raise TypeError(f"expected a Toolset class or instance, or a Tool; got {source!r}")

    def http_app(self, server: FastMCP) -> StarletteWithLifespan:
        stateless = self.config.stateless_http
        return server.http_app(
            path=self.config.path,
            transport="http",
            stateless_http=stateless,
            json_response=self.config.json_response,
            session_idle_timeout=None if stateless else self.config.session_idle_timeout,
            host_origin_protection=self.config.host_origin_protection,
            allowed_hosts=self.config.allowed_hosts or None,
            allowed_origins=self.config.allowed_origins or None,
        )

    def _builtin_middleware(self) -> list[Middleware]:
        middleware: list[Middleware] = [McpLoggingMiddleware()]
        if self.config.rate_limit.enabled:
            middleware.append(
                RateLimitingMiddleware(
                    max_requests_per_second=self.config.rate_limit.requests_per_second,
                    burst_capacity=self.config.rate_limit.burst_capacity,
                    get_client_id=_rate_limit_key,
                )
            )
        return middleware


def _rate_limit_key(_context: MiddlewareContext) -> str:
    """
    Generates a unique key for rate-limiting purposes, based on either the current client
    access token or the originating client IP address.

    If an access token is available, the key will be based on the token's client ID.
    Otherwise, it attempts to retrieve the client IP from the HTTP request. If no client
    IP is available or the request context is missing, a fallback key is used.

    :param _context: The middleware context used for generating the key. The context is not
        directly used in this implementation.
    :return: A string representing the generated key. The key format falls into one of three
        categories: "client:{client_id}" if an access token is available, "ip:{client_host}"
        if the originating client's IP address is accessible, or "local" otherwise.
    """
    if token := get_access_token():
        return f"client:{token.client_id}"
    try:
        client = get_http_request().client
    except RuntimeError:
        return "local"
    return f"ip:{client.host}" if client else "unknown"

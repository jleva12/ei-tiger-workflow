"""The code graph worker's HTTP API (apps/forge-codegraph-worker): reading a
repository's published graph and its ingestion audit, and searching
repositories' code. Every call sends the worker's API token
(``CODEGRAPH_ADMISSION_TOKEN``); the caller decides which repositories a
reader may see."""

from typing import Any, Self

import httpx2 as httpx

#: What the graph reads are named on the worker, after the repository.
READS = frozenset({"graph", "neighbors", "symbols", "source", "node", "stats", "runs"})


class WorkerError(Exception):
    """
    The worker refused a call, or could not be reached.

    :ivar status: The worker's HTTP status; 0 when it could not be reached.
    :ivar code: Its error code, e.g. ``not_found``.
    :ivar message: Its explanation.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class CodeGraph:
    """
    Calls the worker's API with its token.

    :param http: A client for the worker only: its base URL, the token and
        the timeout are set on it.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def connect(cls, url: str, token: str, *, timeout: float = 30.0) -> Self:
        """
        :param url: The worker's base URL, e.g. ``http://127.0.0.1:8090``.
        :param token: Its API token.
        :param timeout: Seconds a call may take.
        :return: A client.
        """
        return cls(
            httpx.AsyncClient(
                base_url=url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                follow_redirects=False,
            )
        )

    async def read(
        self, repository_id: str, what: str, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        """
        Read a repository's published graph, or about it.

        :param repository_id: The graph's repository (``repo:…``).
        :param what: One of :data:`READS`.
        :param params: The query, e.g. ``{"node": …, "generation": 3}``;
            empty values are left out.
        :return: The worker's answer.
        :raises WorkerError: 404 for a node it doesn't have, 400 for a bad
            query.
        """
        if what not in READS:
            raise ValueError(f"not a graph read: {what}")
        return await self._call(
            "GET",
            f"/v1/repositories/{repository_id}/{what}",
            params={k: str(v) for k, v in (params or {}).items() if v != ""},
        )

    async def search(
        self,
        repository_ids: list[str],
        query: str,
        *,
        limit: int = 8,
        source: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Search repositories' code, best first.

        :param repository_ids: The graphs' repositories, at most 50.
        :param query: A question in plain language, or a name.
        :param limit: How many hits, 1 to 25.
        :param source: Each hit with its code (``text``).
        :return: The hits: ``{repository_id, id, kind, qualified_name, name,
            file, line, score, signature, snippet, text, text_start_line,
            text_end_line}``.
        :raises WorkerError: The worker refused or could not be reached.
        """
        if not repository_ids:
            return []
        answer = await self._call(
            "POST",
            "/v1/search",
            json={
                "repository_ids": repository_ids,
                "query": query,
                "limit": limit,
                "source": source,
            },
        )
        hits = answer.get("hits")
        return (
            [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as error:
            raise WorkerError(
                0, "unreachable", str(error) or type(error).__name__
            ) from None
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise WorkerError(
                response.status_code, "invalid_response", response.text[:200]
            )
        if response.is_error:
            raise WorkerError(
                response.status_code,
                str(answer.get("code", "error")),
                str(answer.get("message", "")),
            )
        return answer

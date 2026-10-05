"""SourceLoaders turn a URI into a SourceFile (bytes + identity)."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from forge_task_documents.errors import FileTooLargeError
from forge_task_documents.models import SourceFile
from forge_tasks.errors import TransientError


class LocalFileLoader:
    schemes = frozenset({"", "file"})

    def __init__(self, *, max_bytes: int = 100 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes

    async def load(
        self,
        uri: str,
        *,
        tenant_id: str,
        doc_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SourceFile:
        path = Path(unquote(urlparse(uri).path) if uri.startswith("file://") else uri)
        size = path.stat().st_size
        if size > self.max_bytes:
            raise FileTooLargeError(f"{path.name} is {size} bytes (limit {self.max_bytes})")
        data = await asyncio.to_thread(path.read_bytes)
        return SourceFile(
            tenant_id=tenant_id,
            doc_id=doc_id,
            filename=filename or path.name,
            data=data,
            media_type=media_type or mimetypes.guess_type(path.name)[0],
            uri=uri,
            metadata=metadata or {},
        )


class S3Loader:
    """s3://bucket/key -> SourceFile. boto3 is sync, so it runs in a thread."""

    schemes = frozenset({"s3"})

    def __init__(self, *, client: Any = None, max_bytes: int = 100 * 1024 * 1024) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3")
        self._s3 = client
        self.max_bytes = max_bytes

    def _get(self, bucket: str, key: str) -> tuple[bytes, str | None]:
        """
        Retrieves an object from the specified S3 bucket and key if it does not exceed the
        maximum allowed size. Returns both the object data and its content type.

        :param bucket: The name of the S3 bucket where the object is stored.
        :type bucket: str
        :param key: The key of the object to retrieve within the specified S3 bucket.
        :type key: str
        :return: A tuple consisting of the object data as bytes and its content type as
            a string or None if no content type is specified.
        :rtype: tuple[bytes, str | None]
        :raises FileTooLargeError: If the size of the object exceeds the maximum allowable
            size for retrieval.
        :raises FileNotFoundError: If the specified object cannot be found in the S3
            bucket or access to it is denied.
        :raises TransientError: For other errors encountered when interacting with S3
            (e.g., network issues or service errors).
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            head = self._s3.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] > self.max_bytes:
                raise FileTooLargeError(f"s3://{bucket}/{key} is {head['ContentLength']} bytes")
            obj = self._s3.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read(), obj.get("ContentType")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "AccessDenied", "403"):
                raise FileNotFoundError(f"s3://{bucket}/{key}: {code}") from exc
            raise TransientError(f"s3 error: {exc}") from exc
        except BotoCoreError as exc:
            raise TransientError(f"s3 error: {exc}") from exc

    async def load(
        self,
        uri: str,
        *,
        tenant_id: str,
        doc_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SourceFile:
        """
        Asynchronously loads a file from a specified URI, retrieves its content, and constructs a
        SourceFile instance. This function utilizes a thread-based I/O operation for fetching
        data from the given URI and processes optional metadata to generate a SourceFile object.

        :param uri: The URI of the file to load.
        :type uri: str
        :param tenant_id: The tenant identifier associated with the file.
        :type tenant_id: str
        :param doc_id: The unique document identifier for the file.
        :type doc_id: str
        :param filename: The name of the file, optional. If not provided, the name will
            be derived from the URI's key path.
        :type filename: str | None
        :param media_type: The type of the media (e.g., MIME type) of the file. If not
            provided, the type will be inferred from the content.
        :type media_type: str | None
        :param metadata: A dictionary containing additional metadata associated with the
            file. Optional parameter.
        :type metadata: dict[str, Any] | None
        :return: A constructed SourceFile object containing the file's content and
            associated metadata.
        :rtype: SourceFile
        """
        parsed = urlparse(uri)
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        data, content_type = await asyncio.to_thread(self._get, bucket, key)
        return SourceFile(
            tenant_id=tenant_id,
            doc_id=doc_id,
            filename=filename or Path(key).name,
            data=data,
            media_type=media_type or (content_type if content_type != "binary/octet-stream" else None),
            uri=uri,
            metadata=metadata or {},
        )


class LoaderRouter:
    """Dispatches on URI scheme to the right SourceLoader."""

    def __init__(self, loaders: list[Any]) -> None:
        self._by_scheme: dict[str, Any] = {}
        for loader in loaders:
            for scheme in loader.schemes:
                self._by_scheme[scheme] = loader
        self.schemes = frozenset(self._by_scheme)

    async def load(self, uri: str, **kwargs: Any) -> SourceFile:
        """
        Asynchronously loads a source file based on the provided URI and optional parameters.
        Determines the appropriate loader based on the URI scheme, and raises an exception
        if no loader is available for the scheme. Returns the loaded SourceFile instance.

        :param uri: A string representing the URI of the source file to be loaded.
        :param kwargs: Optional keyword arguments to be passed to the loader.
        :return: An instance of SourceFile representing the loaded file.
        :rtype: SourceFile
        :raises ValueError: If no loader exists for the given URI scheme.
        """
        scheme = urlparse(uri).scheme
        scheme = "" if len(scheme) == 1 else scheme  # Windows drive letters
        loader = self._by_scheme.get(scheme)
        if loader is None:
            raise ValueError(f"no loader for scheme {scheme!r} ({uri})")
        result: SourceFile = await loader.load(uri, **kwargs)
        return result

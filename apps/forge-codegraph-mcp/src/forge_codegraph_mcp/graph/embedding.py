"""Embedding a question the way the worker embedded the graph.

The worker embeds each declaration's retrieval document in windows of at most
``max_input_bytes`` (on character boundaries, so every byte is embedded once)
and stores the unit-length, byte-length-weighted mean of the windows'
vectors. Questions go through the same transform with the same model and
dimensions, or their vectors would not be comparable.
"""

from __future__ import annotations

import math
from typing import Protocol

from openai import AsyncOpenAI

from forge_codegraph_mcp.graph.errors import InvalidRequest

DEFAULT_DIMENSIONS = 3072
DEFAULT_MAX_INPUT_BYTES = 7500
MAX_INPUTS_PER_REQUEST = 16


class Embedder(Protocol):
    """
    Defines the structure for an embedder protocol.

    An implementation of this protocol is responsible for providing text embedding
    functionalities. It specifies the required properties and method to retrieve
    model metadata and generate embeddings for given texts. This can be used as
    a base type for various embedder implementations.

    :ivar model: The name of the model implemented by the embedder.
    :type model: str
    :ivar dimensions: The dimensionality of the embeddings produced by the model.
    :type dimensions: int
    :ivar max_input_bytes: The maximum number of input bytes accepted by the model.
    :type max_input_bytes: int
    """

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def max_input_bytes(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(Exception):
    """The provider's answer can't be used."""


class OpenAIEmbedder:
    """
    Represents an OpenAI embedding client for generating vector embeddings using
    a specified model.

    This class provides methods to embed textual inputs into vector representations
    and manage the lifecycle of the underlying client. It supports configurable
    input constraints, including dimensions and maximum input size.

    :ivar model: The name of the OpenAI model used for embeddings.
    :type model: str
    :ivar dimensions: The number of dimensions for the embeddings, ranging
        from 1 to 4096.
    :type dimensions: int
    :ivar max_input_bytes: The maximum size (in bytes) of the input text for
        embedding, ranging from 4 to 8000.
    :type max_input_bytes: int
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int,
        *,
        base_url: str | None = None,
        max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
        timeout: float = 90.0,
        max_retries: int = 5,
    ) -> None:
        if not api_key or not model or not 1 <= dimensions <= 4096:
            raise ValueError("embedding: an API key, a model and 1-4096 dimensions are required")
        if not 4 <= max_input_bytes <= 8000:
            raise ValueError("embedding: max_input_bytes must be 4-8000")
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url or None, timeout=timeout, max_retries=max_retries
        )
        self._model = model
        self._dimensions = dimensions
        self._max_input_bytes = max_input_bytes

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_input_bytes(self) -> int:
        return self._max_input_bytes

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not 1 <= len(texts) <= MAX_INPUTS_PER_REQUEST:
            raise EmbeddingError("invalid embedding batch")
        response = await self._client.embeddings.create(
            model=self._model, input=texts, dimensions=self._dimensions, encoding_format="float"
        )
        if len(response.data) != len(texts):
            raise EmbeddingError("embedding response count mismatch")
        out: list[list[float] | None] = [None] * len(texts)
        for row in response.data:
            if not 0 <= row.index < len(out) or out[row.index] is not None:
                raise EmbeddingError("invalid embedding response index")
            out[row.index] = validate_vector(list(row.embedding), self._dimensions)
        return [vector for vector in out if vector is not None]

    async def close(self) -> None:
        await self._client.close()


def validate_vector(vector: list[float], dimensions: int) -> list[float]:
    """
    Validates a given vector against specified dimensions and ensures its mathematical
    integrity. The function checks that the vector has the correct dimensions, contains
    finite values, and possesses a valid non-zero norm. If any of these conditions are
    violated, an EmbeddingError is raised.

    :param vector: The input list of float values representing the vector.
    :param dimensions: The expected number of dimensions for the vector.
    :type vector: list[float]
    :type dimensions: int
    :return: The validated vector, unchanged, if it matches the required
             dimension and mathematical conditions.
    :rtype: list[float]
    :raises EmbeddingError: If the dimensions do not match, the vector contains
                            non-finite values, or the norm is zero or non-finite.
    """
    if not 1 <= dimensions <= 4096 or len(vector) != dimensions:
        raise EmbeddingError("embedding dimension mismatch")
    norm = 0.0
    for x in vector:
        if not math.isfinite(x):
            raise EmbeddingError("non-finite embedding")
        norm += x * x
    if norm == 0 or not math.isfinite(norm):
        raise EmbeddingError("invalid embedding norm")
    return vector


def windows(text: str, max_bytes: int) -> list[str]:
    """
    Divides a given text into chunks of encoded bytes, ensuring each chunk is
    within the specified size limit. Handles UTF-8 character boundaries to
    prevent text corruption in the encoded output.

    :param text: The input string that needs to be chunked.
    :type text: str
    :param max_bytes: The maximum allowed size in bytes for each chunk.
    :type max_bytes: int
    :return: A list of string chunks where each chunk is within the specified
        size limit in bytes.
    :rtype: list[str]
    :raises InvalidRequest: If the input text is empty and cannot be encoded.
    """
    data = text.encode()
    if not data:
        raise InvalidRequest("empty text to embed")
    out: list[str] = []
    while data:
        n = min(len(data), max_bytes)
        while n < len(data) and data[n] & 0xC0 == 0x80:
            n -= 1
        out.append(data[:n].decode())
        data = data[n:]
    return out


def combine(pieces: list[str], vectors: list[list[float]], dimensions: int) -> list[float]:
    """
    Combines a list of text pieces and their associated embedding vectors into a single
    normalized vector representation. This method performs weighted normalization
    of the input vectors based on the length of each corresponding text piece.

    :param pieces: List of textual segments whose embeddings are to be combined. Each
        segment must be a string.
    :param vectors: List of embedding vectors corresponding to the text pieces.
        Each embedding must be a list of floats.
    :param dimensions: The dimensionality of the embedding vectors. It must match
        the size of each vector in `vectors`.
    :return: A single normalized embedding vector as a list of floats with
        the specified dimensionality.
    :raises EmbeddingError: If the number of pieces and vectors do not match,
        or if the `pieces` list is empty.
    """
    if len(vectors) != len(pieces) or not pieces:
        raise EmbeddingError("embedding count mismatch")
    mean = [0.0] * dimensions
    for piece, vector in zip(pieces, vectors, strict=True):
        validate_vector(vector, dimensions)
        weight = len(piece.encode()) / math.sqrt(sum(x * x for x in vector))
        for j, x in enumerate(vector):
            mean[j] += x * weight
    validate_vector(mean, dimensions)
    norm = math.sqrt(sum(x * x for x in mean))
    return [x / norm for x in mean]


async def embed_text(embedder: Embedder, text: str) -> list[float]:
    """
    Embeds text into vectors using the specified embedder.

    This function takes a large text input and splits it into smaller pieces according
    to the maximum input byte size of the embedder. It processes the text in chunks,
    ensuring it does not exceed the maximum number of inputs per request, and then
    generates embeddings for the text. Finally, the function combines the pieces
    and their corresponding vectors into a single result.

    :param embedder: The embedder instance used to process text into embeddings.
    :type embedder: Embedder
    :param text: The input text that needs to be embedded into numerical vectors.
    :type text: str
    :return: A list of floating-point values representing the combined embedding of the input text.
    :rtype: list[float]
    """
    pieces = windows(text, embedder.max_input_bytes)
    vectors: list[list[float]] = []
    for start in range(0, len(pieces), MAX_INPUTS_PER_REQUEST):
        chunk = pieces[start : start + MAX_INPUTS_PER_REQUEST]
        out = await embedder.embed(chunk)
        if len(out) != len(chunk):
            raise EmbeddingError("embedding count mismatch")
        vectors.extend(out)
    return combine(pieces, vectors, embedder.dimensions)

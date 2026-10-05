"""Where knowledge base documents are stored in the bucket (by the
hierarchy, so every level is a prefix), reading them back, and how they're
sent."""

import asyncio
import io

import pytest
from botocore.exceptions import ClientError

from forge_admin.api.routes.knowledge_documents import _disposition, _served_type
from forge_admin.knowledge.storage import (
    READ_CHUNK,
    DocumentStore,
    StorageError,
    StoredFile,
    document_key,
    scope_prefix,
)


def test_each_level_is_a_prefix_of_the_levels_below() -> None:
    org = scope_prefix("o1")
    knowledge_base = scope_prefix("o1", "k1")
    assert (org, knowledge_base) == ("forge/org-o1/", "forge/org-o1/kb-k1/")
    key = document_key(
        organization_id="o1",
        knowledge_base_id="k1",
        document_id="d1",
        name="Refund-runbook.md",
    )
    assert key == "forge/org-o1/kb-k1/documents/d1/Refund-runbook.md"
    assert all(key.startswith(prefix) for prefix in (org, knowledge_base))
    # A sibling's prefix doesn't match: org-o1 isn't a prefix of org-o10.
    assert not scope_prefix("o10").startswith(org)


class _S3:
    """get_object over one object, or an error to raise."""

    def __init__(self, body: bytes = b"", error: Exception | None = None) -> None:
        self.body = io.BytesIO(body)
        self.error = error

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        if self.error:
            raise self.error
        return {"ContentLength": len(self.body.getvalue()), "Body": self.body}


def _read(stored: StoredFile) -> bytes:
    async def main() -> bytes:
        return b"".join([chunk async for chunk in stored.chunks()])

    return asyncio.run(main())


def test_a_stored_file_reads_back_in_chunks_and_closes() -> None:
    content = bytes(range(256)) * (READ_CHUNK // 128)  # two chunks
    s3 = _S3(content)
    store = DocumentStore(s3, "docs", create_bucket=False)

    stored = asyncio.run(store.open("forge/key"))

    assert stored.size == len(content)
    assert _read(stored) == content
    assert s3.body.closed


def test_a_missing_file_is_named_by_s3s_code() -> None:
    missing = ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "Get")
    store = DocumentStore(_S3(error=missing), "docs", create_bucket=False)

    with pytest.raises(StorageError) as raised:
        asyncio.run(store.open("forge/key"))

    assert raised.value.code == "NoSuchKey"


def test_only_this_buckets_uris_have_keys() -> None:
    store = DocumentStore(_S3(), "docs", create_bucket=False)

    assert store.key_of("s3://docs/forge/org-o1/a.md") == "forge/org-o1/a.md"
    assert store.key_of("s3://docs-old/forge/org-o1/a.md") is None
    assert store.key_of("gs://docs/forge/org-o1/a.md") is None


@pytest.mark.parametrize(
    ("filename", "uploaded_as", "served"),
    [
        ("Runbook.pdf", "", "application/pdf"),
        # The extension wins over whatever the browser said.
        ("Rates.csv", "application/vnd.ms-excel", "text/csv"),
        ("notes.mmd", "text/plain", "text/plain"),
        ("blob", "", "application/octet-stream"),
        # Anything a browser would run or render as a page is bytes.
        ("diagram.svg", "image/svg+xml", "application/octet-stream"),
        ("page.html", "text/html", "application/octet-stream"),
        ("page.txt", "text/html; charset=utf-8", "text/plain"),
        ("page", "text/html; charset=utf-8", "application/octet-stream"),
    ],
)
def test_files_are_served_as_their_kind(
    filename: str, uploaded_as: str, served: str
) -> None:
    assert _served_type(filename, uploaded_as) == served


def test_dispositions_name_the_file_in_any_language() -> None:
    assert _disposition("Plan.md", download=False) == (
        "inline; filename=\"Plan.md\"; filename*=UTF-8''Plan.md"
    )
    assert _disposition('Ré "q".pdf', download=True) == (
        "attachment; filename=\"R_ _q_.pdf\"; filename*=UTF-8''R%C3%A9%20%22q%22.pdf"
    )

import io
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types

from forge_common.adk.artifacts import S3ArtifactService

APP = dict(app_name="ca_x", user_id="u1")


def refused(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "op"
    )


class FakeS3:
    """The boto3 S3 calls the service makes, kept in a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        #: Keys a racing save takes just before this one writes them.
        self.taken_first: set[str] = set()
        self.buckets = {"bucket"}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict[str, str], **rest: Any
    ) -> None:
        if Key in self.taken_first:
            self.taken_first.discard(Key)
            self.objects[Key] = {
                "Body": b"theirs",
                "Metadata": {},
                "LastModified": datetime.now(UTC),
            }
        if rest.get("IfNoneMatch") == "*" and Key in self.objects:
            raise refused("PreconditionFailed", 412)
        assert all(value.isascii() for value in Metadata.values())
        self.objects[Key] = {
            "Body": Body,
            "Metadata": Metadata,
            "ContentType": rest.get("ContentType", "binary/octet-stream"),
            "LastModified": datetime(2026, 10, 4, tzinfo=UTC),
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise refused("NoSuchKey", 404)
        found = self.objects[Key]
        return {**found, "Body": io.BytesIO(found["Body"])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise refused("404", 404)
        return {k: v for k, v in self.objects[Key].items() if k != "Body"}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:
        for each in Delete["Objects"]:
            self.objects.pop(each["Key"], None)

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise refused("404", 404)

    def create_bucket(self, *, Bucket: str) -> None:
        self.buckets.add(Bucket)

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        objects = self.objects

        class Pages:
            def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
                keys = sorted(k for k in objects if k.startswith(Prefix))
                # Two pages, as S3 would for a long listing.
                half = len(keys) // 2
                return [
                    {"Contents": [{"Key": k} for k in keys[:half]]},
                    {"Contents": [{"Key": k} for k in keys[half:]]},
                ]

        return Pages()


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def artifacts(s3: FakeS3) -> S3ArtifactService:
    return S3ArtifactService("bucket", prefix="/agents/", client=s3)


async def test_versions_count_up_under_the_prefix(artifacts: S3ArtifactService, s3: FakeS3) -> None:
    for text in ("one", "two"):
        await artifacts.save_artifact(
            **APP, session_id="s1", filename="notes.md", artifact=types.Part(text=text)
        )
    assert sorted(s3.objects) == ["agents/ca_x/u1/s1/notes.md/0", "agents/ca_x/u1/s1/notes.md/1"]
    assert await artifacts.list_versions(**APP, session_id="s1", filename="notes.md") == [0, 1]
    latest = await artifacts.load_artifact(**APP, session_id="s1", filename="notes.md")
    first = await artifacts.load_artifact(**APP, session_id="s1", filename="notes.md", version=0)
    assert (latest.text, first.text) == ("two", "one")


async def test_files_keep_their_type_and_name(artifacts: S3ArtifactService) -> None:
    pdf = types.Part(
        inline_data=types.Blob(mime_type="application/pdf", data=b"%PDF", display_name="résumé.pdf")
    )
    await artifacts.save_artifact(
        **APP,
        session_id="s1",
        filename="cv",
        artifact=pdf,
        custom_metadata={"Source": "upload", "pages": 2},
    )
    loaded = await artifacts.load_artifact(**APP, session_id="s1", filename="cv")
    assert loaded.inline_data.data == b"%PDF"
    assert loaded.inline_data.mime_type == "application/pdf"
    assert loaded.inline_data.display_name == "résumé.pdf"
    described = await artifacts.get_artifact_version(**APP, session_id="s1", filename="cv")
    assert described.canonical_uri == "s3://bucket/agents/ca_x/u1/s1/cv/0"
    assert described.custom_metadata == {"Source": "upload", "pages": 2}
    assert described.mime_type == "application/pdf"


async def test_a_racing_save_takes_the_next_version(
    artifacts: S3ArtifactService, s3: FakeS3
) -> None:
    s3.taken_first.add("agents/ca_x/u1/s1/a.txt/0")
    version = await artifacts.save_artifact(
        **APP, session_id="s1", filename="a.txt", artifact=types.Part(text="mine")
    )
    assert version == 1
    assert (await artifacts.load_artifact(**APP, session_id="s1", filename="a.txt")).text == "mine"


async def test_user_files_are_listed_in_every_session(artifacts: S3ArtifactService) -> None:
    await artifacts.save_artifact(
        **APP, filename="user:profile.json", artifact=types.Part(text="{}")
    )
    await artifacts.save_artifact(
        **APP, session_id="s1", filename="reports/q3.csv", artifact=types.Part(text="a,b")
    )
    await artifacts.save_artifact(
        **APP, session_id="s2", filename="other.txt", artifact=types.Part(text="x")
    )
    assert await artifacts.list_artifact_keys(**APP, session_id="s1") == [
        "reports/q3.csv",
        "user:profile.json",
    ]
    assert await artifacts.list_artifact_keys(**APP) == ["user:profile.json"]
    # A filename's own folder isn't one of its versions.
    await artifacts.save_artifact(
        **APP, session_id="s1", filename="reports", artifact=types.Part(text="r")
    )
    assert await artifacts.list_versions(**APP, session_id="s1", filename="reports") == [0]


async def test_deleting_removes_every_version(artifacts: S3ArtifactService, s3: FakeS3) -> None:
    for _ in range(3):
        await artifacts.save_artifact(
            **APP, session_id="s1", filename="a.txt", artifact=types.Part(text="x")
        )
    await artifacts.delete_artifact(**APP, session_id="s1", filename="a.txt")
    assert s3.objects == {}
    assert await artifacts.load_artifact(**APP, session_id="s1", filename="a.txt") is None
    assert await artifacts.get_artifact_version(**APP, session_id="s1", filename="a.txt") is None


async def test_references_are_kept_not_uploaded(artifacts: S3ArtifactService, s3: FakeS3) -> None:
    await artifacts.save_artifact(
        **APP, session_id="s1", filename="src.txt", artifact=types.Part(text="hello")
    )
    ref = types.Part(
        file_data=types.FileData(
            file_uri="artifact://apps/ca_x/users/u1/sessions/s1/artifacts/src.txt/versions/0"
        )
    )
    await artifacts.save_artifact(**APP, session_id="s1", filename="link", artifact=ref)
    assert s3.objects["agents/ca_x/u1/s1/link/0"]["Body"] == b""
    assert (await artifacts.load_artifact(**APP, session_id="s1", filename="link")).text == "hello"
    remote = types.Part(
        file_data=types.FileData(file_uri="https://example.com/a.png", mime_type="image/png")
    )
    await artifacts.save_artifact(**APP, session_id="s1", filename="pic", artifact=remote)
    loaded = await artifacts.load_artifact(**APP, session_id="s1", filename="pic")
    assert (loaded.file_data.file_uri, loaded.file_data.mime_type) == (
        "https://example.com/a.png",
        "image/png",
    )


async def test_session_files_need_a_session(artifacts: S3ArtifactService) -> None:
    with pytest.raises(InputValidationError):
        await artifacts.save_artifact(**APP, filename="a.txt", artifact=types.Part(text="x"))
    with pytest.raises(InputValidationError):
        await artifacts.load_artifact(
            app_name="../x", user_id="u1", session_id="s1", filename="a.txt"
        )


def test_from_url_reads_the_bucket_prefix_and_endpoint() -> None:
    artifacts = S3ArtifactService.from_url(
        "s3://agent-files/prod/chat",
        {
            "AWS_ACCESS_KEY_ID": "k",
            "AWS_SECRET_ACCESS_KEY": "s",
            "AWS_REGION": "eu-west-1",
            "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:9000",
        },
    )
    assert (artifacts.bucket, artifacts.prefix) == ("agent-files", "prod/chat/")
    assert artifacts.client.meta.endpoint_url == "http://127.0.0.1:9000"
    assert artifacts.client.meta.region_name == "eu-west-1"
    assert S3ArtifactService.from_url("s3://b", {"AWS_REGION": "us-east-1"}).prefix == ""
    with pytest.raises(ValueError, match="S3 URL"):
        S3ArtifactService.from_url("gs://b/x", {})


async def test_the_bucket_is_made_on_first_use_when_asked(s3: FakeS3) -> None:
    fresh = S3ArtifactService("new-bucket", client=s3, create_bucket=True)
    assert await fresh.list_artifact_keys(**APP, session_id="s1") == []
    assert "new-bucket" in s3.buckets
    s3.buckets.discard("new-bucket")
    await fresh.list_artifact_keys(**APP, session_id="s1")
    assert "new-bucket" not in s3.buckets  # only the first time

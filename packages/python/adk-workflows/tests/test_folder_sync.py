"""
A run's folder copied out and its changes brought back (support.folder_sync):
only what the copy changed is made in the run's folder, and nothing that came
back is written outside it.
"""

import io
import os
import shutil
import stat
import tarfile
from pathlib import Path

import pytest

from forge_task_adk_workflows.support.folder_sync import Applied, Manifest, SyncError, apply, pack


def packed(folder: Path) -> tuple[bytes, Manifest]:
    out = io.BytesIO()
    manifest = pack(folder, out)
    return out.getvalue(), manifest


def unpacked(archive: bytes, into: Path) -> Path:
    into.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="fully_trusted")
    return into


def archived(folder: Path) -> io.BytesIO:
    """The folder as a tar, as the sandbox's ``tar -cf - .`` sends it."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        tar.add(folder, arcname=".")
    out.seek(0)
    return out


def crafted(*members: tuple[tarfile.TarInfo, bytes]) -> io.BytesIO:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for member, data in members:
            member.size = len(data) if member.isreg() else 0
            tar.addfile(member, io.BytesIO(data) if member.isreg() else None)
    out.seek(0)
    return out


@pytest.fixture
def run(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    (folder / "repo" / "src").mkdir(parents=True)
    (folder / "repo" / "README.md").write_text("hello\n")
    (folder / "repo" / "src" / "main.py").write_text("print('hi')\n")
    (folder / "repo" / "old").mkdir()
    (folder / "repo" / "old" / "stale.txt").write_text("stale\n")
    (folder / "repo" / "link").symlink_to("README.md")
    return folder


def test_it_sends_folders_files_and_links_as_they_are_and_says_what_it_sent(run: Path, tmp_path: Path) -> None:
    os.mkfifo(run / "pipe")
    archive, manifest = packed(run)

    copy = unpacked(archive, tmp_path / "copy")
    assert (copy / "repo" / "src" / "main.py").read_text() == "print('hi')\n"
    assert os.readlink(copy / "repo" / "link") == "README.md"
    # A pipe (or a socket, a device) isn't the folder's to send.
    assert not (copy / "pipe").exists()
    assert set(manifest) == {
        "repo",
        "repo/README.md",
        "repo/link",
        "repo/old",
        "repo/old/stale.txt",
        "repo/src",
        "repo/src/main.py",
    }
    assert manifest["repo/link"].target == "README.md"
    assert manifest["repo/README.md"].size == 6 and len(manifest["repo/README.md"].digest) == 32


def test_only_what_the_copy_changed_is_made_in_the_run_s_folder(run: Path, tmp_path: Path) -> None:
    archive, manifest = packed(run)
    copy = unpacked(archive, tmp_path / "copy")
    # In the copy: a file changed, one made, one made runnable, a folder
    # removed, a link changed, a folder made.
    (copy / "repo" / "README.md").write_text("hello, world\n")
    (copy / "repo" / "src" / "new.py").write_text("x = 1\n")
    (copy / "repo" / "src" / "main.py").chmod(0o755)
    shutil.rmtree(copy / "repo" / "old")
    (copy / "repo" / "link").unlink()
    (copy / "repo" / "link").symlink_to("src/main.py")
    (copy / "build").mkdir()
    untouched = (run / "repo" / "src" / "main.py").stat().st_ino
    # Meanwhile another step writes in the run's folder.
    (run / "repo" / "theirs.txt").write_text("another branch's\n")

    applied = apply(archived(copy), run, manifest)

    assert applied == Applied(
        written=("build", "repo/README.md", "repo/link", "repo/src/main.py", "repo/src/new.py"),
        removed=("repo/old/stale.txt", "repo/old"),
    )
    assert (run / "repo" / "README.md").read_text() == "hello, world\n"
    assert (run / "repo" / "src" / "new.py").read_text() == "x = 1\n"
    assert stat.S_IMODE((run / "repo" / "src" / "main.py").stat().st_mode) == 0o755
    assert (run / "repo" / "src" / "main.py").stat().st_ino != untouched  # written, being runnable now
    assert not (run / "repo" / "old").exists()
    assert os.readlink(run / "repo" / "link") == "src/main.py"
    assert (run / "build").is_dir()
    assert (run / "repo" / "theirs.txt").read_text() == "another branch's\n"


def test_what_the_copy_didn_t_touch_isn_t_written(run: Path, tmp_path: Path) -> None:
    archive, manifest = packed(run)
    copy = unpacked(archive, tmp_path / "copy")
    before = {path: path.stat().st_ino for path in run.rglob("*") if not path.is_symlink()}
    # Another step changes a file meanwhile: the copy didn't, so it stays.
    (run / "repo" / "README.md").write_text("theirs\n")

    assert apply(archived(copy), run, manifest) == Applied(written=(), removed=())
    assert {path: path.stat().st_ino for path in run.rglob("*") if not path.is_symlink()} == before
    assert (run / "repo" / "README.md").read_text() == "theirs\n"


def test_a_folder_the_copy_removed_stays_while_another_step_has_put_something_in_it(run: Path, tmp_path: Path) -> None:
    archive, manifest = packed(run)
    copy = unpacked(archive, tmp_path / "copy")
    shutil.rmtree(copy / "repo" / "old")
    (run / "repo" / "old" / "theirs.txt").write_text("kept\n")

    apply(archived(copy), run, manifest)

    assert not (run / "repo" / "old" / "stale.txt").exists()
    assert (run / "repo" / "old" / "theirs.txt").read_text() == "kept\n"


def test_a_file_that_became_a_folder_and_a_folder_that_became_a_file(run: Path, tmp_path: Path) -> None:
    archive, manifest = packed(run)
    copy = unpacked(archive, tmp_path / "copy")
    (copy / "repo" / "README.md").unlink()
    (copy / "repo" / "README.md").mkdir()
    (copy / "repo" / "README.md" / "index.md").write_text("now a folder\n")
    shutil.rmtree(copy / "repo" / "src")
    (copy / "repo" / "src").write_text("now a file\n")

    apply(archived(copy), run, manifest)

    assert (run / "repo" / "README.md" / "index.md").read_text() == "now a folder\n"
    assert (run / "repo" / "src").read_text() == "now a file\n"


def test_a_file_that_was_a_link_is_written_in_its_place_not_through_it(run: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("the worker's\n")
    (run / "escape").symlink_to(outside)
    archive, manifest = packed(run)
    copy = unpacked(archive, tmp_path / "copy")
    (copy / "escape").unlink()
    (copy / "escape").write_text("replaced\n")

    apply(archived(copy), run, manifest)

    assert outside.read_text() == "the worker's\n"
    assert (run / "escape").read_text() == "replaced\n" and not (run / "escape").is_symlink()


@pytest.mark.parametrize("name", ["../escaped.txt", "/tmp/escaped.txt", "repo/../../escaped.txt"])
def test_a_path_outside_the_run_s_folder_changes_nothing(run: Path, tmp_path: Path, name: str) -> None:
    _, manifest = packed(run)
    bad = tarfile.TarInfo(name)
    changed = tarfile.TarInfo("repo/README.md")

    with pytest.raises(SyncError, match="isn't a path in the run's folder"):
        apply(crafted((changed, b"changed\n"), (bad, b"x")), run, manifest)

    assert (run / "repo" / "README.md").read_text() == "hello\n"
    assert not list(tmp_path.rglob("escaped.txt"))


def test_nothing_is_written_through_a_link_that_leads_out(run: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    _, manifest = packed(run)
    link = tarfile.TarInfo("out")
    link.type = tarfile.SYMTYPE
    link.linkname = str(tmp_path / "elsewhere")
    through = tarfile.TarInfo("out/escaped.txt")

    with pytest.raises(SyncError, match="would be written outside the run's folder"):
        apply(crafted((link, b""), (through, b"x")), run, manifest)

    assert not (tmp_path / "elsewhere" / "escaped.txt").exists()


def test_modes_keep_their_permission_bits_and_the_owner_may_always_write(run: Path) -> None:
    _, manifest = packed(run)
    setuid = tarfile.TarInfo("tool")
    setuid.mode = 0o4555
    archive = crafted((setuid, b"#!/bin/sh\n"))

    apply(archive, run, manifest)

    assert stat.S_IMODE((run / "tool").stat().st_mode) == 0o755


def test_what_isn_t_a_tar_says_so(run: Path) -> None:
    _, manifest = packed(run)
    with pytest.raises(SyncError, match="isn't a tar"):
        apply(io.BytesIO(b"not a tar at all" * 100), run, manifest)

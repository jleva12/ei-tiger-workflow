"""
A run's folder copied somewhere else and its changes brought back
(``support.sandbox_commands``: a Bash step's sandbox).

- :func:`pack` writes the folder's contents as a tar (folders, files and
  links, never followed; sockets and devices left out) and returns its
  :data:`Manifest`: what it sent, each file's hash among it.
- :func:`apply` reads the folder's contents as they came back, as a tar, and
  makes in the run's folder only the changes made to it since: files written
  or made, folders and links made, what was removed. What wasn't touched isn't
  written, so another step changing the run's folder meanwhile (a step of
  another branch) keeps its changes; where both changed one file, what came
  back wins.

What comes back is the sandbox's, so it's read as untrusted: every path is
checked before anything is written (none absolute, none with ``..``), nothing
is written through a link that leads out of the run's folder, and modes keep
only their permission bits (the owner always reads and writes). Links are kept
as they came, wherever they point, as a command on the worker's machine could
make them.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, BinaryIO, Literal

#: What a member of the folder is.
Kind = Literal["dir", "file", "link"]
#: Bytes read at a time.
BLOCK = 1024 * 1024


class SyncError(Exception):
    """The folder couldn't be read, or what came back couldn't be applied:
    the message says why."""


@dataclass(frozen=True)
class Entry:
    """One member of the folder as it was sent."""

    kind: Kind
    #: Its permission bits.
    mode: int
    #: A file's size and SHA-256, a link's target.
    size: int = 0
    digest: bytes = b""
    target: str = ""


#: What a folder held when it was sent: each member by its path from the
#: folder, ``/``-separated.
Manifest = dict[str, Entry]


@dataclass(frozen=True)
class Applied:
    """What :func:`apply` changed in the run's folder: the paths it wrote
    (made or changed) and removed."""

    written: tuple[str, ...]
    removed: tuple[str, ...]


class _Hashing:
    """A file read through, its bytes hashed as they're read."""

    def __init__(self, file: BinaryIO) -> None:
        self.file = file
        self.hash = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.file.read(size)
        self.hash.update(data)
        return data


def _members(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """:return: The folder's members (not following links), parents before
    what's in them, in name order."""
    waiting = [("", root)]
    while waiting:
        prefix, folder = waiting.pop()
        try:
            with os.scandir(folder) as found:
                entries = sorted(found, key=lambda entry: entry.name)
        except OSError as error:
            raise SyncError(f"{folder} can't be read: {error.strerror or error}") from None
        below = []
        for entry in entries:
            name = f"{prefix}{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SyncError(f"{entry.path} can't be read: {error.strerror or error}") from None
            yield name, info
            if stat.S_ISDIR(info.st_mode):
                below.append((f"{name}/", Path(entry.path)))
        waiting.extend(reversed(below))


def pack(root: Path, out: BinaryIO) -> Manifest:
    """
    Write a folder's contents to ``out`` as a tar.

    :param root: The folder (its own entry isn't in the tar).
    :param out: Where the tar goes; its errors (one that stops it past a
        limit) come out of this as they are.
    :return: What it sent.
    :raises SyncError: When the folder can't be read.
    """
    manifest: Manifest = {}
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        for name, info in _members(root):
            mode = stat.S_IMODE(info.st_mode) & 0o777
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.mtime = int(info.st_mtime)
            path = root / name
            if stat.S_ISDIR(info.st_mode):
                member.type = tarfile.DIRTYPE
                tar.addfile(member)
                manifest[name] = Entry("dir", mode)
            elif stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as error:
                    raise SyncError(f"{path} can't be read: {error.strerror or error}") from None
                member.type = tarfile.SYMTYPE
                member.linkname = target
                tar.addfile(member)
                manifest[name] = Entry("link", mode, target=target)
            elif stat.S_ISREG(info.st_mode):
                member.size = info.st_size
                try:
                    with path.open("rb") as file:
                        hashing = _Hashing(file)
                        tar.addfile(member, hashing)  # type: ignore[arg-type]  # read() is all tarfile reads
                except OSError as error:
                    # Gone, unreadable, or shorter than it was a moment before.
                    raise SyncError(f"{path} can't be read: {getattr(error, 'strerror', None) or error}") from None
                manifest[name] = Entry("file", mode, size=info.st_size, digest=hashing.hash.digest())
    return manifest


def _relative(name: str) -> str | None:
    """:return: A member's path from the folder, ``/``-separated; None for
    the folder itself.
    :raises SyncError: When it isn't a path in the folder."""
    path = PurePosixPath(name)
    parts = [part for part in path.parts if part != "."]
    if path.is_absolute() or ".." in parts:
        raise SyncError(f"it came back with {name!r}, which isn't a path in the run's folder")
    return "/".join(parts) or None


def _kind(member: tarfile.TarInfo) -> Kind | None:
    if member.isdir():
        return "dir"
    if member.issym():
        return "link"
    if member.isreg() or member.islnk():
        return "file"
    return None


class _Folder:
    """The run's folder, written only where its paths lead inside it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def place(self, name: str, *, make: bool = True) -> Path:
        """:return: Where a member goes: in its parent folder, links on the
        way followed, made when it isn't there (unless not to ``make``).
        :raises SyncError: When its parent leads out of the run's folder."""
        path = self.root.joinpath(*PurePosixPath(name).parts)
        parent = Path(os.path.realpath(path.parent))
        if not parent.is_relative_to(self.root):
            raise SyncError(f"{name} would be written outside the run's folder")
        if make:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return parent / path.name

    @staticmethod
    def clear(path: Path) -> None:
        """Remove what's at a path (a folder with what's in it), if anything."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()


def _digest(file: IO[bytes]) -> bytes:
    hashed = hashlib.sha256()
    while block := file.read(BLOCK):
        hashed.update(block)
    return hashed.digest()


def apply(archive: BinaryIO, root: Path, before: Manifest) -> Applied:
    """
    Make in the run's folder the changes made to its copy.

    :param archive: The copy's contents as a tar (seekable).
    :param root: The run's folder.
    :param before: What the copy started as (:func:`pack`).
    :return: What it changed.
    :raises SyncError: When the archive isn't one, a path in it isn't in the
        run's folder (then nothing is changed), or a change can't be made.
    """
    try:
        with tarfile.open(fileobj=archive, mode="r:") as tar:
            members: dict[str, tarfile.TarInfo] = {}
            for member in tar:
                name = _relative(member.name)
                if name is not None and _kind(member) is not None:
                    members[name] = member
            folder = _Folder(root)
            removed = _remove(folder, [name for name in before if name not in members])
            written = [name for name, member in members.items() if _write(folder, tar, name, member, before.get(name))]
    except tarfile.TarError as error:
        raise SyncError(f"what came back isn't a tar: {error}") from None
    except OSError as error:
        raise SyncError(f"the run's folder couldn't be changed: {getattr(error, 'strerror', None) or error}") from None
    return Applied(written=tuple(written), removed=tuple(removed))


def _remove(folder: _Folder, names: list[str]) -> list[str]:
    """Remove what the copy no longer has, deepest first: a folder only when
    it's empty (what another step put in it stays)."""
    removed = []
    for name in sorted(names, key=lambda each: each.count("/"), reverse=True):
        try:
            path = folder.place(name, make=False)
            info = os.lstat(path)
        except (SyncError, FileNotFoundError):
            continue
        try:
            if stat.S_ISDIR(info.st_mode):
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed


def _write(folder: _Folder, tar: tarfile.TarFile, name: str, member: tarfile.TarInfo, old: Entry | None) -> bool:
    """:return: Whether a member was changed in the copy, and so written."""
    kind = _kind(member)
    mode = member.mode & 0o777
    if kind == "dir":
        if old is not None and old.kind == "dir" and old.mode == mode:
            return False
        path = folder.place(name)
        if not path.is_dir() or path.is_symlink():
            folder.clear(path)
            path.mkdir(mode=0o700)
        path.chmod(mode | 0o700)
        return True
    if kind == "link":
        if old is not None and old.kind == "link" and old.target == member.linkname:
            return False
        path = folder.place(name)
        folder.clear(path)
        os.symlink(member.linkname, path)
        return True
    content = tar.extractfile(member)
    if content is None:
        return False
    if old is not None and old.kind == "file" and old.mode == mode:
        if (member.islnk() or member.size == old.size) and _digest(content) == old.digest:
            return False
        content = tar.extractfile(member)
        assert content is not None
    path = folder.place(name)
    # Written beside it, then put in its place: a link there is replaced, not followed.
    written = path.with_name(f".forge-{uuid.uuid4().hex[:12]}")
    with written.open("xb") as file:
        shutil.copyfileobj(content, file, BLOCK)
    written.chmod(mode | 0o600)
    os.utime(written, (member.mtime, member.mtime))
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    os.replace(written, path)
    return True

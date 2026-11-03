from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class FilesystemSecurityError(RuntimeError):
    pass


def absolute_path(path: Path) -> Path:
    """Return an absolute path without resolving or following symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _reject_symlink(path: Path) -> None:
    if path_lexists(path) and stat.S_ISLNK(os.lstat(path).st_mode):
        raise FilesystemSecurityError(f"symlink path is forbidden: {path}")


def _require_owner(path: Path, metadata: os.stat_result) -> None:
    expected_uid = os.geteuid()
    if metadata.st_uid != expected_uid:
        raise FilesystemSecurityError(
            f"unsafe owner for {path}: uid {metadata.st_uid}, expected {expected_uid}"
        )


def _secure_directory_descriptor(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FilesystemSecurityError(f"directory is a symlink or unsafe type: {path}") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise FilesystemSecurityError(f"path is not a directory: {path}")
        _require_owner(path, metadata)
        if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
            os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
        verified = os.fstat(descriptor)
        _require_owner(path, verified)
        if not stat.S_ISDIR(verified.st_mode):
            raise FilesystemSecurityError(f"path is not a directory: {path}")
        if stat.S_IMODE(verified.st_mode) != PRIVATE_DIRECTORY_MODE:
            raise FilesystemSecurityError(f"directory mode is not 0700: {path}")
    finally:
        os.close(descriptor)


def secure_directory(
    path: Path,
    *,
    create: bool = False,
    exclusive: bool = False,
) -> Path:
    path = absolute_path(path)
    if exclusive and path_lexists(path):
        raise FilesystemSecurityError(f"private directory already exists: {path}")

    _reject_symlink(path)
    if not path_lexists(path):
        if not create:
            raise FilesystemSecurityError(f"required directory is missing: {path}")
        missing: list[Path] = []
        cursor = path
        while not path_lexists(cursor):
            missing.append(cursor)
            cursor = cursor.parent
        _reject_symlink(cursor)
        parent_metadata = os.lstat(cursor)
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise FilesystemSecurityError(f"parent path is not a directory: {cursor}")
        for directory in reversed(missing):
            os.mkdir(directory, PRIVATE_DIRECTORY_MODE)
            _secure_directory_descriptor(directory)

    _reject_symlink(path)
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise FilesystemSecurityError(f"directory symlink is forbidden: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise FilesystemSecurityError(f"path is not a directory: {path}")
    _secure_directory_descriptor(path)
    return path


def _secure_existing_file(path: Path) -> None:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise FilesystemSecurityError(f"file symlink is forbidden: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise FilesystemSecurityError(f"path is not a regular file: {path}")
    _require_owner(path, metadata)

    # Opening and closing another descriptor for an active SQLite file drops
    # this process's POSIX locks, including locks held by other connections.
    # A private file needs only lstat validation. Keep the no-follow descriptor
    # path below exclusively for files whose permissions require correction.
    if stat.S_IMODE(metadata.st_mode) == PRIVATE_FILE_MODE:
        return

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FilesystemSecurityError(f"file symlink is forbidden: {path}") from exc
        raise
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FilesystemSecurityError(f"path is not a regular file: {path}")
        _require_owner(path, opened)
        if stat.S_IMODE(opened.st_mode) != PRIVATE_FILE_MODE:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        verified = os.fstat(descriptor)
        _require_owner(path, verified)
        if not stat.S_ISREG(verified.st_mode):
            raise FilesystemSecurityError(f"path is not a regular file: {path}")
        if stat.S_IMODE(verified.st_mode) != PRIVATE_FILE_MODE:
            raise FilesystemSecurityError(f"file mode is not 0600: {path}")
    finally:
        os.close(descriptor)


def secure_file(path: Path, *, required: bool = True) -> Path:
    path = absolute_path(path)
    _reject_symlink(path)
    if not path_lexists(path):
        if required:
            raise FilesystemSecurityError(f"required file is missing: {path}")
        return path
    _secure_existing_file(path)
    return path


def create_secure_file(path: Path) -> Path:
    path = absolute_path(path)
    secure_directory(path.parent, create=False)
    _reject_symlink(path)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    except FileExistsError as exc:
        raise FilesystemSecurityError(f"private file already exists: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FilesystemSecurityError(f"file symlink is forbidden: {path}") from exc
        raise
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FilesystemSecurityError(f"path is not a regular file: {path}")
        _require_owner(path, metadata)
        if stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
            raise FilesystemSecurityError(f"file mode is not 0600: {path}")
    finally:
        os.close(descriptor)
    return path


def write_secure_text(path: Path, content: str) -> Path:
    path = absolute_path(path)
    secure_directory(path.parent, create=False)
    _reject_symlink(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        created = True
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        secure_file(path)
        return path
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if created and path_lexists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise


def secure_sqlite_family(database_path: Path, *, require_database: bool = True) -> Path:
    database_path = absolute_path(database_path)
    secure_directory(database_path.parent, create=False)
    secure_file(database_path, required=require_database)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(str(database_path) + suffix)
        try:
            # SQLite removes these files when its last connection closes. A
            # concurrent connection may observe one disappear between lstat
            # and open; absence is valid only for this optional sidecar.
            secure_file(sidecar, required=False)
        except FileNotFoundError as exc:
            if exc.filename is None or absolute_path(Path(exc.filename)) != sidecar:
                raise
    return database_path

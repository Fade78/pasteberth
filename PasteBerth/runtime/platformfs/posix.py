"""POSIX implementation of the semantic platform filesystem contract."""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .base import (
    BusyError,
    DirectoryHandle,
    EntryChangedError,
    EntryExistsError,
    EntryInfo,
    FileHandle,
    FileIdentity,
    InvalidNameError,
    PermissionAudit,
    PermissionSecurityError,
    PlatformCapabilities,
    PlatformFS,
    UnsafeLinkError,
    VolumeSpace,
)


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class PosixDirectoryHandle(DirectoryHandle):
    def __init__(self, path: Path, fd: int):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            raise UnsafeLinkError(f"directory expected: {path}")
        super().__init__(path, FileIdentity(info.st_dev, info.st_ino))
        self._fd = fd

    @property
    def fd(self) -> int:
        if self.closed:
            raise ValueError("directory handle is closed")
        return self._fd

    def _close_native(self) -> None:
        os.close(self._fd)


class PosixFileHandle(FileHandle):
    def __init__(self, stream, fd: int, identity: FileIdentity, size: int):
        super().__init__(stream, identity, size)
        self._fd = fd

    @property
    def fd(self) -> int:
        if self.closed:
            raise ValueError("file handle is closed")
        return self._fd

    def _sync_native(self) -> None:
        os.fsync(self.fd)


class PosixPlatformFS(PlatformFS):
    """Common descriptor-relative implementation for POSIX systems."""

    backend_name = "posix"

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            backend=self.backend_name,
            safe_directory_open=bool(_O_DIRECTORY and _O_NOFOLLOW),
            safe_file_open=bool(_O_NOFOLLOW),
            exclusive_create=True,
            identity=True,
            hard_link_guard=True,
            atomic_no_replace_rename=False,
            expected_remove=True,
            interprocess_locks=True,
            file_flush=True,
            directory_flush=True,
            volume_space=True,
            volume_identity=True,
            private_permissions=True,
        )

    def _require_safe_open(self) -> None:
        self.capabilities.require("safe_directory_open", "safe_file_open")

    @staticmethod
    def _native_fd(directory: DirectoryHandle | int) -> int:
        if isinstance(directory, PosixDirectoryHandle):
            return directory.fd
        # Kept for the v1.5 private-helper tests while callers migrate to
        # DirectoryHandle.  Native descriptor handling remains in this module.
        if isinstance(directory, int):
            return directory
        raise TypeError("POSIX handle expected")

    @staticmethod
    def _path(path: Path) -> Path:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError(f"directory path must be absolute: {path}")
        return path

    def open_directory(
        self,
        path: Path,
        *,
        create: bool = False,
        mode: int = 0o700,
    ) -> PosixDirectoryHandle:
        self._require_safe_open()
        path = self._path(path)
        fd = os.open(
            path.anchor,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
        )
        try:
            for part in path.parts[1:]:
                next_fd = -1
                try:
                    try:
                        next_fd = os.open(
                            part,
                            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                            dir_fd=fd,
                        )
                    except FileNotFoundError:
                        if not create:
                            raise
                        try:
                            os.mkdir(part, mode=mode, dir_fd=fd)
                        except FileExistsError:
                            pass
                        next_fd = os.open(
                            part,
                            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                            dir_fd=fd,
                        )
                        os.fchmod(next_fd, mode)
                    os.close(fd)
                    fd = next_fd
                    next_fd = -1
                finally:
                    if next_fd >= 0:
                        os.close(next_fd)
            return PosixDirectoryHandle(path, fd)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _mode_flags(mode: str) -> tuple[int, bool]:
        if mode in {"rb", "r"}:
            return os.O_RDONLY, mode == "r"
        if mode in {"wb", "w"}:
            return os.O_WRONLY, mode == "w"
        if mode in {"r+b", "rb+", "w+b", "wb+"}:
            return os.O_RDWR, False
        raise ValueError(f"unsupported file mode: {mode!r}")

    @staticmethod
    def _stream_from_fd(fd: int, mode: str):
        if "b" in mode:
            return os.fdopen(fd, mode)
        return os.fdopen(fd, mode, encoding="utf-8")

    def _regular_handle(self, fd: int, name: str, mode: str) -> PosixFileHandle:
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            raise
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise UnsafeLinkError(f"file is not regular: {name!r}")
        identity = FileIdentity(info.st_dev, info.st_ino)
        try:
            stream = self._stream_from_fd(fd, mode)
        except BaseException:
            os.close(fd)
            raise
        return PosixFileHandle(stream, stream.fileno(), identity, info.st_size)

    def open_existing(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "rb",
    ) -> PosixFileHandle:
        self.validate_component(name)
        if not isinstance(directory, PosixDirectoryHandle):
            raise TypeError("POSIX handle expected")
        flags, _text = self._mode_flags(mode)
        try:
            fd = os.open(
                name,
                flags | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
                dir_fd=directory.fd,
            )
            return self._regular_handle(fd, name, mode)
        except FileNotFoundError:
            raise
        except UnsafeLinkError:
            raise
        except OSError:
            raise

    def create_exclusive(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "wb",
        permissions: int = 0o600,
    ) -> PosixFileHandle:
        self.validate_component(name)
        if not isinstance(directory, PosixDirectoryHandle):
            raise TypeError("POSIX handle expected")
        flags, _text = self._mode_flags(mode)
        try:
            fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                permissions,
                dir_fd=directory.fd,
            )
            os.fchmod(fd, permissions)
            return self._regular_handle(fd, name, mode)
        except FileExistsError as exc:
            raise EntryExistsError(str(exc)) from exc

    @staticmethod
    def _entry_info_from_stat(name: str, info: os.stat_result) -> EntryInfo:
        return EntryInfo(
            name=name,
            identity=FileIdentity(info.st_dev, info.st_ino),
            size=info.st_size,
            is_regular=stat.S_ISREG(info.st_mode),
            is_symlink=stat.S_ISLNK(info.st_mode),
            owner=getattr(info, "st_uid", None),
            mode=stat.S_IMODE(info.st_mode),
            modified_ns=getattr(info, "st_mtime_ns", None),
            changed_ns=getattr(info, "st_ctime_ns", None),
        )

    def entry_info(self, directory: DirectoryHandle, name: str) -> EntryInfo | None:
        self.validate_component(name)
        directory_fd = self._native_fd(directory)
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return self._entry_info_from_stat(name, info)

    def entries(self, directory: DirectoryHandle) -> tuple[EntryInfo, ...]:
        directory_fd = self._native_fd(directory)
        result: list[EntryInfo] = []
        with os.scandir(directory_fd) as scan:
            for entry in scan:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                result.append(self._entry_info_from_stat(entry.name, info))
        return tuple(result)

    def identity(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        require_regular: bool = True,
    ) -> FileIdentity | None:
        info = self.entry_info(directory, name)
        if info is None:
            return None
        if require_regular and not info.is_regular:
            raise UnsafeLinkError(f"file is not regular: {name!r}")
        return info.identity

    def _check_expected(
        self,
        directory: DirectoryHandle,
        name: str,
        expected: FileIdentity,
    ) -> None:
        actual = self.identity(directory, name)
        if actual != expected:
            raise EntryChangedError(f"file changed during operation: {name!r}")

    def link_expected(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        expected: FileIdentity,
    ) -> None:
        self.validate_component(source)
        self.validate_component(target)
        directory_fd = self._native_fd(directory)
        self._check_expected(directory, source, expected)
        try:
            os.link(
                source,
                target,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise EntryExistsError(str(exc)) from exc
        if self.identity(directory, target) != expected:
            raise EntryChangedError(f"foreign target appeared: {target!r}")

    def rename_noreplace_fallback(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected: FileIdentity | None = None,
    ) -> None:
        """Portable fallback: link then unlink, never replacing the target."""
        self.validate_component(source)
        self.validate_component(target)
        directory_fd = self._native_fd(directory)
        source_identity = self.identity(directory, source)
        if expected is not None and source_identity != expected:
            raise EntryChangedError(f"source changed: {source!r}")
        if source_identity is None:
            raise FileNotFoundError(source)
        try:
            os.link(
                source,
                target,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise EntryExistsError(str(exc)) from exc
        if self.identity(directory, source) != source_identity:
            # Keep the extra link: deleting it would require trusting a name
            # after the source changed.
            raise EntryChangedError(f"source changed: {source!r}")
        if self.identity(directory, target) != source_identity:
            raise EntryChangedError(f"foreign target appeared: {target!r}")
        os.unlink(source, dir_fd=directory_fd)

    def rename_noreplace(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected: FileIdentity | None = None,
    ) -> None:
        return self.rename_noreplace_fallback(
            directory,
            source,
            target,
            expected=expected,
        )

    def move_noreplace(
        self,
        source_directory: DirectoryHandle,
        source: str,
        target_directory: DirectoryHandle,
        target: str,
        *,
        expected: FileIdentity | None = None,
    ) -> None:
        self.validate_component(source)
        self.validate_component(target)
        source_fd = self._native_fd(source_directory)
        target_fd = self._native_fd(target_directory)
        source_identity = self.identity(source_directory, source)
        if source_identity is None:
            raise FileNotFoundError(source)
        if expected is not None and source_identity != expected:
            raise EntryChangedError(f"source changed: {source!r}")
        if self.entry_info(target_directory, target) is not None:
            raise EntryExistsError(f"target already exists: {target!r}")
        try:
            os.link(
                source,
                target,
                src_dir_fd=source_fd,
                dst_dir_fd=target_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise EntryExistsError(str(exc)) from exc
        if self.identity(target_directory, target) != source_identity:
            raise EntryChangedError(f"foreign target appeared: {target!r}")
        if self.identity(source_directory, source) != source_identity:
            raise EntryChangedError(f"source changed: {source!r}")
        try:
            os.unlink(source, dir_fd=source_fd)
        except FileNotFoundError as exc:
            raise EntryChangedError(f"source disappeared: {source!r}") from exc

    def remove_expected(
        self,
        directory: DirectoryHandle,
        name: str,
        expected: FileIdentity | None,
    ) -> bool:
        self.validate_component(name)
        if expected is None:
            return False
        directory_fd = self._native_fd(directory)
        actual = self.identity(directory, name)
        if actual is None:
            return True
        if actual != expected:
            return False
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            return True
        return True

    def replace(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected_source: FileIdentity | None = None,
        expected_target: FileIdentity | None = None,
    ) -> None:
        self.validate_component(source)
        self.validate_component(target)
        directory_fd = self._native_fd(directory)
        source_identity = self.identity(directory, source)
        if source_identity is None or (
            expected_source is not None and source_identity != expected_source
        ):
            raise EntryChangedError(f"source changed: {source!r}")
        target_info = self.entry_info(directory, target)
        if target_info is not None:
            if not target_info.is_regular:
                raise UnsafeLinkError(f"target is not regular: {target!r}")
            if not self.is_owned(target_info):
                raise PermissionSecurityError(f"target is not owned: {target!r}")
            if expected_target is not None and target_info.identity != expected_target:
                raise EntryChangedError(f"target changed: {target!r}")
        elif expected_target is not None:
            raise EntryChangedError(f"target disappeared: {target!r}")
        os.rename(source, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        if self.identity(directory, target) != source_identity:
            raise EntryChangedError(f"foreign target appeared: {target!r}")

    @contextmanager
    def acquire_lock(
        self,
        directory: DirectoryHandle,
        *,
        name: str = ".pasteberth.lock",
        exclusive: bool,
        blocking: bool = True,
    ):
        self.validate_component(name)
        if not isinstance(directory, PosixDirectoryHandle):
            raise TypeError("POSIX handle expected")
        fd = -1
        locked = False
        try:
            fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
                dir_fd=directory.fd,
            )
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafeLinkError(f"lock is not regular: {name!r}")
            uid_getter = getattr(os, "getuid", None)
            if uid_getter is not None and info.st_uid != uid_getter():
                raise PermissionSecurityError(f"lock is not owned: {name!r}")
            os.fchmod(fd, 0o600)
            flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except OSError as exc:
                if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise BusyError(f"lock is busy: {name!r}") from exc
                raise
            locked = True
            yield
        finally:
            if fd >= 0:
                try:
                    if locked:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def flush_directory(self, directory: DirectoryHandle) -> None:
        directory_fd = self._native_fd(directory)
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise OSError(f"cannot synchronize directory: {exc}") from exc

    def volume_space(self, directory: DirectoryHandle) -> VolumeSpace:
        directory_fd = self._native_fd(directory)
        info = os.fstatvfs(directory_fd)
        total = info.f_blocks * info.f_frsize
        available = info.f_bavail * info.f_frsize
        if total <= 0:
            raise OSError("filesystem has no measurable capacity")
        return VolumeSpace(total, available)

    def volume_identity(self, directory: DirectoryHandle) -> int:
        return os.fstat(self._native_fd(directory)).st_dev

    def check_access(
        self,
        path: Path,
        *,
        read: bool = False,
        write: bool = False,
        execute: bool = False,
    ) -> bool:
        mode = 0
        if read:
            mode |= os.R_OK
        if write:
            mode |= os.W_OK
        if execute:
            mode |= os.X_OK
        return os.access(Path(path), mode)

    def path_version(self, path: Path) -> tuple[int, int, int] | None:
        path = Path(path)
        try:
            with self.open_directory(path.parent) as directory:
                info = self.entry_info(directory, path.name)
        except (FileNotFoundError, OSError, ValueError):
            return None
        if info is None or not info.is_regular or info.is_symlink or not self.is_owned(info):
            return None
        return (
            info.identity.file_id,
            info.modified_ns or 0,
            info.changed_ns or 0,
        )

    def owner_token(self) -> str:
        return str(getattr(os, "getuid", lambda: 0)())

    def runtime_directory(self) -> Path:
        runtime_root = os.environ.get("XDG_RUNTIME_DIR")
        return Path(runtime_root) if runtime_root else Path.home() / ".cache"

    def first_symlink_component(self, path: Path) -> Path | None:
        path = Path(path)
        current = Path(path.anchor) if path.is_absolute() else Path(".")
        parts = path.parts[1:] if path.is_absolute() else path.parts
        for part in parts:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                break
            if stat.S_ISLNK(mode):
                return current
        return None

    def audit_permissions(
        self,
        path: Path,
        *,
        directory: bool,
    ) -> PermissionAudit:
        path = Path(path)
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        private_bits = 0o077 if directory else 0o077
        private = not bool(mode & private_bits)
        uid_getter = getattr(os, "getuid", None)
        owner = info.st_uid
        if uid_getter is not None and owner != uid_getter():
            private = False
        detail = None if private else "owner or permissions are too broad"
        return PermissionAudit(path, private, owner, mode, detail)

    def is_owned(self, entry: EntryInfo) -> bool:
        uid_getter = getattr(os, "getuid", None)
        return uid_getter is None or entry.owner == uid_getter()

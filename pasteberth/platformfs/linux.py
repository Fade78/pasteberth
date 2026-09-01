"""Linux specialization of the POSIX platform backend."""
from __future__ import annotations

import ctypes
import errno
import os
import re

from pasteberth.platformfs.base import EntryChangedError, EntryExistsError
from pasteberth.platformfs.posix import PosixDirectoryHandle, PosixPlatformFS


_RENAME_NOREPLACE = 1
_RENAME_NOREPLACE_FALLBACK_ERRNOS = frozenset(
    {
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EINVAL,
    }
)
_SHARED_FILESYSTEM_TYPES = frozenset(
    {
        "9p",
        "drvfs",
        "fuse",
        "fuseblk",
        "virtiofs",
        "v9fs",
    }
)
_MOUNTINFO_PATH = "/proc/self/mountinfo"
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


class LinuxPlatformFS(PosixPlatformFS):
    backend_name = "linux"

    def __init__(self):
        self._renameat2 = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            function = libc.renameat2
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            function.restype = ctypes.c_int
            self._renameat2 = function
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _decode_mountinfo_path(value: str) -> str:
        return _MOUNTINFO_ESCAPE.sub(
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    @classmethod
    def _filesystem_type(cls, path) -> str | None:
        """Return the most specific filesystem type containing ``path``."""
        path = os.path.normpath(os.path.abspath(os.fspath(path)))
        selected_mount = None
        try:
            with open(
                _MOUNTINFO_PATH,
                "r",
                encoding="utf-8",
                errors="surrogateescape",
            ) as mountinfo:
                for line in mountinfo:
                    before_separator, separator, after_separator = line.rstrip(
                        "\n"
                    ).partition(" - ")
                    if not separator:
                        continue
                    fields = before_separator.split()
                    filesystem_fields = after_separator.split()
                    if len(fields) < 5 or not filesystem_fields:
                        continue
                    mount_point = cls._decode_mountinfo_path(fields[4])
                    mount_point = os.path.normpath(mount_point)
                    if mount_point == os.path.sep:
                        is_containing_mount = path.startswith(os.path.sep)
                    else:
                        is_containing_mount = path == mount_point or path.startswith(
                            mount_point + os.path.sep
                        )
                    if is_containing_mount and (
                        selected_mount is None
                        or len(mount_point) > len(selected_mount[0])
                    ):
                        selected_mount = (mount_point, filesystem_fields[0])
        except (OSError, UnicodeError):
            return None
        return None if selected_mount is None else selected_mount[1]

    @classmethod
    def _uses_shared_filesystem(cls, path) -> bool:
        filesystem_type = cls._filesystem_type(path)
        return filesystem_type in _SHARED_FILESYSTEM_TYPES or bool(
            filesystem_type and filesystem_type.startswith("fuse.")
        )

    @property
    def capabilities(self):
        capabilities = super().capabilities
        return capabilities.__class__(
            **{
                **capabilities.as_dict(),
                "atomic_no_replace_rename": self._renameat2 is not None,
            }
        )

    def rename_noreplace(
        self,
        directory,
        source: str,
        target: str,
        *,
        expected=None,
    ) -> None:
        if self._renameat2 is None:
            return super().rename_noreplace(
                directory,
                source,
                target,
                expected=expected,
            )
        self.validate_component(source)
        self.validate_component(target)
        if not isinstance(directory, PosixDirectoryHandle):
            raise TypeError("POSIX handle expected")
        if self._uses_shared_filesystem(directory.path):
            return super().rename_noreplace(
                directory,
                source,
                target,
                expected=expected,
            )
        source_identity = self.identity(directory, source)
        if expected is not None and source_identity != expected:
            raise EntryChangedError(f"source changed: {source!r}")
        if source_identity is None:
            raise FileNotFoundError(source)
        result = self._renameat2(
            directory.fd,
            os.fsencode(source),
            directory.fd,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise EntryExistsError(
                    errno.errorcode.get(error_number, "destination exists")
                )
            if error_number in _RENAME_NOREPLACE_FALLBACK_ERRNOS:
                return super().rename_noreplace(
                    directory,
                    source,
                    target,
                    expected=expected,
                )
            raise OSError(error_number, os.strerror(error_number), source, target)
        if self.identity(directory, target) != source_identity:
            raise EntryChangedError(f"foreign target appeared: {target!r}")

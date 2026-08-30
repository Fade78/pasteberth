"""Linux specialization of the POSIX platform backend."""
from __future__ import annotations

import ctypes
import errno
import os

from pasteberth.platformfs.base import EntryChangedError, EntryExistsError
from pasteberth.platformfs.posix import PosixDirectoryHandle, PosixPlatformFS


_RENAME_NOREPLACE = 1


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
            raise TypeError("handle POSIX attendu")
        source_identity = self.identity(directory, source)
        if expected is not None and source_identity != expected:
            raise EntryChangedError(f"source modifiée : {source!r}")
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
            raise OSError(error_number, os.strerror(error_number), source, target)
        if self.identity(directory, target) != source_identity:
            raise EntryChangedError(f"cible étrangère apparue : {target!r}")
